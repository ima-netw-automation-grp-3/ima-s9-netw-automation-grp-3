"""Arbres de données YANG en JSON (RFC 7951) : résolution des chemins RESTCONF, lecture et édition.

Module pur (aucune E/S). Un arbre est un dict dont les membres de premier niveau sont qualifiés par
leur module (« sonic-port:sonic-port ») ; les membres enfants du même module ne le sont pas. Une liste
YANG est une liste JSON de dicts (ses instances), une leaf-list une liste JSON de valeurs.

Le schéma (type de chaque nœud, clés des listes, config true/false) vient d'un objet Schema : libyang
dans le routeur (sonic.SonicSchema), un dict pour le module sonic-lab et pour les tests (DictSchema).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol

from .rfc8040 import RestconfError, Segment, bad_request, data_exists, format_path, not_found

CONTAINER, LIST, LEAF, LEAF_LIST = "container", "list", "leaf", "leaf-list"


@dataclass(frozen=True)
class SchemaNode:
    kind: str  # container | list | leaf | leaf-list
    keys: tuple[str, ...] = ()  # listes : noms des clés, dans l'ordre du modèle
    config: bool = True


class Schema(Protocol):
    def node(self, path: tuple[str, ...]) -> SchemaNode | None:
        """path : ("module:haut", "enfant", …) sans clés ; None si le nœud n'existe pas."""


class DictSchema:
    """Schéma décrit par un dict {chemin: SchemaNode}."""

    def __init__(self, nodes: dict[tuple[str, ...], SchemaNode]) -> None:
        self.nodes = dict(nodes)

    def node(self, path: tuple[str, ...]) -> SchemaNode | None:
        return self.nodes.get(tuple(path))


@dataclass(frozen=True)
class Step:
    name: str  # membre JSON : qualifié pour le premier niveau, simple ensuite
    node: SchemaNode
    keys: tuple[str, ...] | None  # liste : valeurs des clés ; leaf-list : (valeur,) ; sinon None


@dataclass(frozen=True)
class Target:
    """Nœud visé par un chemin RESTCONF."""

    module: str
    steps: tuple[Step, ...]
    segments: tuple[Segment, ...]

    @property
    def schema_path(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps)

    @property
    def last(self) -> Step:
        return self.steps[-1]

    @property
    def top(self) -> str:
        """Conteneur de premier niveau du module (« sonic-port:sonic-port »)."""
        return self.steps[0].name

    @property
    def member(self) -> str:
        """Nom qualifié du nœud visé, tel qu'il apparaît dans les corps de requête et de réponse."""
        name = self.last.name
        return name if ":" in name else f"{self.module}:{name}"

    @property
    def uri(self) -> str:
        return format_path(list(self.segments))

    @property
    def is_instance(self) -> bool:
        """Instance de liste (LISTE=clés) ou valeur de leaf-list (FEUILLES=valeur)."""
        return self.last.node.kind in (LIST, LEAF_LIST) and self.last.keys is not None


def scalar(value: Any) -> str | None:
    """Valeur d'une feuille sous forme de texte, pour la comparer à une clé d'URI."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == [None]:  # type empty
        return ""
    return str(value)


def qualify(name: str, module: str) -> str:
    return name if ":" in name else f"{module}:{name}"


def strip_module(value: Any, module: str) -> Any:
    """Retire le préfixe « module: » des membres de ce module (forme GCU, ou client trop zélé)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            mod, colon, local = k.partition(":")
            out[local if colon and mod == module else k] = strip_module(v, module)
        return out
    if isinstance(value, list):
        return [strip_module(v, module) for v in value]
    return value


