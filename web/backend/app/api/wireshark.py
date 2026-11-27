"""Proxy inverse vers la visionneuse Wireshark d'une capture (HTTP et WebSocket).

La visionneuse n'est pas publiée : seul ce proxy l'atteint, après les contrôles Host/Origin et
l'authentification de l'interface. Il ajoute l'authentification HTTP Basic propre à la visionneuse.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import AsyncIterator

import aiohttp
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..captures import SIDECAR_PORT, VIEWER_USER, CaptureManager

log = logging.getLogger("sonic-lab.wireshark")
router = APIRouter(tags=["captures"])

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "content-length", "content-encoding",
}  # fmt: skip
# En-têtes de la requête qui n'ont pas à atteindre la visionneuse (identité de l'interface, hôte public).
_DROP_REQUEST = _HOP_BY_HOP | {"host", "authorization", "cookie", "origin", "referer", "accept-encoding"}
_WS_DROP = _DROP_REQUEST | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}

_STARTING_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta http-equiv="refresh" content="1">
<title>Wireshark</title></head><body style="font-family:sans-serif;color:#666;padding:2em">
Démarrage de Wireshark…</body></html>"""


def _basic(password: str) -> str:
    return "Basic " + base64.b64encode(f"{VIEWER_USER}:{password}".encode()).decode()


async def _target(request_app, cid: str) -> tuple[CaptureManager, str, str] | None:
    captures: CaptureManager = request_app.state.captures
    password = await captures.viewer_password(cid)
    if password is None:
        return None
    return captures, f"{captures.viewer_name(cid)}:{SIDECAR_PORT}", password


def _session(request_app) -> aiohttp.ClientSession:
    if getattr(request_app.state, "wireshark_http", None) is None:
        # certificat auto-signé de la visionneuse, sur le réseau interne
        request_app.state.wireshark_http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False), auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, connect=10)
        )
    return request_app.state.wireshark_http


@router.api_route("/wireshark/{cid}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def proxy_http(cid: str, path: str, request: Request):
    target = await _target(request.app, cid)
    if target is None:
        return JSONResponse({"error": f"visionneuse inconnue : {cid}"}, status_code=404)
    _, host, password = target
    url = f"https://{host}/wireshark/{cid}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
    headers["Authorization"] = _basic(password)
    try:
        upstream = await _session(request.app).request(
            request.method,
            url,
            params=request.query_params,
            headers=headers,
            data=await request.body() or None,
            allow_redirects=False,
        )
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            # visionneuse encore en démarrage (fenêtre séparée ouverte trop tôt) : la page se recharge seule
            return HTMLResponse(_STARTING_PAGE, status_code=503, headers={"Retry-After": "1"})
        return JSONResponse({"error": f"visionneuse injoignable : {exc}"}, status_code=502)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content.iter_any():
                yield chunk
        finally:
            upstream.release()

    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}
    if "Content-Encoding" in upstream.headers:
        out["Content-Encoding"] = upstream.headers["Content-Encoding"]
    return StreamingResponse(body(), status_code=upstream.status, headers=out)


@router.websocket("/wireshark/{cid}/{path:path}")
async def proxy_ws(ws: WebSocket, cid: str, path: str) -> None:
    target = await _target(ws.app, cid)
    if target is None:
        await ws.close(code=1008)
        return
    _, host, password = target
    url = f"wss://{host}/wireshark/{cid}/{path}"
    if ws.url.query:
        url += f"?{ws.url.query}"
    protocols = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    try:
        upstream = await _session(ws.app).ws_connect(
            url, headers={"Authorization": _basic(password)}, protocols=protocols, max_msg_size=0, heartbeat=30
        )
    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        log.warning("visionneuse %s injoignable : %s", cid, exc)
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=upstream.protocol or None)

    async def to_upstream() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await upstream.send_bytes(msg["bytes"])
            elif msg.get("text") is not None:
                await upstream.send_str(msg["text"])

    async def to_browser() -> None:
        async for msg in upstream:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_text(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
            else:
                return

    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError, aiohttp.ClientError):
                await task
        await upstream.close()
        with contextlib.suppress(RuntimeError):
            await ws.close()
