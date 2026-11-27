"""Logique RESTCONF : routage, méthodes, corps, réponses. Sans E/S propres (sources injectées).

  /.well-known/host-meta                      découverte de la racine (RFC 8040 § 3.1)
  /restconf                                   ressource racine
  /restconf/yang-library-version
  /restconf/data[/chemin]                     données : sonic-* (CONFIG_DB), sonic-lab (FRR),
                                              ietf-yang-library:modules-state
  /restconf/operations[/module:opération]     sonic-lab:ping, sonic-lab:preview
  /restconf/yang/<module>[@révision]          téléchargement des modules YANG
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from . import configdb_patch, labmodule
from .datatree import (
    CONTAINER,
    Missing,
    Target,
    create,
    delete,
    filter_content,
    find,
    limit_depth,
    merge,
    missing_error,
    prune,
    put,
    resolve,
    unwrap,
    wrap,
)
from .rfc8040 import (
    DATA,
    HOST_META,
    MEDIA_JSON,
    MEDIA_XRD,
    MEDIA_YANG,
    OPERATIONS,
    ROOT,
    SCHEMAS,
    Query,
    RestconfError,
    Segment,
    accepts_json,
    bad_request,
    invalid_data,
    library_version_document,
    media_type,
    method_not_allowed,
    modules_state,
    not_found,
    operation_failed,
    parse_path,
    parse_query,
    root_document,
)
from .store import FrrStore, InvalidData, OperationFailed, SonicStore

log = logging.getLogger("restconf")

ALLOW_SONIC = "GET, HEAD, PUT, PATCH, POST, DELETE, OPTIONS"
ALLOW_LAB = "GET, HEAD, PUT, PATCH, OPTIONS"
ALLOW_READ = "GET, HEAD, OPTIONS"
ALLOW_DATASTORE = "GET, HEAD, POST, OPTIONS"
ALLOW_OPERATION = "POST, OPTIONS"
LIBRARY = "ietf-yang-library"


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Edit:
    """Résultat d'une écriture sur les données sonic-* (appliquée ou simulée)."""

    target: Target
    created: bool
    before: dict[str, Any]
    after: dict[str, Any]
    ops: list[dict[str, Any]]


