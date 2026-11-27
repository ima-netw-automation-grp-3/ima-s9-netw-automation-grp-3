"""Terminal web : relie un WebSocket (xterm.js) à un `docker exec -it` dans le nœud.

Protocole :
  navigateur -> serveur : texte JSON {"type": "input", "data": "..."} ou {"type": "resize", "cols", "rows"}
  serveur -> navigateur : octets bruts du terminal ; texte JSON {"type": "exit"|"error", ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..errors import LabError

router = APIRouter()

TTY_ENV = {"TERM": "xterm-256color", "LANG": "C.UTF-8", "HOME": "/root"}


@router.websocket("/ws/nodes/{name}/terminal")
async def terminal(ws: WebSocket, name: str, shell: str | None = None, cols: int = 120, rows: int = 32) -> None:
    lab = ws.app.state.lab
    await ws.accept()
    try:
        node = await lab.running_node(name)
        argv = node.shell_argv(shell)
        session = await lab.docker.open_tty(node.container, argv, env=TTY_ENV, cols=cols, rows=rows)
    except LabError as exc:
        await ws.send_text(json.dumps({"type": "error", "message": exc.message}))
        await ws.close(code=1011)
        return

    async def to_browser() -> None:
        while True:
            data = await session.read()
            if data is None:
                return
            await ws.send_bytes(data)

    async def to_container() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await session.write(msg["bytes"])
                continue
            try:
                data = json.loads(msg.get("text") or "")
            except ValueError:
                continue
            if data.get("type") == "input":
                await session.write(str(data.get("data", "")).encode("utf-8"))
            elif data.get("type") == "resize":
                await session.resize(int(data.get("cols", 80)), int(data.get("rows", 24)))

    out_task = asyncio.create_task(to_browser())
    in_task = asyncio.create_task(to_container())
    try:
        done, pending = await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    finally:
        await session.close()
    if out_task in done:  # le shell s'est terminé (exit, Ctrl-D)
        code = await session.exit_code()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await ws.send_text(json.dumps({"type": "exit", "code": code}))
            await ws.close()
