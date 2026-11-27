"""Différence entre deux CONFIG_DB → JSON Patch (RFC 6902) pour GCU (config apply-patch). Module pur.

Format CONFIG_DB de GCU (sonic-cfggen -d --print-data) : {TABLE: {clé: {champ: valeur}}}, une clé à
plusieurs parties s'écrivant « a|b », une leaf-list étant une liste JSON.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

ConfigDB = dict[str, dict[str, dict[str, Any]]]


def escape(token: str) -> str:
    """Échappement d'un élément de chemin JSON Pointer (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def diff(before: ConfigDB, after: ConfigDB) -> list[dict[str, Any]]:
    """Opérations add/remove/replace (par table, clé, puis champ) qui transforment before en after."""
    ops: list[dict[str, Any]] = []
    for table in sorted(set(before) | set(after)):
        b = before.get(table) or {}
        a = after.get(table) or {}
        if b == a:
            continue
        tpath = f"/{escape(table)}"
        if not b:
            ops.append({"op": "add", "path": tpath, "value": a})
            continue
        if not a:
            ops.append({"op": "remove", "path": tpath})
            continue
        for key in sorted(set(b) | set(a)):
            kb, ka = b.get(key), a.get(key)
            if kb == ka:
                continue
            kpath = f"{tpath}/{escape(key)}"
            if kb is None:
                ops.append({"op": "add", "path": kpath, "value": ka})
            elif ka is None:
                ops.append({"op": "remove", "path": kpath})
            else:
                for field in sorted(set(kb) | set(ka)):
                    fb, fa = kb.get(field), ka.get(field)
                    if fb == fa:
                        continue
                    fpath = f"{kpath}/{escape(field)}"
                    if fb is None:
                        ops.append({"op": "add", "path": fpath, "value": fa})
                    elif fa is None:
                        ops.append({"op": "remove", "path": fpath})
                    else:
                        ops.append({"op": "replace", "path": fpath, "value": fa})
    return ops


def dump(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def unified_diff(before: Any, after: Any, *, before_name: str = "running", after_name: str = "après") -> str:
    """Diff lisible de deux documents JSON (clés triées)."""
    return "".join(
        difflib.unified_diff(
            dump(before).splitlines(keepends=True),
            dump(after).splitlines(keepends=True),
            fromfile=before_name,
            tofile=after_name,
        )
    )
