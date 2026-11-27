"""Comparaison de CONFIG_DB (format config_db.json) et calcul du plan d'application.

Modes de push :
  - merge   : ajoute les clés absentes et met à jour les champs fournis ; ne supprime jamais rien
              (équivalent à `config load` / `sonic-cfggen -j ... --write-to-db`).
  - replace : la configuration fournie fait foi : les clés absentes sont supprimées et les entrées
              modifiées sont remplacées entièrement. Les tables « système » (ports, métadonnées,
              buffers/QoS, tables d'exécution...) restent fusionnées : jamais de suppression.

Ce module est pur (aucune E/S) : il est entièrement couvert par les tests unitaires.
"""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import BadRequest

ConfigDB = dict[str, dict[str, dict[str, Any]]]
Mode = Literal["merge", "replace"]

# Tables remplies à l'exécution par les démons : ni sauvegardées, ni comparées.
VOLATILE_TABLES = frozenset({"LOGGER"})
# Champs propres à une instance (ex. MAC dérivée de eth0) : ni sauvegardés, ni comparés.
VOLATILE_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("DEVICE_METADATA", "localhost"): frozenset({"mac"}),
}

PROTECTED_TABLES = frozenset(
    {
        "DEVICE_METADATA",
        "PORT",
        "FEATURE",
        "FLEX_COUNTER_TABLE",
        "VERSIONS",
        "BREAKOUT_CFG",
        "CABLE_LENGTH",
        "CRM",
        "KDUMP",
        "SYSTEM_DEFAULTS",
        "LOGGER",
    }
)
PROTECTED_PREFIXES = (
    "BUFFER_",
    "QUEUE",
    "SCHEDULER",
    "WRED_",
    "COPP_",
    "PFC_",
    "AUTO_TECHSUPPORT",
    "SYSLOG",
    "MAP_",
)
# Tables protégées dont les entrées sont tout de même remplacées champ par champ en mode replace
# (admin_status, mtu, description... reviennent à la conf voulue) ; les champs « matériels »
# absents de la conf voulue sont conservés pour qu'un extrait partiel ne casse pas un port.
FIELD_REPLACE_TABLES: dict[str, frozenset[str]] = {
    "PORT": frozenset({"lanes", "alias", "index", "subport", "speed"}),
}
# Valeurs implicites quand le champ est absent. Les démons SONiC n'appliquent pas de défaut quand un
# champ disparaît de CONFIG_DB : le replace écrit donc la valeur par défaut explicitement, et les
# comparaisons considèrent « absent » et « valeur par défaut » comme équivalents.
FIELD_DEFAULTS: dict[str, dict[str, str]] = {
    "PORT": {"admin_status": "down", "mtu": "9100"},
}


