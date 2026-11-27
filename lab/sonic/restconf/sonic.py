"""CONFIG_DB du routeur à travers les modèles YANG de SONiC : libyang (lecture, validation), GCU (écriture).

S'exécute dans le routeur, avec les bibliothèques de l'image : sonic_yang, libyang,
generic_config_updater. Les données circulent en JSON RFC 7951 canonique (sortie de libyang) ; la
conversion vers la CONFIG_DB et le JSON Patch passent par les fonctions de GCU.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import libyang
import sonic_yang
from generic_config_updater.gu_common import ConfigWrapper

from .datatree import CONTAINER, LEAF, LEAF_LIST, LIST, SchemaNode, strip_module
from .messages import gcu_message, libyang_message
from .store import InvalidData, OperationFailed

YANG_DIR = "/usr/local/yang-models"
APPLY_TIMEOUT = 180  # s : un patch GCU prend 1 à 3 s dans le lab

log = logging.getLogger("restconf")
LibyangError = getattr(libyang, "LibyangError", Exception)


class SonicSchema:
    """Schéma des modules sonic-* tiré du contexte libyang (type des nœuds, clés des listes)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._cache: dict[tuple[str, ...], SchemaNode | None] = {}

    def node(self, path: tuple[str, ...]) -> SchemaNode | None:
        path = tuple(path)
        if path not in self._cache:
            self._cache[path] = self._lookup(path)
        return self._cache[path]

    def _lookup(self, path: tuple[str, ...]) -> SchemaNode | None:
        try:
            snode = next(iter(self.ctx.find_path("/" + "/".join(path))), None)
        except Exception:  # LibyangError « cannot find path » : nœud inconnu
            return None
        if snode is None:
            return None
        kind = snode.keyword()
        if kind not in (CONTAINER, LIST, LEAF, LEAF_LIST):
            return None
        keys = tuple(k.name() for k in snode.keys()) if kind == LIST else ()
        return SchemaNode(kind, keys, not snode.config_false())


class SonicConfigDB:
    def __init__(self, yang_dir: str = YANG_DIR) -> None:
        self.sy = sonic_yang.SonicYang(yang_dir, print_log_enabled=False)
        self.sy.loadYangModel()
        self.ctx = self.sy.ctx
        self.schema = SonicSchema(self.ctx)
        self.wrapper = ConfigWrapper()
        self._modules = self._list_modules()

    def _list_modules(self) -> list[dict[str, Any]]:
        out = []
        for m in self.ctx:
            revisions = [r.date() for r in m.revisions()]
            try:
                namespace = libyang.util.c2str(m.cdata.ns)
            except Exception:
                namespace = ""
            filepath = m.filepath()
            out.append(
                {
                    "name": m.name(),
                    "revision": revisions[0] if revisions else "",
                    "namespace": namespace or "",
                    "implemented": bool(m.implemented()),
                    "downloadable": bool(filepath),
                    "file": filepath,
                }
            )
        return out

    def modules(self) -> list[dict[str, Any]]:
        return [dict(m) for m in self._modules]

    def schema_file(self, name: str, revision: str = "") -> Path | None:
        for m in self._modules:
            if m["name"] == name and m["file"] and revision in ("", m["revision"]):
                return Path(m["file"])
        return None

    def read(self) -> dict[str, Any]:
        cfg = self.wrapper.get_config_db_as_json()
        xlate = self.wrapper.convert_config_db_to_sonic_yang(cfg)
        try:
            dnode = self._parse(xlate)
        except LibyangError as exc:
            # CONFIG_DB non conforme (ex. écrite sans validation) : lecture quand même, en forme GCU
            message, path = libyang_message(str(exc))
            log.warning("CONFIG_DB non conforme aux modèles YANG : %s%s", message, f" ({path})" if path else "")
            return {top: strip_module(value, top.split(":", 1)[0]) for top, value in xlate.items()}
        if dnode is None:
            return {}
        try:
            text = dnode.print_mem("json", with_siblings=True, pretty=False)
        finally:
            dnode.free()
        return json.loads(text) if text else {}

    def _parse(self, tree: dict[str, Any]) -> Any:
        # mêmes options que sonic_yang.loadData : validation complète (leafref, must, mandatory)
        return self.ctx.parse_data_mem(json.dumps(tree), "json", no_state=True, strict=True, json_string_datatypes=True)

    def validate(self, tree: dict[str, Any]) -> None:
        try:
            dnode = self._parse(tree)
        except LibyangError as exc:
            message, path = libyang_message(str(exc))
            raise InvalidData(message, path) from None
        if dnode is not None:
            dnode.free()

    def to_configdb(self, tree: dict[str, Any]) -> dict[str, Any]:
        if not tree:
            return {}
        return self.wrapper.convert_sonic_yang_to_config_db(tree)

    def apply(self, ops: list[dict[str, Any]], *, dry_run: bool = False) -> None:
        fd, path = tempfile.mkstemp(prefix="restconf-patch-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(ops, f)
            cmd = ["config", "apply-patch", *(["--dry-run"] if dry_run else []), path]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=APPLY_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise OperationFailed(f"config apply-patch : pas de réponse en {APPLY_TIMEOUT} s") from None
        finally:
            os.unlink(path)
        if res.returncode != 0:
            raise InvalidData(f"refusé par GCU (config apply-patch) : {gcu_message(res.stdout + chr(10) + res.stderr)}")
