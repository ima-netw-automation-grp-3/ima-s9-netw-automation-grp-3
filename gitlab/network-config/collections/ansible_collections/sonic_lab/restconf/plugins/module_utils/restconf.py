# Client RESTCONF des routeurs du lab et application de documents validés par le routeur.
#
# Utilisé par le module sonic_lab.restconf.config. La logique (apply_documents) ne dépend que d'un
# client qui expose request() et operation() : les tests la vérifient avec un faux routeur.

from __future__ import annotations

import json
import socket
import ssl
from typing import Any
from urllib.error import HTTPError, URLError

MEDIA = "application/yang-data+json"
FRR_PREFIX = "sonic-lab:frr"


class RestconfError(Exception):
    """Routeur injoignable, réponse inattendue, ou document refusé par le routeur."""


def errors_text(doc: Any) -> str:
    """Message d'un document d'erreur RFC 8040 (ietf-restconf:errors), ou le document tronqué."""
    try:
        return "; ".join(
            e.get("error-message", "?") + (f" ({e['error-path']})" if e.get("error-path") else "")
            for e in doc["ietf-restconf:errors"]["error"]
        )
    except (KeyError, TypeError):
        return json.dumps(doc, ensure_ascii=False)[:500] if doc else "(pas de corps)"


class Restconf:
    """Client minimal : HTTP Basic, JSON, certificat auto-signé accepté par défaut."""

    def __init__(self, url: str, user: str, password: str, validate_certs: bool = False, timeout: int = 300, opener=None) -> None:
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.validate_certs = validate_certs
        self.timeout = timeout
        if opener is None:
            from ansible.module_utils.urls import open_url

            opener = open_url
        self._open = opener

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Accept": MEDIA}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = MEDIA
        try:
            resp = self._open(
                self.url + path,
                data=data,
                headers=headers,
                method=method,
                url_username=self.user,
                url_password=self.password,
                force_basic_auth=True,
                validate_certs=self.validate_certs,
                timeout=self.timeout,
            )
            status, raw = resp.getcode(), resp.read()
        except HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            raise RestconfError(f"routeur injoignable ({self.url}) : {exc}") from None
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            doc = {"raw": raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)}
        return status, doc

    def operation(self, name: str, inp: dict[str, Any]) -> dict[str, Any]:
        status, doc = self.request("POST", f"/operations/{name}", {"sonic-lab:input": inp})
        if status != 200:
            raise RestconfError(f"opération {name} : HTTP {status} : {errors_text(doc)}")
        return (doc or {}).get("sonic-lab:output") or {}


def has_changes(path: str, preview: dict[str, Any]) -> bool:
    """Le document change-t-il le routeur ? CONFIG_DB : opérations du JSON Patch calculé par le routeur ;
    FRR : différence de texte, ou commandes du remplacement."""
    if path.startswith(FRR_PREFIX):
        return bool((preview.get("diff") or "").strip()) or bool(preview.get("commands"))
    patch = preview.get("patch")
    if patch is not None:
        try:
            return bool(json.loads(patch) if isinstance(patch, str) else patch)
        except ValueError:
            pass
    return bool((preview.get("diff") or "").strip())


class ApplyError(RestconfError):
    def __init__(self, message: str, results: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.results = results


def apply_documents(client: Any, documents: list[dict[str, Any]], check_mode: bool) -> list[dict[str, Any]]:
    """Pour chaque document, dans l'ordre : preview par le routeur, puis PUT ou PATCH s'il change
    quelque chose (sauf en check_mode). S'arrête au premier refus, résultats déjà obtenus joints."""
    results: list[dict[str, Any]] = []
    for doc in documents:
        path = doc["path"].removeprefix("/restconf/data/").lstrip("/")
        method = (doc.get("method") or "put").upper()
        entry: dict[str, Any] = {"name": doc.get("name") or path, "path": path, "method": method}
        try:
            preview = client.operation("sonic-lab:preview", {"path": path, "method": method, "data": doc["content"]})
        except RestconfError as exc:
            results.append({**entry, "error": str(exc)})
            raise ApplyError(f"{entry['name']} : {exc}", results) from None
        if not preview.get("valid"):
            reason = preview.get("errors") or "refusé par le routeur"
            results.append({**entry, "error": reason})
            raise ApplyError(f"{entry['name']} ({method} {path}) refusé par le routeur : {reason}", results)
        entry.update(changed=has_changes(path, preview), diff=preview.get("diff") or "", resets=list(preview.get("resets") or []))
        if entry["changed"] and not check_mode:
            status, body = client.request(method, f"/data/{path}", doc["content"])
            if status not in (200, 201, 204):
                results.append({**entry, "error": f"HTTP {status} : {errors_text(body)}"})
                raise ApplyError(f"{entry['name']} ({method} {path}) : HTTP {status} : {errors_text(body)}", results)
            entry["status"] = status
        results.append(entry)
    return results


def diff_sections(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Diffs pour --diff : un bloc par document modifié, titré."""
    return [
        {"prepared": f"=== {r['name']} : {r['method']} {r['path']}\n{r['diff'].rstrip()}\n"}
        for r in results
        if r.get("changed")
    ]