def canonical(table: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Entrée sans les champs égaux à leur valeur implicite (pour comparer)."""
    defaults = FIELD_DEFAULTS.get(table)
    if not defaults:
        return entry
    return {f: v for f, v in entry.items() if defaults.get(f) != v}

# Ordre de création (parents avant enfants). Les suppressions suivent l'ordre inverse.
CREATE_ORDER = [
    "DEVICE_METADATA",
    "PORT",
    "VRF",
    "PORTCHANNEL",
    "VLAN",
    "ACL_TABLE",
    "PORTCHANNEL_MEMBER",
    "VLAN_MEMBER",
    "LOOPBACK_INTERFACE",
    "INTERFACE",
    "PORTCHANNEL_INTERFACE",
    "VLAN_INTERFACE",
    "VLAN_SUB_INTERFACE",
    "ACL_RULE",
    "STATIC_ROUTE",
]
_CREATE_RANK = {t: i for i, t in enumerate(CREATE_ORDER)}


def is_protected(table: str) -> bool:
    return (
        table in PROTECTED_TABLES
        or table.startswith(PROTECTED_PREFIXES)
        or table.endswith("_MAP")
    )


def key_depth(key: str) -> int:
    return key.count("|")


def _create_rank(table: str) -> int:
    return _CREATE_RANK.get(table, len(CREATE_ORDER))


@dataclass
class Op:
    """Opération élémentaire exécutée dans le routeur par remote/apply_configdb.py."""

    op: Literal["set", "mod", "delete"]
    table: str
    key: str
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "table": self.table, "key": self.key, "data": self.data}


@dataclass
class FieldChange:
    name: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "before": self.before, "after": self.after}


@dataclass
class Change:
    action: Literal["add", "modify", "delete"]
    table: str
    key: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    fields: list[FieldChange] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "table": self.table,
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass
class Plan:
    mode: Mode
    ops: list[Op]
    changes: list[Change]
    result: ConfigDB

    @property
    def summary(self) -> dict[str, int]:
        counts = {"add": 0, "modify": 0, "delete": 0}
        for c in self.changes:
            counts[c.action] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "summary": self.summary,
            "changes": [c.to_dict() for c in self.changes],
            "ops": [o.to_dict() for o in self.ops],
        }


def _norm_value(table: str, key: str, fname: str, value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
        return [str(v) for v in value]
    raise BadRequest(f"valeur invalide pour {table}|{key}.{fname} : {value!r}")


def normalize(cfg: Any) -> ConfigDB:
    """Valide la structure {TABLE: {clé: {champ: valeur}}} et normalise les valeurs en chaînes."""
    if not isinstance(cfg, dict):
        raise BadRequest("la configuration doit être un objet JSON {TABLE: {clé: {champ: valeur}}}")
    out: ConfigDB = {}
    for table, keys in cfg.items():
        if not isinstance(table, str) or not table:
            raise BadRequest(f"nom de table invalide : {table!r}")
        if not isinstance(keys, dict):
            raise BadRequest(f"la table {table} doit être un objet {{clé: {{champ: valeur}}}}")
        out_keys: dict[str, dict[str, Any]] = {}
        for key, entry in keys.items():
            if entry is None:
                entry = {}
            if not isinstance(entry, dict):
                raise BadRequest(f"l'entrée {table}|{key} doit être un objet de champs")
            fields = {
                str(f): _norm_value(table, key, f, v)
                for f, v in entry.items()
                if not (f == "NULL" and v == "NULL")
            }
            out_keys[str(key)] = fields
        out[table] = out_keys
    return out


def strip_volatile(cfg: ConfigDB) -> ConfigDB:
    """Copie sans les tables et champs propres à l'instance en cours d'exécution."""
    out = {t: copy.deepcopy(keys) for t, keys in cfg.items() if t not in VOLATILE_TABLES}
    for (table, key), names in VOLATILE_FIELDS.items():
        entry = out.get(table, {}).get(key)
        if entry is not None:
            for name in names:
                entry.pop(name, None)
    return out


def _field_changes(before: dict[str, Any], after: dict[str, Any]) -> list[FieldChange]:
    names = sorted(set(before) | set(after))
    return [
        FieldChange(n, before.get(n), after.get(n)) for n in names if before.get(n) != after.get(n)
    ]


def apply_ops(cfg: ConfigDB, ops: list[Op]) -> ConfigDB:
    """Simule l'application des opérations (aperçu de la configuration résultante)."""
    out = copy.deepcopy(cfg)
    for op in ops:
        table = out.setdefault(op.table, {})
        if op.op == "delete":
            table.pop(op.key, None)
        elif op.op == "set":
            table[op.key] = dict(op.data or {})
        else:  # mod
            table.setdefault(op.key, {}).update(op.data or {})
        if not table:
            out.pop(op.table, None)
    return out


def _order(ops: list[Op]) -> list[Op]:
    deletes = [o for o in ops if o.op == "delete"]
    writes = [o for o in ops if o.op != "delete"]
    # Suppressions : tables dépendantes d'abord (ordre inverse), clés les plus profondes d'abord
    # (ex. INTERFACE|Ethernet0|10.0.0.1/30 avant INTERFACE|Ethernet0).
    deletes.sort(key=lambda o: (-_create_rank(o.table), -key_depth(o.key), o.table, o.key))
    # Créations : parents d'abord, clé de base avant ses sous-clés (IP).
    writes.sort(key=lambda o: (_create_rank(o.table), key_depth(o.key), o.table, o.key))
    return deletes + writes


def plan(running: ConfigDB, desired: ConfigDB, mode: Mode) -> Plan:
    if mode not in ("merge", "replace"):
        raise BadRequest(f"mode inconnu : {mode!r} (merge ou replace)")
    ops: list[Op] = []
    changes: list[Change] = []

    def merge_entry(table: str, key: str, data: dict[str, Any]) -> None:
        cur = running.get(table, {}).get(key)
        if cur is None:
            ops.append(Op("mod", table, key, dict(data)))
            changes.append(Change("add", table, key, None, dict(data), _field_changes({}, data)))
            return
        delta = {f: v for f, v in data.items() if cur.get(f) != v}
        if delta:
            after = {**cur, **delta}
            ops.append(Op("mod", table, key, delta))
            changes.append(Change("modify", table, key, cur, after, _field_changes(cur, after)))

    if mode == "merge":
        for table, keys in desired.items():
            for key, data in keys.items():
                merge_entry(table, key, data)
    else:
        for table in sorted(set(running) | set(desired)):
            cur_keys = running.get(table, {})
            want_keys = desired.get(table, {})
            keep = FIELD_REPLACE_TABLES.get(table)
            if keep is not None:
                for key, data in want_keys.items():
                    cur = cur_keys.get(key)
                    if cur is None:
                        merge_entry(table, key, data)
                        continue
                    target = {**{f: v for f, v in cur.items() if f in keep}, **data}
                    for f, default in FIELD_DEFAULTS.get(table, {}).items():
                        if f in cur and f not in target and cur[f] != default:
                            target[f] = default  # ex. port remis admin down / MTU par défaut
                    if canonical(table, target) != canonical(table, cur):
                        ops.append(Op("set", table, key, target))
                        changes.append(Change("modify", table, key, cur, target, _field_changes(cur, target)))
                continue
            if is_protected(table):
                for key, data in want_keys.items():
                    merge_entry(table, key, data)
                continue
            for key, data in want_keys.items():
                cur = cur_keys.get(key)
                if cur is None:
                    ops.append(Op("set", table, key, dict(data)))
                    changes.append(Change("add", table, key, None, dict(data), _field_changes({}, data)))
                elif cur != data:
                    ops.append(Op("set", table, key, dict(data)))
                    changes.append(Change("modify", table, key, cur, dict(data), _field_changes(cur, data)))
            for key, cur in cur_keys.items():
                if key not in want_keys:
                    ops.append(Op("delete", table, key))
                    changes.append(Change("delete", table, key, cur, None, _field_changes(cur, {})))

    ordered = _order(ops)
    changes.sort(key=lambda c: (c.table, c.key))
    return Plan(mode=mode, ops=ordered, changes=changes, result=apply_ops(running, ordered))


def diff(before: ConfigDB, after: ConfigDB) -> list[Change]:
    """Différences structurelles entre deux configurations (hors éléments volatils)."""
    a = strip_volatile(before)
    b = strip_volatile(after)
    changes: list[Change] = []
    for table in sorted(set(a) | set(b)):
        ka = a.get(table, {})
        kb = b.get(table, {})
        for key in sorted(set(ka) | set(kb)):
            ea, eb = ka.get(key), kb.get(key)
            if ea is None and eb is not None:
                changes.append(Change("add", table, key, None, eb, _field_changes({}, eb)))
            elif eb is None and ea is not None:
                changes.append(Change("delete", table, key, ea, None, _field_changes(ea, {})))
            elif ea is not None and eb is not None:
                ca, cb = canonical(table, ea), canonical(table, eb)
                if ca != cb:
                    changes.append(Change("modify", table, key, ea, eb, _field_changes(ca, cb)))
    return changes


def preserve_representation(new: ConfigDB, reference: ConfigDB) -> ConfigDB:
    """Reprend l'entrée de la référence quand elle est équivalente à la nouvelle.

    Utilisé à la sauvegarde : un champ explicite égal à sa valeur par défaut (ex. mtu 9100 écrit par
    un replace) ne doit pas modifier un fichier de démarrage qui ne le mentionnait pas, et inversement.
    """
    out: ConfigDB = {}
    for table, keys in new.items():
        ref = reference.get(table, {})
        out[table] = {
            key: ref[key] if key in ref and canonical(table, ref[key]) == canonical(table, entry) else entry
            for key, entry in keys.items()
        }
    return out


def dump(cfg: ConfigDB) -> str:
    """Rendu canonique d'une configuration (celui de `config save`)."""
    return json.dumps(cfg, indent=4, sort_keys=True, ensure_ascii=False) + "\n"


def unified_diff(before: ConfigDB, after: ConfigDB, *, before_name: str, after_name: str) -> str:
    """Diff texte (format unifié) entre deux configurations, hors éléments volatils."""
    return "".join(
        difflib.unified_diff(
            dump(strip_volatile(before)).splitlines(keepends=True),
            dump(strip_volatile(after)).splitlines(keepends=True),
            fromfile=before_name,
            tofile=after_name,
            n=2,
        )
    )