def json_body(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


class RestconfService:
    def __init__(self, sonic: SonicStore, frr: FrrStore, *, scheme: str = "https") -> None:
        self.sonic = sonic
        self.frr = frr
        self.scheme = scheme
        # libyang et GCU ne sont pas prévus pour des accès concurrents : une requête à la fois sur les
        # données (les lectures sont rapides ; une écriture GCU prend 1 à 3 s).
        self.lock = threading.RLock()

    # -- point d'entrée ----------------------------------------------------------------------
    def handle(self, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
        path, _, query = target.partition("?")
        hdrs = {k.lower(): v for k, v in headers.items()}
        try:
            return self._route(method.upper(), path or "/", query, hdrs, body)
        except RestconfError as exc:
            return self.error_response(exc)
        except InvalidData as exc:
            return self.error_response(invalid_data(exc.message, exc.path))
        except OperationFailed as exc:
            return self.error_response(operation_failed(str(exc)))
        except Exception as exc:  # garde-fou : jamais de trace Python vers le client
            log.exception("erreur interne sur %s %s", method, target)
            return self.error_response(operation_failed(f"erreur interne : {exc}"))

    @staticmethod
    def error_response(exc: RestconfError) -> Response:
        headers = {"Content-Type": MEDIA_JSON}
        if exc.allow:
            headers["Allow"] = exc.allow
        return Response(exc.status, json_body(exc.document()), headers)

    # -- routage -------------------------------------------------------------------------------
    def _route(self, method: str, path: str, query: str, headers: dict[str, str], body: bytes) -> Response:
        if path == "/.well-known/host-meta":
            if method == "OPTIONS":
                return self._options(ALLOW_READ)
            self._only(method, ALLOW_READ)
            return Response(200, HOST_META.encode(), {"Content-Type": MEDIA_XRD})
        if path.rstrip("/") == ROOT:
            return self._document(method, query, headers, root_document())
        if not path.startswith(ROOT + "/"):
            raise not_found(f"ressource inconnue : {path} (racine RESTCONF : {ROOT})")
        rest = path[len(ROOT) + 1:]
        if rest == "yang-library-version":
            return self._document(method, query, headers, library_version_document())
        if rest.startswith("yang/"):
            return self._schema(method, rest[len("yang/"):])
        if rest.rstrip("/") == "operations":
            doc = {"ietf-restconf:operations": {op: [None] for op in labmodule.OPERATIONS}}
            return self._document(method, query, headers, doc)
        if rest.startswith("operations/"):
            return self._operation(method, rest[len("operations/"):].strip("/"), query, headers, body)
        if rest == "data" or rest.startswith("data/"):
            return self._data(method, rest[len("data"):], query, headers, body)
        raise not_found(f"ressource inconnue : {path}")

    @staticmethod
    def _only(method: str, allow: str) -> None:
        if method not in {m.strip() for m in allow.split(",")}:
            raise method_not_allowed(method, allow)

    @staticmethod
    def _options(allow: str, *, patch: bool = False) -> Response:
        headers = {"Allow": allow}
        if patch:
            headers["Accept-Patch"] = MEDIA_JSON
        return Response(200, b"", headers)

    @staticmethod
    def _no_query(query: str) -> None:
        if query:
            raise bad_request("paramètres de requête acceptés seulement par GET et HEAD")

    @staticmethod
    def _check_accept(headers: dict[str, str]) -> None:
        if not accepts_json(headers.get("accept")):
            raise RestconfError(406, "invalid-value", f"seul JSON est servi ({MEDIA_JSON})")

    def _json(self, status: int, doc: Any, headers: dict[str, str] | None = None) -> Response:
        return Response(status, json_body(doc), {"Content-Type": MEDIA_JSON, **(headers or {})})

    def _document(self, method: str, query: str, headers: dict[str, str], doc: Any) -> Response:
        if method == "OPTIONS":
            return self._options(ALLOW_READ)
        self._only(method, ALLOW_READ)
        self._no_query(query)
        self._check_accept(headers)
        return self._json(200, doc)

    @staticmethod
    def _payload(headers: dict[str, str], body: bytes, *, required: bool = True) -> Any:
        if not body.strip():
            if required:
                raise bad_request("corps de requête manquant", tag="malformed-message")
            return None
        ctype = media_type(headers.get("content-type"))
        if ctype == "application/yang-patch+json":
            raise RestconfError(415, "invalid-value", "YANG-Patch (RFC 8072) non pris en charge : utilisez PATCH en application/yang-data+json")
        if ctype not in ("application/yang-data+json", "application/json"):
            raise RestconfError(415, "invalid-value", f"type de contenu non pris en charge : {ctype or 'absent'} (attendu : {MEDIA_JSON})")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise bad_request(f"JSON invalide : {exc}", tag="malformed-message") from None

    # -- schémas -------------------------------------------------------------------------------
    def _schema(self, method: str, spec: str) -> Response:
        if method == "OPTIONS":
            return self._options(ALLOW_READ)
        self._only(method, ALLOW_READ)
        name, _, revision = spec.strip("/").partition("@")
        if name == labmodule.MODULE:
            path = labmodule.YANG_FILE if revision in ("", labmodule.REVISION) else None
        else:
            path = self.sonic.schema_file(name, revision)
        if path is None or not path.is_file():
            raise not_found(f"module YANG inconnu : {spec}")
        return Response(200, path.read_bytes(), {"Content-Type": MEDIA_YANG})

    # -- données ---------------------------------------------------------------------------------
    def _data(self, method: str, sub: str, query: str, headers: dict[str, str], body: bytes) -> Response:
        segments = parse_path(sub)
        if method in ("GET", "HEAD"):
            q = parse_query(query)
            self._check_accept(headers)
        else:
            self._no_query(query)
            q = Query()
        if not segments:
            return self._datastore(method, q, headers, body)
        module = segments[0].module
        if module == labmodule.MODULE:
            return self._lab(method, segments, q, headers, body)
        if module == LIBRARY:
            return self._library(method, segments, q, headers)
        return self._sonic(method, segments, q, headers, body)

    def _datastore(self, method: str, q: Query, headers: dict[str, str], body: bytes) -> Response:
        if method == "OPTIONS":
            return self._options(ALLOW_DATASTORE, patch=False)
        if method == "POST":
            payload = self._payload(headers, body)
            name = next(iter(payload), "") if isinstance(payload, dict) else ""
            if name.partition(":")[0] in (labmodule.MODULE, LIBRARY):
                raise method_not_allowed("POST", ALLOW_READ)
            edit = self._sonic_edit("POST", [], payload, dry_run=False)
            return Response(201, b"", {"Location": f"{DATA}/{edit.target.uri}"})
        self._only(method, ALLOW_DATASTORE)
        data: dict[str, Any] = {}
        if q.content != "nonconfig":
            with self.lock:
                data.update(self.sonic.read())
        frr = self._lab_data(want_config=q.content != "nonconfig", want_state=q.content != "config", path=(labmodule.FRR,))
        data.update(frr)
        if q.content != "config":
            data[f"{LIBRARY}:modules-state"] = self._modules_state(headers)
        # le datastore est au niveau 1, les conteneurs des modules au niveau 2 (RFC 8040 § 4.8.2)
        return self._json(200, {"ietf-restconf:data": limit_depth(data, q.depth)})

    def _modules_state(self, headers: dict[str, str]) -> dict[str, Any]:
        host = headers.get("host")
        base = f"{self.scheme}://{host}{SCHEMAS}" if host else SCHEMAS
        return modules_state([*self.sonic.modules(), labmodule.module_entry()], base)

    def _library(self, method: str, segments: list[Segment], q: Query, headers: dict[str, str]) -> Response:
        if method == "OPTIONS":
            return self._options(ALLOW_READ)
        self._only(method, ALLOW_READ)
        if len(segments) != 1 or segments[0].name != "modules-state" or segments[0].keys is not None:
            raise not_found(f"seul {LIBRARY}:modules-state est servi")
        if q.content == "config":
            raise not_found(f"{LIBRARY}:modules-state ne contient que des données d'état (content=config)")
        doc = limit_depth(self._modules_state(headers), q.depth)
        return self._json(200, {f"{LIBRARY}:modules-state": doc})

    # -- données sonic-* (CONFIG_DB) -----------------------------------------------------------
    def _sonic(self, method: str, segments: list[Segment], q: Query, headers: dict[str, str], body: bytes) -> Response:
        if method == "OPTIONS":
            resolve(self.sonic.schema, segments)
            return self._options(ALLOW_SONIC, patch=True)
        if method in ("GET", "HEAD"):
            target = resolve(self.sonic.schema, segments)
            with self.lock:
                tree = self.sonic.read()
            return self._read(target, tree, q, self.sonic.schema)
        if method not in ("PUT", "PATCH", "POST", "DELETE"):
            raise method_not_allowed(method, ALLOW_SONIC)
        payload = self._payload(headers, body, required=method != "DELETE")
        if method == "DELETE" and payload is not None:
            raise bad_request("DELETE n'accepte pas de corps", tag="malformed-message")
        edit = self._sonic_edit(method, segments, payload, dry_run=False)
        if method == "POST":
            return Response(201, b"", {"Location": f"{DATA}/{edit.target.uri}"})
        if method == "PUT" and edit.created:
            return Response(201, b"", {"Location": f"{DATA}/{edit.target.uri}"})
        return Response(204)

    def _read(self, target: Target, tree: dict[str, Any], q: Query, schema: Any) -> Response:
        try:
            value = find(tree, target)
        except Missing:
            if target.last.node.kind != CONTAINER:
                raise missing_error(target) from None
            value = {}  # conteneur sans présence : vide plutôt qu'absent
        value = filter_content(schema, target.schema_path, value, q.content)
        if value is None:
            if target.last.node.kind != CONTAINER:
                raise not_found(f"{target.uri} : aucune donnée pour content={q.content}", path=target.uri)
            value = {}
        return self._json(200, wrap(target, limit_depth(value, q.depth)))

    def _sonic_edit(self, method: str, segments: list[Segment], payload: Any, *, dry_run: bool) -> Edit:
        """Édite une copie de l'arbre, valide le résultat complet, puis applique la différence par GCU."""
        schema = self.sonic.schema
        with self.lock:
            tree = self.sonic.read()
            new = copy.deepcopy(tree)
            created = False
            if method == "POST":
                target = create(schema, new, segments, payload)
                created = True
            else:
                target = resolve(schema, segments)
                if method == "PUT":
                    created = put(new, target, unwrap(target, payload))
                elif method == "PATCH":
                    merge(schema, new, target, unwrap(target, payload))
                elif method == "DELETE":
                    delete(new, target)
                else:
                    raise method_not_allowed(method, ALLOW_SONIC)
            prune(new)
            top = target.top
            before = {top: tree[top]} if top in tree else {}
            after = {top: new[top]} if top in new else {}
            ops: list[dict[str, Any]] = []
            if before != after:
                self.sonic.validate(new)
                ops = configdb_patch.diff(self.sonic.to_configdb(before), self.sonic.to_configdb(after))
                if ops:
                    self.sonic.apply(ops, dry_run=dry_run)
                    if not dry_run:
                        log.info("CONFIG_DB : %s %s, %d opération(s) appliquée(s) par GCU", method, target.uri, len(ops))
            return Edit(target, created, before, after, ops)

    # -- module sonic-lab (FRR) ----------------------------------------------------------------
    def _lab_data(self, *, want_config: bool, want_state: bool, path: tuple[str, ...]) -> dict[str, Any]:
        frr: dict[str, Any] = {}
        with self.lock:
            if want_config and path in ((labmodule.FRR,), labmodule.RUNNING):
                frr["running-config"] = self.frr.running()
            if want_state and (path == (labmodule.FRR,) or path[:2] == labmodule.STATE):
                frr["state"] = {"bgp-neighbor": self.frr.neighbors()}
        return prune({labmodule.FRR: frr})

    def _lab(self, method: str, segments: list[Segment], q: Query, headers: dict[str, str], body: bytes) -> Response:
        target = resolve(labmodule.SCHEMA, segments)
        writable = target.schema_path in ((labmodule.FRR,), labmodule.RUNNING)
        allow = ALLOW_LAB if writable else ALLOW_READ
        if method == "OPTIONS":
            return self._options(allow, patch=writable)
        if method in ("GET", "HEAD"):
            tree = self._lab_data(
                want_config=q.content != "nonconfig", want_state=q.content != "config", path=target.schema_path
            )
            return self._read(target, tree, q, labmodule.SCHEMA)
        if method not in ("PUT", "PATCH") or not writable:
            raise method_not_allowed(method, allow)
        text = self._frr_text(target, unwrap(target, self._payload(headers, body)), method)
        if text is not None:
            with self.lock:
                result = self.frr.apply(text, "replace" if method == "PUT" else "merge")
            log.info("FRR : %s %s (%s)", method, target.uri, ", ".join(s["step"] for s in result.get("steps", [])))
        return Response(204)

    @staticmethod
    def _frr_text(target: Target, value: Any, method: str) -> str | None:
        """Texte de configuration FRR d'un PUT/PATCH sur sonic-lab:frr ou sur sa feuille running-config."""
        if target.schema_path == labmodule.RUNNING:
            text = value
        else:
            if "state" in value:
                raise bad_request("state est en lecture seule (config false)")
            unknown = set(value) - {"running-config"}
            if unknown:
                raise bad_request(f"nœud(s) inconnu(s) : {', '.join(sorted(unknown))}", tag="unknown-element")
            if "running-config" not in value:
                if method == "PUT":
                    raise bad_request("PUT sur sonic-lab:frr : running-config obligatoire (il remplace toute la configuration FRR)")
                return None
            text = value["running-config"]
        if not isinstance(text, str):
            raise bad_request("running-config : du texte est attendu", tag="malformed-message")
        return text

    # -- opérations ----------------------------------------------------------------------------
    def _operation(self, method: str, name: str, query: str, headers: dict[str, str], body: bytes) -> Response:
        if name not in labmodule.OPERATIONS:
            raise not_found(f"opération inconnue : {name} (disponibles : {', '.join(labmodule.OPERATIONS)})")
        if method == "OPTIONS":
            return self._options(ALLOW_OPERATION)
        self._only(method, ALLOW_OPERATION)
        self._no_query(query)
        self._check_accept(headers)
        inp = labmodule.rpc_input(self._payload(headers, body, required=False), name)
        if name == f"{labmodule.MODULE}:ping":
            destination, source, count = labmodule.ping_input(inp)
            output = self.frr.ping(destination, source, count)
        else:
            path, meth, data = labmodule.preview_input(inp)
            output = self._preview(path, meth, data)
        return self._json(200, {f"{labmodule.MODULE}:output": output})

    def _preview(self, path: str, method: str, data: Any) -> dict[str, Any]:
        """Écriture simulée : toute erreur liée à la requête simulée donne valid=false (pas un 4xx)."""
        try:
            segments = parse_path(path)
            if segments:
                module = segments[0].module
            else:
                if method != "POST":
                    raise bad_request("chemin vide : seul POST vise le datastore")
                module = (next(iter(data), "") if isinstance(data, dict) else "").partition(":")[0]
            if module == labmodule.MODULE:
                target = resolve(labmodule.SCHEMA, segments)
                if method not in ("PUT", "PATCH") or target.schema_path not in ((labmodule.FRR,), labmodule.RUNNING):
                    raise method_not_allowed(method, ALLOW_LAB)
                text = self._frr_text(target, unwrap(target, data), method)
                if text is None:
                    return {"valid": True, "diff": ""}
                with self.lock:
                    return self.frr.preview(text, "replace" if method == "PUT" else "merge")
            if module == LIBRARY:
                raise method_not_allowed(method, ALLOW_READ)
            edit = self._sonic_edit(method, segments, data, dry_run=True)
            return {
                "valid": True,
                "diff": configdb_patch.unified_diff(edit.before, edit.after, before_name="running", after_name="après"),
                "patch": json.dumps(edit.ops, indent=2, ensure_ascii=False),
            }
        except RestconfError as exc:
            return {"valid": False, "errors": exc.message + (f" ({exc.path})" if exc.path and exc.path not in exc.message else "")}
        except InvalidData as exc:
            return {"valid": False, "errors": exc.message + (f" ({exc.path})" if exc.path else "")}