def resolve(schema: Schema, segments: list[Segment]) -> Target:
    """Chemin RESTCONF → Target, vérifié contre le schéma (RFC 8040 § 3.5.3)."""
    if not segments:
        raise ValueError("chemin vide")
    first = segments[0]
    if first.module is None:
        raise bad_request("le premier nœud du chemin doit être qualifié par son module (module:nœud)")
    module = first.module
    steps: list[Step] = []
    path: tuple[str, ...] = ()
    uri = format_path(segments)
    for i, seg in enumerate(segments):
        if seg.module not in (None, module):
            raise bad_request(f"{seg.module}:{seg.name} : nœud d'un autre module que {module}", tag="unknown-element", path=uri)
        name = f"{module}:{seg.name}" if i == 0 else seg.name
        path = path + (name,)
        node = schema.node(path)
        if node is None:
            where = format_path(segments[: i + 1])
            raise bad_request(f"nœud inconnu dans le modèle : {where}", tag="unknown-element", path=uri)
        last = i == len(segments) - 1
        if node.kind == LIST:
            if seg.keys is None and not last:
                raise bad_request(f"{seg.name} est une liste : précisez l'instance ({seg.name}={','.join(node.keys)})", path=uri)
            if seg.keys is not None and len(seg.keys) != len(node.keys):
                raise bad_request(
                    f"la liste {seg.name} attend {len(node.keys)} valeur(s) de clé : {', '.join(node.keys)}", path=uri
                )
        elif node.kind == LEAF_LIST:
            if not last:
                raise bad_request(f"{seg.name} est une leaf-list : rien ne peut la suivre", path=uri)
            if seg.keys is not None and len(seg.keys) != 1:
                raise bad_request(f"{seg.name} : une seule valeur attendue après « = »", path=uri)
        else:
            if seg.keys is not None:
                raise bad_request(f"{seg.name} n'est pas une liste : « = » interdit", path=uri)
            if node.kind == LEAF and not last:
                raise bad_request(f"{seg.name} est une feuille : rien ne peut la suivre", path=uri)
        steps.append(Step(name, node, seg.keys))
    return Target(module, tuple(steps), tuple(segments))


# --- lecture ---------------------------------------------------------------------------------


class Missing(Exception):
    """Le nœud visé n'existe pas dans l'arbre."""


def _index(entries: Any, key_names: tuple[str, ...], key_values: tuple[str, ...]) -> int | None:
    if not isinstance(entries, list):
        return None
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and all(scalar(entry.get(k)) == v for k, v in zip(key_names, key_values)):
            return i
    return None


def _value_index(values: Any, value: str) -> int | None:
    if not isinstance(values, list):
        return None
    for i, v in enumerate(values):
        if scalar(v) == value:
            return i
    return None


def _parent(tree: dict[str, Any], target: Target, *, create: bool) -> dict[str, Any]:
    """Dict qui contient (ou contiendra) le membre visé ; crée les ancêtres si create."""
    node: Any = tree
    for step in target.steps[:-1]:
        if not isinstance(node, dict):
            raise Missing
        if step.name not in node:
            if not create:
                raise Missing
            node[step.name] = [] if step.node.kind == LIST else {}
        child = node[step.name]
        if step.node.kind == LIST:
            assert step.keys is not None  # garanti par resolve()
            idx = _index(child, step.node.keys, step.keys)
            if idx is None:
                if not create or not isinstance(child, list):
                    raise Missing
                child.append(dict(zip(step.node.keys, step.keys)))
                idx = len(child) - 1
            node = child[idx]
        else:
            node = child
    if not isinstance(node, dict):
        raise Missing
    return node


def find(tree: dict[str, Any], target: Target) -> Any:
    """Valeur du nœud visé : dict (conteneur, instance), liste (liste entière), valeur de feuille."""
    parent = _parent(tree, target, create=False)
    step = target.last
    if step.name not in parent:
        raise Missing
    value = parent[step.name]
    if step.keys is not None:
        if step.node.kind == LIST:
            idx = _index(value, step.node.keys, step.keys)
        else:
            idx = _value_index(value, step.keys[0])
        if idx is None:
            raise Missing
        return value[idx]
    return value


def exists(tree: dict[str, Any], target: Target) -> bool:
    try:
        find(tree, target)
    except Missing:
        return False
    return True


