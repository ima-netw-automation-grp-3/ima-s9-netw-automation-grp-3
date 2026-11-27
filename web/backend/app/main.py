"""Application FastAPI : API REST, WebSocket du terminal et SPA React.

Lancement : uvicorn app.main:create_app --factory --port 8080 (dans l'image : --host 0.0.0.0, et compose
publie le port sur WEB_BIND, 127.0.0.1 par défaut).
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from .api import captures as captures_api
from .api import config as config_api
from .api import lab as lab_api
from .api import monitoring as monitoring_api
from .api import nodes as nodes_api
from .api import terminal as terminal_api
from .api import wireshark as wireshark_api
from .captures import CaptureManager
from .docker_api import DockerAPI
from .errors import LabError
from .lab import LabService
from .monitoring import Monitoring
from .security import SameOriginMiddleware
from .settings import Settings
from .store import Store

log = logging.getLogger("sonic-lab")


class SPAStaticFiles(StaticFiles):
    """Fichiers statiques de la SPA ; les routes inconnues renvoient index.html (routage côté client).

    index.html est revalidé à chaque chargement (no-cache) : après une mise à jour de l'image, le navigateur prend
    le nouveau bundle au lieu de garder l'ancien. Les assets, nommés d'après leur contenu, restent en cache.
    """

    async def get_response(self, path: str, scope: Scope) -> Any:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith(("api/", "ws/")):
                raise
            response = await super().get_response("index.html", scope)
        else:
            if response.status_code == 404 and not path.startswith(("api/", "ws/")):
                response = await super().get_response("index.html", scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


class BasicAuthMiddleware:
    """Authentification HTTP Basic optionnelle (HTTP et WebSocket), sauf /api/health."""

    def __init__(self, app: ASGIApp, user: str, password: str) -> None:
        self.app = app
        # comparés en octets : compare_digest refuse les chaînes non ASCII (accents dans un mot de passe)
        self.user = user.encode()
        self.password = password.encode()

    def _authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if not raw.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(raw[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        user_ok = secrets.compare_digest(user.encode(), self.user)
        return secrets.compare_digest(password.encode(), self.password) and user_ok


    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") == "/api/health":
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"error": "authentification requise"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="sonic-lab"'},
        )
        await response(scope, receive, send)


def check_exposure(settings: Settings) -> None:
    """Refuse de servir sans authentification une interface exposée : elle donne un accès root à l'hôte
    (socket Docker, terminal dans des conteneurs privilégiés)."""
    if settings.exposed and not settings.auth_enabled:
        message = (
            "interface exposée (WEB_BIND ou WEB_ALLOWED_HOSTS hors de la boucle locale) sans authentification : "
            "définissez WEB_AUTH_USER et WEB_AUTH_PASSWORD dans .env, puis make up"
        )
        log.error(message)
        raise RuntimeError(message)


def create_app(settings: Settings | None = None, docker: DockerAPI | None = None) -> FastAPI:
    if settings is None:  # lancement réel (uvicorn --factory) : réglages de l'environnement
        settings = Settings.from_env()
        check_exposure(settings)
    docker = docker or DockerAPI(settings.docker_url)
    store = Store(settings.configs_dir, settings.backups_dir, owner_ref=settings.lab_dir)
    lab = LabService(settings, docker, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await docker.start()
        try:
            yield
        finally:
            if app.state.wireshark_http is not None:
                await app.state.wireshark_http.close()
            await docker.close()

    app = FastAPI(
        title="SONiC Lab",
        description="Gestion du lab containerlab SONiC : nœuds, configuration, terminal, captures, métrologie (Grafana)",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.lab = lab
    app.state.docker = docker
    app.state.monitoring = Monitoring(settings)
    app.state.captures = CaptureManager(settings, docker, lab)
    app.state.wireshark_http = None

    @app.exception_handler(LabError)
    async def _lab_error(_: Request, exc: LabError) -> JSONResponse:
        return JSONResponse({"error": exc.message, "detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("erreur inattendue")
        return JSONResponse({"error": f"erreur interne : {type(exc).__name__}: {exc}"}, status_code=500)

    app.include_router(lab_api.router)
    app.include_router(nodes_api.router)
    app.include_router(config_api.router)
    app.include_router(terminal_api.router)
    app.include_router(monitoring_api.router)
    app.include_router(captures_api.router)
    app.include_router(wireshark_api.router)

    if settings.auth_enabled:
        app.add_middleware(BasicAuthMiddleware, user=settings.auth_user, password=settings.auth_password)
    # ajouté en dernier = exécuté en premier
    app.add_middleware(SameOriginMiddleware, allowed_hosts=settings.allowed_hosts)

    if settings.static_dir.is_dir():
        app.mount("/", SPAStaticFiles(directory=settings.static_dir, html=True), name="spa")
    else:
        log.warning("dossier statique absent (%s) : seule l'API est servie", settings.static_dir)

    return app
