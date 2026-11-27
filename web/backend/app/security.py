"""Protections contre les requêtes intersites.

L'interface pilote Docker : une page web malveillante ouverte dans le navigateur de l'utilisateur ne
doit pas pouvoir s'en servir.
  - Les WebSockets n'obéissent pas à CORS : sans contrôle de l'en-tête Origin, n'importe quel site
    pourrait ouvrir un terminal root dans les nœuds du lab.
  - Les POST sans corps (redéploiement, réinitialisation...) sont des requêtes « simples » qu'un site
    tiers peut déclencher.
  - Le DNS rebinding contourne l'écoute sur 127.0.0.1 : l'en-tête Host est donc vérifié.

Règles : Host doit figurer dans la liste autorisée ; pour les WebSockets et les méthodes modifiantes,
un en-tête Origin, s'il est présent, doit désigner la même origine que Host. Les clients sans Origin
(curl, scripts) restent acceptés.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def hostname_of(host: str) -> str:
    """Nom d'hôte sans le port ("[::1]:8080" -> "[::1]", "localhost:8080" -> "localhost")."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class SameOriginMiddleware:
    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.any_host = "*" in allowed_hosts
        self.allowed = {h.lower() for h in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        host = headers.get("host", "")
        if not self.any_host and hostname_of(host) not in self.allowed:
            await self._reject(scope, receive, send, 400, f"hôte non autorisé : {host!r} (voir WEB_ALLOWED_HOSTS)")
            return
        origin = headers.get("origin")
        unsafe = scope["type"] == "websocket" or scope.get("method", "GET") not in SAFE_METHODS
        if unsafe and origin is not None and urlsplit(origin).netloc.lower() != host.lower():
            await self._reject(scope, receive, send, 403, f"origine refusée : {origin}")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": message[:120]})
            return
        await JSONResponse({"error": message}, status_code=status)(scope, receive, send)