def wrap(target: Target, value: Any) -> dict[str, Any]:
    """Corps de réponse : le nœud visé, sous son nom qualifié ; une instance de liste dans une liste."""
    if target.is_instance:
        return {target.member: [value]}
    return {target.member: value}


# --- corps de requête ------------------------------------------------------------------------


def unwrap(target: Target, body: Any) -> Any:
    """Corps d'un PUT ou d'un PATCH : {"module:nœud": valeur}, où le nœud est celui de l'URI."""
    if not isinstance(body, dict) or len(body) != 1:
        raise bad_request(f"le corps doit contenir exactement le nœud visé : {target.member}", tag="malformed-message")
    ((name, value),) = body.items()
    if qualify(name, target.module) != target.member:
        raise bad_request(f"le corps doit contenir le nœud {target.member}, pas {name}", tag="malformed-message")
    value = strip_module(value, target.module)
    step = target.last
    if step.node.kind == LIST:
        if step.keys is None:
            raise bad_request(f"{target.uri} : précisez l'instance de la liste dans l'URI (LISTE=clé)")
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise bad_request(f"{target.member} : une liste contenant exactement une instance est attendue", tag="malformed-message")
        return _with_keys(value[0], step.node.keys, step.keys, target)
    if step.node.kind == LEAF_LIST:
        if step.keys is None:
            if not isinstance(value, list):
                raise bad_request(f"{target.member} : une liste de valeurs est attendue", tag="malformed-message")
            return value
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if scalar(value) != step.keys[0]:
            raise bad_request(f"{target.member} : la valeur du corps diffère de celle de l'URI")
        return value
    if step.node.kind == CONTAINER and not isinstance(value, dict):
        raise bad_request(f"{target.member} est un conteneur : un objet JSON est attendu", tag="malformed-message")
    if step.node.kind == LEAF and isinstance(value, (dict, list)) and value != [None]:
        raise bad_request(f"{target.member} est une feuille : une valeur simple est attendue", tag="malformed-message")
    return value


def _with_keys(entry: dict[str, Any], key_names: tuple[str, ...], key_values: tuple[str, ...], target: Target) -> dict[str, Any]:
    entry = dict(entry)
    for k, v in zip(key_names, key_values):
        if k in entry and scalar(entry[k]) != v:
            raise bad_request(f"la clé {k} du corps ({scalar(entry[k])}) diffère de celle de l'URI ({v})", path=target.uri)
        entry.setdefault(k, v)
    return entry


# --- édition (sur une copie de l'arbre : l'appelant valide puis applique) ------------------


def put(tree: dict[str, Any], target: Target, value: Any) -> bool:
    """Crée ou remplace le nœud visé (RFC 8040 § 4.5) ; True s'il a été créé."""
    parent = _parent(tree, target, create=True)
    step = target.last
    if step.node.kind == LIST:
        if step.keys is None:
            raise bad_request(f"{target.uri} : PUT sur une liste entière impossible, visez une instance")
        entries = parent.setdefault(step.name, [])
        idx = _index(entries, step.node.keys, step.keys)
        if idx is None:
            entries.append(copy.deepcopy(value))
            return True
        entries[idx] = copy.deepcopy(value)
        return False
    if step.node.kind == LEAF_LIST and step.keys is not None:
        values = parent.setdefault(step.name, [])
        if _value_index(values, step.keys[0]) is not None:
            return False
        values.append(value)
        return True
    created = step.name not in parent
    parent[step.name] = copy.deepcopy(value)
    return created


