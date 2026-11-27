#!/usr/bin/env python3
"""Validation d'une topologie du lab (sonic-lab.clab.yml + configs/) avant plan ou déploiement.

Fait autorité dans l'agent GitOps (lab/gitops/agent.py) ; une copie, ci/validate.py, sert à la CI du
projet GitLab de l'infra. Autonome : bibliothèque standard + PyYAML, aucun import du lab.

    python3 ci/validate.py [DOSSIER]        (défaut : dossier courant ; code de sortie 1 si erreur)

Règles : nom du lab et réseau de management inchangés, IP de management valides et uniques, kinds et
images autorisés, éléments fournis par le lab présents sur les routeurs (bootstrap, RESTCONF, env),
binds limités aux confs de démarrage du nœud et aux fichiers du lab, liens cohérents. Les nœuds du
lab sont des conteneurs privilégiés : ce qui n'est pas explicitement permis est refusé.

Variables (valeurs du lab par défaut) : LAB_NAME, MGMT_NETWORK, MGMT_SUBNET, GITLAB_IP,
FLOW_COLLECTOR_IP (collecteur sFlow), GITOPS_ALLOWED_IMAGES (liste séparée par des virgules).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TOPOLOGY_FILE = "sonic-lab.clab.yml"
CONFIG_FILES = ("config_db.json", "frr.conf")
KINDS = ("sonic-vs", "linux")
SONIC = "sonic-vs"
MAX_SONIC_ETH = 32  # HWSKU Force10-S6000 : 32 ports, eth1..eth32 = Ethernet0..Ethernet124

TOP_KEYS = {"name", "mgmt", "topology"}
MGMT_KEYS = {"network", "ipv4-subnet"}
TOPOLOGY_KEYS = {"kinds", "nodes", "links"}
KIND_KEYS = {"image", "env", "sysctls", "binds", "exec"}
NODE_KEYS = KIND_KEYS | {"kind", "mgmt-ipv4"}

# Fichiers fournis par le lab, montés dans chaque routeur (source relative au dossier du lab : cible)
LAB_BINDS = {
    "sonic/lab-bootstrap.sh": "/usr/local/bin/lab-bootstrap.sh",
    "sonic/restconf": "/usr/local/lib/sonic-lab/restconf",
    "../web/backend/app/frr_config.py": "/usr/local/lib/sonic-lab/frr_config.py",
    "../web/backend/app/parsers.py": "/usr/local/lib/sonic-lab/parsers.py",
}
STARTUP_TARGETS = {"config_db.json": "/var/sonic/config_db.json", "frr.conf": "/etc/sonic/lab/frr.conf"}
SONIC_ENV = ("BGPD_GRACEFUL_RESTART_ARG", "RESTCONF_USER", "RESTCONF_PASSWORD")
BOOTSTRAP_EXEC = "bash /usr/local/bin/lab-bootstrap.sh"

_NODE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENDPOINT = re.compile(r"^([a-z][a-z0-9-]{0,30}):eth([0-9]{1,3})$")


@dataclass
class Settings:
    lab_name: str = "sonic-lab"
    mgmt_network: str = "sonic-lab-mgmt"
    mgmt_subnet: str = "172.20.30.0/24"
    # IP fixes des services de compose sur le réseau de management -> service
    reserved_ips: dict[str, str] = field(default_factory=lambda: {"172.20.30.5": "GitLab", "172.20.30.6": "collecteur sFlow"})
    allowed_images: tuple[str, ...] = ("docker-sonic-vs:latest", "alpine:3.22")

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        images = tuple(i.strip() for i in (env("GITOPS_ALLOWED_IMAGES") or "").split(",") if i.strip())
        return cls(
            lab_name=env("LAB_NAME") or cls.lab_name,
            mgmt_network=env("MGMT_NETWORK") or cls.mgmt_network,
            mgmt_subnet=env("MGMT_SUBNET") or cls.mgmt_subnet,
            reserved_ips={
                env("GITLAB_IP") or "172.20.30.5": "GitLab",
                env("FLOW_COLLECTOR_IP") or "172.20.30.6": "collecteur sFlow",
            },
            allowed_images=images or cls.allowed_images,
        )


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_topology(root: Path) -> dict[str, Any]:
    """Lit root/sonic-lab.clab.yml ; ValueError si absent ou illisible."""
    path = root / TOPOLOGY_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{TOPOLOGY_FILE} absent") from None
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ValueError(f"{TOPOLOGY_FILE} illisible : {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{TOPOLOGY_FILE} : un document YAML (dictionnaire) est attendu")
    return data


def _as_dict(value: Any, where: str, report: Report) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        report.error(f"{where} : dictionnaire attendu")
        return {}
    return value


def _as_list(value: Any, where: str, report: Report) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        report.error(f"{where} : liste attendue")
        return []
    return value


def _check_keys(value: dict[str, Any], allowed: set[str], where: str, report: Report) -> None:
    for key in sorted(set(value) - allowed):
        report.error(f"{where} : clé « {key} » non autorisée (permises : {', '.join(sorted(allowed))})")


def parse_bind(bind: Any) -> tuple[str, str, str] | None:
    """« source:cible[:mode] » → (source, cible, mode)."""
    if not isinstance(bind, str):
        return None
    parts = bind.split(":")
    if len(parts) == 2:
        return parts[0], parts[1], ""
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return None


def _check_binds(binds: list[Any], node: str | None, where: str, report: Report) -> list[tuple[str, str]]:
    """Binds permis : fichiers du lab (LAB_BINDS) et, sur un nœud, configs/<nœud>/<fichier>."""
    out = []
    for bind in binds:
        parsed = parse_bind(bind)
        if parsed is None:
            report.error(f"{where} : bind invalide {bind!r} (format source:cible[:ro])")
            continue
        source, target, mode = parsed
        if mode not in ("", "ro", "rw"):
            report.error(f"{where} : bind {bind!r} : mode « {mode} » non autorisé")
        if source in LAB_BINDS:
            if target != LAB_BINDS[source]:
                report.error(f"{where} : {source} doit être monté sur {LAB_BINDS[source]}")
        elif node is not None and source in {f"configs/{node}/{f}" for f in CONFIG_FILES}:
            pass
        else:
            allowed = ", ".join([*LAB_BINDS, *(f"configs/{node}/{f}" for f in CONFIG_FILES)] if node else LAB_BINDS)
            report.error(f"{where} : source de bind non autorisée : {source!r} (permises : {allowed})")
        out.append((source, target))
    return out


def _env(value: Any, where: str, report: Report) -> dict[str, Any]:
    env = _as_dict(value, where, report)
    for k, v in env.items():
        if not isinstance(v, (str, int, float, bool)) and v is not None:
            report.error(f"{where} : valeur de {k} : texte attendu")
    return env


def validate(root: Path, settings: Settings | None = None) -> Report:
    """Valide root/sonic-lab.clab.yml et root/configs/."""
    settings = settings or Settings()
    report = Report()
    try:
        data = load_topology(root)
    except ValueError as exc:
        report.error(str(exc))
        return report

    _check_keys(data, TOP_KEYS, "racine", report)
    if data.get("name") != settings.lab_name:
        report.error(f"name doit rester « {settings.lab_name} » (sinon containerlab déploierait un second lab)")

    mgmt = _as_dict(data.get("mgmt"), "mgmt", report)
    _check_keys(mgmt, MGMT_KEYS, "mgmt", report)
    if mgmt.get("network") != settings.mgmt_network:
        report.error(f"mgmt.network doit rester « {settings.mgmt_network} » (réseau créé par docker compose)")
    if mgmt.get("ipv4-subnet") != settings.mgmt_subnet:
        report.error(f"mgmt.ipv4-subnet doit rester « {settings.mgmt_subnet} »")
    subnet = ipaddress.ip_network(settings.mgmt_subnet)

    topo = _as_dict(data.get("topology"), "topology", report)
    _check_keys(topo, TOPOLOGY_KEYS, "topology", report)
    kinds = _as_dict(topo.get("kinds"), "topology.kinds", report)
    for kind_name, kind_def in kinds.items():
        where = f"kinds.{kind_name}"
        if kind_name not in KINDS:
            report.error(f"{where} : kind non pris en charge par le lab (permis : {', '.join(KINDS)})")
            continue
        kind_def = _as_dict(kind_def, where, report)
        _check_keys(kind_def, KIND_KEYS, where, report)
        _check_binds(_as_list(kind_def.get("binds"), f"{where}.binds", report), None, where, report)
        _env(kind_def.get("env"), f"{where}.env", report)

    nodes = _as_dict(topo.get("nodes"), "topology.nodes", report)
    if not nodes:
        report.error("topology.nodes : aucun nœud")
    used_ips: dict[str, str] = {}
    node_kinds: dict[str, str] = {}
    for name, node in nodes.items():
        where = f"nœud {name}"
        if not isinstance(name, str) or not _NODE_NAME.match(name):
            report.error(f"{where} : nom invalide (minuscules, chiffres, « - », 31 caractères au plus)")
        node = _as_dict(node, where, report)
        _check_keys(node, NODE_KEYS, where, report)
        kind = node.get("kind")
        if kind not in KINDS:
            report.error(f"{where} : kind obligatoire, parmi {', '.join(KINDS)}")
            continue
        node_kinds[name] = kind
        kind_def = kinds.get(kind) if isinstance(kinds.get(kind), dict) else {}

        image = node.get("image") or kind_def.get("image")
        if image not in settings.allowed_images:
            report.error(f"{where} : image {image!r} non autorisée (GITOPS_ALLOWED_IMAGES : {', '.join(settings.allowed_images)})")

        ip = node.get("mgmt-ipv4")
        try:
            addr = ipaddress.ip_address(str(ip))
        except ValueError:
            report.error(f"{where} : mgmt-ipv4 obligatoire (adresse IPv4 de {settings.mgmt_subnet})")
        else:
            if addr not in subnet or addr in (subnet.network_address, subnet.broadcast_address, subnet.network_address + 1):
                report.error(f"{where} : mgmt-ipv4 {ip} hors de {settings.mgmt_subnet} ou réservée (réseau, passerelle, diffusion)")
            elif str(addr) in settings.reserved_ips:
                report.error(f"{where} : mgmt-ipv4 {ip} réservée ({settings.reserved_ips[str(addr)]})")
            elif str(addr) in used_ips:
                report.error(f"{where} : mgmt-ipv4 {ip} déjà prise par {used_ips[str(addr)]}")
            else:
                used_ips[str(addr)] = name

        env = {**_env(kind_def.get("env"), where, report), **_env(node.get("env"), f"{where}.env", report)}
        binds = _check_binds(_as_list(node.get("binds"), f"{where}.binds", report), name, where, report)
        kind_binds = (parse_bind(x) for x in (kind_def.get("binds") or []))
        binds += [(b[0], b[1]) for b in kind_binds if b is not None]  # contrôlés avec le kind
        execs = [str(e) for e in (kind_def.get("exec") or [])]
        execs += [str(e) for e in _as_list(node.get("exec"), f"{where}.exec", report)]

        if kind == SONIC:
            for var in SONIC_ENV:
                if var not in env:
                    report.error(f"{where} : variable d'environnement {var} manquante (fournie par le lab dans kinds.sonic-vs.env)")
            sources = {b[0]: b[1] for b in binds}
            for source, target in LAB_BINDS.items():
                if sources.get(source) != target:
                    report.error(f"{where} : bind du lab manquant : {source}:{target}:ro (kinds.sonic-vs.binds)")
            if BOOTSTRAP_EXEC not in execs:
                report.error(f"{where} : exec « {BOOTSTRAP_EXEC} » manquant (bootstrap : FRR, RESTCONF)")
            for fname, target in STARTUP_TARGETS.items():
                source = f"configs/{name}/{fname}"
                if sources.get(source) != target:
                    report.error(f"{where} : bind de démarrage manquant : {source}:{target}:ro")
                path = root / source
                if not path.is_file():
                    report.error(f"{where} : fichier {source} absent du dépôt")
                elif fname == "config_db.json":
                    _check_configdb(path, name, report)
        else:
            for source, _ in binds:
                if source in LAB_BINDS:
                    report.error(f"{where} : {source} est réservé aux routeurs {SONIC}")
            for source, _ in binds:
                if source.startswith("configs/") and not (root / source).is_file():
                    report.error(f"{where} : fichier {source} absent du dépôt")

    _check_links(_as_list(topo.get("links"), "topology.links", report), node_kinds, report)

    configs = root / "configs"
    if configs.is_dir():
        for d in sorted(p for p in configs.iterdir() if p.is_dir() and p.name not in node_kinds):
            report.warn(f"configs/{d.name}/ ne correspond à aucun nœud (ignoré)")
    return report


def _check_configdb(path: Path, node: str, report: Report) -> None:
    where = f"configs/{node}/config_db.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report.error(f"{where} : JSON invalide : {exc}")
        return
    if not isinstance(data, dict):
        report.error(f"{where} : objet JSON (tables CONFIG_DB) attendu")
        return
    meta = (data.get("DEVICE_METADATA") or {}).get("localhost") or {}
    if meta.get("hostname") not in (None, node):
        report.warn(f"{where} : DEVICE_METADATA.localhost.hostname vaut {meta.get('hostname')!r}, pas {node!r}")
    if meta.get("switch_type") == "switch":
        report.warn(f"{where} : switch_type « switch » refusé par le modèle YANG (retiré par le bootstrap)")


def _check_links(links: list[Any], node_kinds: dict[str, str], report: Report) -> None:
    seen: dict[str, int] = {}
    for i, link in enumerate(links, 1):
        where = f"lien {i}"
        if not isinstance(link, dict) or set(link) != {"endpoints"}:
            report.error(f"{where} : seule la forme {{endpoints: [\"a:ethN\", \"b:ethM\"]}} est permise")
            continue
        endpoints = link["endpoints"]
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            report.error(f"{where} : deux extrémités attendues")
            continue
        ends = []
        for ep in endpoints:
            m = _ENDPOINT.match(str(ep))
            if not m:
                report.error(f"{where} : extrémité invalide {ep!r} (nœud:ethN)")
                continue
            node, n = m.group(1), int(m.group(2))
            if node not in node_kinds:
                report.error(f"{where} : nœud inconnu {node!r}")
            elif n < 1 or (node_kinds[node] == SONIC and n > MAX_SONIC_ETH):
                limit = f"1 à {MAX_SONIC_ETH}" if node_kinds[node] == SONIC else "1 ou plus"
                report.error(f"{where} : {ep} : interface hors limites ({limit})")
            if ep in seen:
                report.error(f"{where} : {ep} déjà utilisée par le lien {seen[ep]}")
            seen[str(ep)] = i
            ends.append(node)
        if len(ends) == 2 and ends[0] == ends[1]:
            report.error(f"{where} : un lien relie deux nœuds différents")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path.cwd()
    report = validate(root, Settings.from_env())
    for w in report.warnings:
        print(f"⚠ {w}")
    for e in report.errors:
        print(f"✘ {e}")
    if report.ok:
        print(f"✔ {TOPOLOGY_FILE} valide ({len(report.warnings)} avertissement(s))")
        return 0
    print(f"\n✘ {len(report.errors)} erreur(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
