"""RESTCONF (RFC 8040) : chemins d'URI, paramètres, types de média, documents. Module pur (aucune E/S)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, unquote

ROOT = "/restconf"
DATA = "/restconf/data"
OPERATIONS = "/restconf/operations"
SCHEMAS = "/restconf/yang"  # téléchargement des modules YANG (feuille « schema » de la yang-library)

MEDIA_JSON = "application/yang-data+json"
MEDIA_YANG = "application/yang"
MEDIA_XRD = "application/xrd+xml"
JSON_TYPES = frozenset({MEDIA_JSON, "application/json"})
YANG_LIBRARY_VERSION = "2016-06-21"

HOST_META = (
    "<?xml version='1.0' encoding='UTF-8'?>\n"
    "<XRD xmlns='http://docs.oasis-open.org/ns/xri/xrd-1.0'>\n"
    "  <Link rel='restconf' href='/restconf'/>\n"
    "</XRD>\n"
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class RestconfError(Exception):
    """Erreur renvoyée au client : code HTTP et error-tag (RFC 8040 § 7)."""

    def __init__(
        self,
        status: int,
        tag: str,
        message: str,
        *,
        error_type: str = "protocol",
        path: str | None = None,
        allow: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.tag = tag
        self.message = message
        self.error_type = error_type
        self.path = path
        self.allow = allow  # méthodes permises, pour l'en-tête Allow d'un 405

    def document(self) -> dict[str, Any]:
        error: dict[str, Any] = {"error-type": self.error_type, "error-tag": self.tag}
        if self.path:
            error["error-path"] = self.path
        error["error-message"] = self.message
        return {"ietf-restconf:errors": {"error": [error]}}


def bad_request(message: str, *, tag: str = "invalid-value", path: str | None = None) -> RestconfError:
    return RestconfError(400, tag, message, path=path)


def invalid_data(message: str, path: str | None = None) -> RestconfError:
    """Données refusées par les modèles YANG ou par l'équipement (erreur « application »)."""
    return RestconfError(400, "invalid-value", message, error_type="application", path=path)


def not_found(message: str, path: str | None = None) -> RestconfError:
    return RestconfError(404, "invalid-value", message, path=path)


def data_exists(message: str, path: str | None = None) -> RestconfError:
    return RestconfError(409, "data-exists", message, error_type="application", path=path)


def method_not_allowed(method: str, allow: str) -> RestconfError:
    return RestconfError(405, "operation-not-supported", f"méthode {method} non prise en charge sur cette ressource", allow=allow)


def operation_failed(message: str) -> RestconfError:
    return RestconfError(500, "operation-failed", message, error_type="application")


@dataclass(frozen=True)
class Segment:
    """Élément d'un chemin RESTCONF : [module:]nom[=clé1,clé2…] (RFC 8040 § 3.5.3)."""

    module: str | None
    name: str
    keys: tuple[str, ...] | None = None

    def __str__(self) -> str:
        ident = f"{self.module}:{self.name}" if self.module else self.name
        if self.keys is None:
            return ident
        return ident + "=" + ",".join(quote(k, safe="") for k in self.keys)


def parse_path(path: str) -> list[Segment]:
    """Chemin encodé (« sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0 ») → segments.

    Le découpage (« / », « = », « , ») se fait avant le décodage %, comme l'exige la RFC : une virgule
    ou une barre oblique dans une valeur de clé arrive encodée (%2C, %2F).
    """
    path = path.strip("/")
    if not path:
        return []
    segments: list[Segment] = []
    for raw in path.split("/"):
        if not raw:
            raise bad_request("chemin d'URI invalide : segment vide")
        ident, sep, rest = raw.partition("=")
        ident = unquote(ident)
        module, colon, name = ident.partition(":")
        if not colon:
            module, name = "", ident
        if not _IDENTIFIER.match(name) or (module and not _IDENTIFIER.match(module)):
            raise bad_request(f"identifiant invalide dans l'URI : {ident!r}")
        keys = tuple(unquote(k) for k in rest.split(",")) if sep else None
        segments.append(Segment(module or None, name, keys))
    return segments


def format_path(segments: list[Segment]) -> str:
    return "/".join(str(s) for s in segments)


@dataclass(frozen=True)
class Query:
    content: str = "all"  # config | nonconfig | all
    depth: int | None = None  # None : unbounded


def parse_query(query: str) -> Query:
    """Paramètres de requête pris en charge : content et depth (RFC 8040 § 4.8)."""
    content, depth = "all", None
    seen: set[str] = set()
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name in seen:
            raise bad_request(f"paramètre de requête répété : {name}")
        seen.add(name)
        if name == "content":
            if value not in ("config", "nonconfig", "all"):
                raise bad_request(f"content={value!r} : valeurs possibles config, nonconfig, all")
            content = value
        elif name == "depth":
            if value == "unbounded":
                depth = None
            elif value.isdigit() and 1 <= int(value) <= 65535:
                depth = int(value)
            else:
                raise bad_request(f"depth={value!r} : entier de 1 à 65535, ou unbounded")
        else:
            raise bad_request(f"paramètre de requête non pris en charge : {name} (seulement content et depth)")
    return Query(content, depth)


def media_type(header: str | None) -> str:
    """« application/yang-data+json; charset=utf-8 » → « application/yang-data+json »."""
    return (header or "").split(";", 1)[0].strip().lower()


def accepts_json(accept: str | None) -> bool:
    if not accept or not accept.strip():
        return True
    for part in accept.split(","):
        mt = media_type(part)
        if mt in JSON_TYPES or mt in ("*/*", "application/*"):
            return True
    return False


def root_document() -> dict[str, Any]:
    return {
        "ietf-restconf:restconf": {
            "data": {},
            "operations": {},
            "yang-library-version": YANG_LIBRARY_VERSION,
        }
    }


def library_version_document() -> dict[str, Any]:
    return {"ietf-restconf:yang-library-version": YANG_LIBRARY_VERSION}


def modules_state(modules: list[dict[str, Any]], schema_base: str) -> dict[str, Any]:
    """Contenu de ietf-yang-library:modules-state (RFC 7895) ; schema_base : URL de /restconf/yang."""
    entries = []
    for m in sorted(modules, key=lambda m: m["name"]):
        entry: dict[str, Any] = {
            "name": m["name"],
            "revision": m.get("revision") or "",
            "namespace": m.get("namespace") or "",
            "conformance-type": "implement" if m.get("implemented", True) else "import",
        }
        if m.get("downloadable", True):
            rev = f"@{m['revision']}" if m.get("revision") else ""
            entry["schema"] = f"{schema_base}/{m['name']}{rev}"
        entries.append(entry)
    digest = hashlib.sha256(
        "\n".join(f"{e['name']}@{e['revision']}" for e in entries).encode()
    ).hexdigest()[:16]
    return {"module-set-id": digest, "module": entries}
