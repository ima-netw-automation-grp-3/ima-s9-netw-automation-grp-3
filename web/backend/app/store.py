"""Fichiers de configuration du lab sur l'hôte.

  - conf de démarrage : lab/configs/<nœud>/{config_db.json, frr.conf}
    (montées dans les routeurs au déploiement, voir sonic-lab.clab.yml) ;
  - snapshots         : lab/backups/<nœud>/<horodatage>[-label]/{config_db.json, frr.conf, meta.json}.

Les fichiers écrits reçoivent le propriétaire du dossier du lab : l'interface tourne en root dans son
conteneur, mais les fichiers du dépôt doivent rester éditables par l'utilisateur de l'hôte.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import BadRequest, NotFound

DOMAINS: dict[str, str] = {"configdb": "config_db.json", "frr": "frr.conf"}

_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SNAP_RE = re.compile(r"^\d{8}-\d{6}(-[a-z0-9][a-z0-9-]*)?$")


def check_domain(domain: str) -> str:
    if domain not in DOMAINS:
        raise BadRequest(f"domaine inconnu : {domain!r} (configdb ou frr)")
    return domain


def _check_node(node: str) -> str:
    if not _NODE_RE.match(node):
        raise BadRequest(f"nom de nœud invalide : {node!r}")
    return node


def _slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return slug[:40].strip("-")


class Store:
    def __init__(self, configs_dir: Path, backups_dir: Path, owner_ref: Path | None = None) -> None:
        self.configs_dir = configs_dir
        self.backups_dir = backups_dir
        self._owner_ref = owner_ref or configs_dir.parent

    # -- propriété des fichiers -------------------------------------------------------------
    def _owner(self) -> tuple[int, int] | None:
        try:
            st = self._owner_ref.stat()
        except OSError:
            return None
        return st.st_uid, st.st_gid

    def _chown(self, path: Path) -> None:
        owner = self._owner()
        if owner is None:
            return
        try:
            os.chown(path, *owner)
        except PermissionError:
            pass  # pas root (exécution locale) : sans importance

    def _mkdirs(self, path: Path) -> None:
        missing: list[Path] = []
        p = path
        while not p.exists():
            missing.append(p)
            p = p.parent
        path.mkdir(parents=True, exist_ok=True)
        for d in reversed(missing):
            self._chown(d)

    def _write(self, path: Path, content: str) -> None:
        self._mkdirs(path.parent)
        # écriture en place (pas de rename) : les bind mounts de fichiers voient le nouveau contenu
        with path.open("w", encoding="utf-8") as fh:
            fh.write(content)
        self._chown(path)

    # -- conf de démarrage ------------------------------------------------------------------
    def startup_path(self, node: str, domain: str) -> Path:
        return self.configs_dir / _check_node(node) / DOMAINS[check_domain(domain)]

    def read_startup(self, node: str, domain: str) -> str | None:
        path = self.startup_path(node, domain)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_startup(self, node: str, domain: str, content: str) -> Path:
        path = self.startup_path(node, domain)
        self._write(path, content)
        return path

    # -- snapshots ---------------------------------------------------------------------------
    def _snap_dir(self, node: str, snap_id: str) -> Path:
        if not _SNAP_RE.match(snap_id):
            raise BadRequest(f"identifiant de snapshot invalide : {snap_id!r}")
        return self.backups_dir / _check_node(node) / snap_id

    def list_snapshots(self, node: str) -> list[dict[str, Any]]:
        base = self.backups_dir / _check_node(node)
        if not base.is_dir():
            return []
        out = []
        for d in sorted(base.iterdir(), reverse=True):
            if d.is_dir() and _SNAP_RE.match(d.name):
                out.append(self._meta(node, d))
        return out

    def _meta(self, node: str, d: Path) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        meta_file = d / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        meta.update(
            {
                "id": d.name,
                "node": node,
                "domains": [dom for dom, fname in DOMAINS.items() if (d / fname).exists()],
            }
        )
        return meta

    def get_snapshot(self, node: str, snap_id: str) -> dict[str, Any]:
        d = self._snap_dir(node, snap_id)
        if not d.is_dir():
            raise NotFound(f"snapshot {snap_id} introuvable pour {node}")
        return self._meta(node, d)

    def create_snapshot(
        self, node: str, contents: dict[str, str], *, label: str = "", source: str = "running"
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        snap_id = now.strftime("%Y%m%d-%H%M%S")
        slug = _slug(label) if label else ""
        if slug:
            snap_id += f"-{slug}"
        d = self._snap_dir(node, snap_id)
        suffix = 1
        while d.exists():  # deux snapshots dans la même seconde
            suffix += 1
            d = self._snap_dir(node, f"{snap_id}-{suffix}" if slug else f"{snap_id}-n{suffix}")
        for domain, content in contents.items():
            self._write(d / DOMAINS[check_domain(domain)], content)
        meta = {"label": label, "created": now.isoformat(timespec="seconds"), "source": source}
        self._write(d / "meta.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        return self._meta(node, d)

    def read_snapshot(self, node: str, snap_id: str, domain: str) -> str:
        path = self._snap_dir(node, snap_id) / DOMAINS[check_domain(domain)]
        if not path.exists():
            raise NotFound(f"le snapshot {snap_id} ne contient pas de conf {domain}")
        return path.read_text(encoding="utf-8")

    def delete_snapshot(self, node: str, snap_id: str) -> None:
        d = self._snap_dir(node, snap_id)
        if not d.is_dir():
            raise NotFound(f"snapshot {snap_id} introuvable pour {node}")
        shutil.rmtree(d)