def merge(schema: Schema, tree: dict[str, Any], target: Target, value: Any) -> None:
    """PATCH simple (RFC 8040 § 4.6.1) : fusion dans le nœud visé, qui doit exister.

    Un conteneur sans présence existe toujours (on fusionne en le créant au besoin) ; une instance de
    liste ou une feuille absente donne 404, la RFC interdisant à PATCH de la créer.
    """
    kind = target.last.node.kind
    try:
        current = find(tree, target)
    except Missing:
        if kind != CONTAINER:
            raise not_found(f"{target.uri} n'existe pas (PATCH ne crée pas de ressource : utilisez PUT ou POST)") from None
        put(tree, target, value)
        return
    if kind == CONTAINER or (kind == LIST and target.last.keys is not None):
        _merge_into(schema, target.schema_path, current, value)
    elif kind == LEAF_LIST and target.last.keys is None:
        for v in value:
            if _value_index(current, scalar(v) or "") is None:
                current.append(v)
    elif kind == LEAF:
        _parent(tree, target, create=False)[target.last.name] = copy.deepcopy(value)
    # instance de leaf-list : déjà présente, rien à fusionner


def _merge_into(schema: Schema, path: tuple[str, ...], dst: dict[str, Any], src: dict[str, Any]) -> None:
    for name, value in src.items():
        child_path = path + (name,)
        node = schema.node(child_path)
        current = dst.get(name)
        if node is None or current is None:
            dst[name] = copy.deepcopy(value)  # nœud inconnu : la validation YANG le refusera
        elif node.kind == CONTAINER and isinstance(value, dict) and isinstance(current, dict):
            _merge_into(schema, child_path, current, value)
        elif node.kind == LIST and isinstance(value, list) and isinstance(current, list):
            for entry in value:
                if not isinstance(entry, dict):
                    current.append(entry)
                    continue
                keys = tuple(scalar(entry.get(k)) or "" for k in node.keys)
                idx = _index(current, node.keys, keys)
                if idx is None:
                    current.append(copy.deepcopy(entry))
                else:
                    _merge_into(schema, child_path, current[idx], entry)
        elif node.kind == LEAF_LIST and isinstance(value, list) and isinstance(current, list):
            for v in value:
                if _value_index(current, scalar(v) or "") is None:
                    current.append(v)
        else:
            dst[name] = copy.deepcopy(value)


def create(schema: Schema, tree: dict[str, Any], parent_segments: list[Segment], body: Any) -> Target:
    """POST sur une ressource (ou sur le datastore) : crée l'enfant décrit par le corps (§ 4.4.1).

    Renvoie la cible créée (pour l'en-tête Location) ; 409 data-exists si elle existe déjà.
    """
    if not isinstance(body, dict) or len(body) != 1:
        raise bad_request("le corps doit contenir exactement la ressource à créer", tag="malformed-message")
    ((name, value),) = body.items()
    module, colon, local = name.partition(":")
    if parent_segments:
        parent_module = parent_segments[0].module
        if colon and module != parent_module:
            raise bad_request(f"{name} : nœud d'un autre module que {parent_module}", tag="unknown-element")
        child = Segment(None, local if colon else name)
    else:
        if not colon:
            raise bad_request("POST sur le datastore : le nœud doit être qualifié par son module (module:nœud)")
        child = Segment(module, local)
        parent_module = module
    value = strip_module(value, parent_module or "")
    probe = resolve(schema, [*parent_segments, child])
    node = probe.last.node
    if node.kind == LIST:
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise bad_request(f"{name} : une liste contenant exactement une instance est attendue", tag="malformed-message")
        entry = value[0]
        missing = [k for k in node.keys if k not in entry]
        if missing:
            raise bad_request(f"{name} : clé(s) manquante(s) : {', '.join(missing)}")
        keys = tuple(scalar(entry[k]) or "" for k in node.keys)
        target = resolve(schema, [*parent_segments, Segment(child.module, child.name, keys)])
        value = entry
    elif node.kind == LEAF_LIST:
        values = value if isinstance(value, list) else [value]
        if len(values) != 1:
            raise bad_request(f"{name} : une seule valeur à créer", tag="malformed-message")
        target = resolve(schema, [*parent_segments, Segment(child.module, child.name, (scalar(values[0]) or "",))])
        value = values[0]
    else:
        target = probe
        if node.kind == CONTAINER and not isinstance(value, dict):
            raise bad_request(f"{name} est un conteneur : un objet JSON est attendu", tag="malformed-message")
    if exists(tree, target):
        raise data_exists(f"{target.uri} existe déjà", path=target.uri)
    put(tree, target, value)
    return target


