"""Module YANG sonic-lab (yang/sonic-lab.yang) : schéma et conversions. Module pur.

  /restconf/data/sonic-lab:frr/running-config         configuration FRR (texte vtysh)
  /restconf/data/sonic-lab:frr/state/bgp-neighbor     état des sessions BGP (config false)
  /restconf/operations/sonic-lab:ping                 ping depuis le routeur
  /restconf/operations/sonic-lab:preview              aperçu d'une écriture, sans l'appliquer
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .datatree import CONTAINER, LEAF, LIST, DictSchema, SchemaNode
from .rfc8040 import bad_request

MODULE = "sonic-lab"
NAMESPACE = "urn:sonic-lab:yang:sonic-lab"
REVISION = "2026-11-20"
YANG_FILE = Path(__file__).resolve().parent / "yang" / f"{MODULE}.yang"

FRR = f"{MODULE}:frr"
RUNNING = (FRR, "running-config")
STATE = (FRR, "state")
NEIGHBOR = (FRR, "state", "bgp-neighbor")
NEIGHBOR_LEAVES = ("address", "remote-as", "state", "description", "uptime", "prefixes-received")

SCHEMA = DictSchema(
    {
        (FRR,): SchemaNode(CONTAINER),
        RUNNING: SchemaNode(LEAF),
        STATE: SchemaNode(CONTAINER, config=False),
        NEIGHBOR: SchemaNode(LIST, keys=("address",), config=False),
        **{NEIGHBOR + (leaf,): SchemaNode(LEAF, config=False) for leaf in NEIGHBOR_LEAVES},
    }
)

OPERATIONS = (f"{MODULE}:ping", f"{MODULE}:preview")
PREVIEW_METHODS = ("PUT", "PATCH", "POST", "DELETE")

# Cible et source de ping : adresse, nom d'hôte ou interface ; jamais d'option (pas de « - » en tête).
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_/-]{0,62}$")


def bgp_neighbors(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """`vtysh -c "show bgp summary json"` → instances de la liste bgp-neighbor."""
    peers: dict[str, dict[str, Any]] = {}
    for family in summary.values():
        if not isinstance(family, dict):
            continue
        for address, p in (family.get("peers") or {}).items():
            entry = peers.setdefault(address, {"address": address})
            remote_as = p.get("remoteAs")
            if isinstance(remote_as, int) and not isinstance(remote_as, bool):
                entry["remote-as"] = remote_as
            if p.get("state"):
                entry["state"] = str(p["state"])
            if p.get("desc"):
                entry["description"] = str(p["desc"])
            if p.get("peerUptime"):
                entry["uptime"] = str(p["peerUptime"])
            if isinstance(p.get("pfxRcd"), int):
                entry["prefixes-received"] = entry.get("prefixes-received", 0) + p["pfxRcd"]
    return [peers[a] for a in sorted(peers)]


def rpc_input(body: Any, operation: str) -> dict[str, Any]:
    """Corps d'une opération : {"sonic-lab:input": {…}} (§ 3.6.1) ; corps absent = entrée vide."""
    if body is None:
        return {}
    if not isinstance(body, dict) or len(body) != 1:
        raise bad_request(f"{operation} : le corps doit être {{\"{MODULE}:input\": {{…}}}}", tag="malformed-message")
    ((name, value),) = body.items()
    if name not in (f"{MODULE}:input", "input") or not isinstance(value, dict):
        raise bad_request(f"{operation} : le corps doit être {{\"{MODULE}:input\": {{…}}}}", tag="malformed-message")
    return value


def ping_input(inp: dict[str, Any]) -> tuple[str, str | None, int]:
    unknown = set(inp) - {"destination", "source", "count"}
    if unknown:
        raise bad_request(f"ping : paramètre(s) inconnu(s) : {', '.join(sorted(unknown))}", tag="unknown-element")
    destination = inp.get("destination")
    if not isinstance(destination, str) or not _TARGET_RE.match(destination):
        raise bad_request("ping : destination (adresse IP ou nom d'hôte) obligatoire")
    source = inp.get("source")
    if source is not None and (not isinstance(source, str) or not _SOURCE_RE.match(source)):
        raise bad_request("ping : source invalide (adresse IP ou nom d'interface)")
    count = inp.get("count", 3)
    if isinstance(count, str) and count.isdigit():
        count = int(count)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
        raise bad_request("ping : count doit être un entier de 1 à 20")
    return destination, source, count


def ping_output(summary: dict[str, Any], output: str) -> dict[str, Any]:
    """Résumé de parsers.parse_ping → sortie de l'opération (decimal64 en texte, RFC 7951)."""
    out: dict[str, Any] = {
        "transmitted": int(summary.get("transmitted") or 0),
        "received": int(summary.get("received") or 0),
        "success": bool(summary.get("ok")),
    }
    if summary.get("loss") is not None:
        out["loss-percent"] = f"{float(summary['loss']):.1f}"
    for name in ("min", "avg", "max"):
        value = summary.get(f"rtt_{name}")
        if value is not None:
            out[f"rtt-{name}"] = f"{float(value):.3f}"
    out["output"] = output
    return out


def preview_input(inp: dict[str, Any]) -> tuple[str, str, Any]:
    unknown = set(inp) - {"path", "method", "data"}
    if unknown:
        raise bad_request(f"preview : paramètre(s) inconnu(s) : {', '.join(sorted(unknown))}", tag="unknown-element")
    path, method = inp.get("path"), inp.get("method")
    if not isinstance(path, str) or not path.strip():
        raise bad_request("preview : path (chemin RESTCONF de la ressource) obligatoire")
    if method not in PREVIEW_METHODS:
        raise bad_request(f"preview : method doit valoir {', '.join(PREVIEW_METHODS)}")
    path = path.strip()
    for prefix in ("/restconf/data/", "restconf/data/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    data = inp.get("data")
    if method != "DELETE" and data is None:
        raise bad_request(f"preview : data (corps de la requête {method}) obligatoire")
    return path, method, data


def module_entry() -> dict[str, Any]:
    return {"name": MODULE, "revision": REVISION, "namespace": NAMESPACE, "implemented": True}