def delete(tree: dict[str, Any], target: Target) -> None:
    """DELETE (§ 4.7) : 404 si le nœud n'existe pas."""
    try:
        parent = _parent(tree, target, create=False)
    except Missing:
        raise not_found(f"{target.uri} n'existe pas") from None
    step = target.last
    if step.name not in parent:
        raise not_found(f"{target.uri} n'existe pas")
    if step.keys is not None:
        values = parent[step.name]
        if step.node.kind == LIST:
            idx = _index(values, step.node.keys, step.keys)
        else:
            idx = _value_index(values, step.keys[0])
        if idx is None:
            raise not_found(f"{target.uri} n'existe pas")
        del values[idx]
        if not values:
            del parent[step.name]
        return
    del parent[step.name]


def prune(value: Any) -> Any:
    """Retire les conteneurs et listes vides (conteneurs sans présence : absents s'ils sont vides)."""
    if isinstance(value, dict):
        for k in list(value):
            v = value[k]
            if isinstance(v, dict):
                prune(v)
                if not v:
                    del value[k]
            elif isinstance(v, list):
                for e in v:
                    if isinstance(e, dict):
                        prune(e)
                if not v:
                    del value[k]
    return value


# --- filtres de lecture (RFC 8040 § 4.8.1, 4.8.2) --------------------------------------------

_DROP = object()


def filter_content(schema: Schema, path: tuple[str, ...], value: Any, content: str) -> Any:
    """content=config : données de configuration seules ; nonconfig : données d'état seules.

    Renvoie None si rien ne reste.
    """
    if content == "all":
        return value
    want_config = content == "config"

    def walk(p: tuple[str, ...], val: Any) -> Any:
        node = schema.node(p)
        if node is None:
            return val
        if not node.config:  # config false : tout le sous-arbre est de l'état
            return _DROP if want_config else val
        if node.kind in (LEAF, LEAF_LIST):
            return val if want_config else _DROP
        if node.kind == LIST and isinstance(val, list):
            out = []
            for entry in val:
                kept = walk_entry(p, node, entry)
                if kept is not _DROP:
                    out.append(kept)
            return out or _DROP
        if isinstance(val, dict):
            kept = walk_children(p, val)
            return kept if kept else _DROP
        return val

    def walk_children(p: tuple[str, ...], val: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in val.items():
            r = walk(p + (k,), v)
            if r is not _DROP:
                out[k] = r
        return out

    def walk_entry(p: tuple[str, ...], node: SchemaNode, entry: Any) -> Any:
        if not isinstance(entry, dict):
            return entry
        kept = walk_children(p, entry)
        if want_config:
            return kept
        if not kept:
            return _DROP
        # données d'état sous une liste de configuration : garder les clés pour situer l'instance
        return {**{k: entry[k] for k in node.keys if k in entry}, **kept}

    node = schema.node(path)
    if node is not None and node.kind == LIST and isinstance(value, dict):  # instance de liste
        result = walk_entry(path, node, value)
    else:
        result = walk(path, value)
    return None if result is _DROP else result


def limit_depth(value: Any, depth: int | None, level: int = 1) -> Any:
    """depth=N : le nœud visé est au niveau 1, ses enfants au niveau 2, etc. (§ 4.8.2)."""
    if depth is None:
        return value
    if isinstance(value, dict):
        if level >= depth:
            return {}
        return {k: limit_depth(v, depth, level + 1) for k, v in value.items()}
    if isinstance(value, list) and value and all(isinstance(e, dict) for e in value):
        return [limit_depth(e, depth, level) for e in value]  # instances : même niveau que la liste
    return value


def missing_error(target: Target) -> RestconfError:
    return not_found(f"{target.uri} n'existe pas", path=target.uri)
