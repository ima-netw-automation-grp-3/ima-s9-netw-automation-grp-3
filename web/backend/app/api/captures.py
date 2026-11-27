"""Captures de paquets : liste, démarrage, arrêt, suppression et téléchargement du pcap."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..captures import CaptureManager

router = APIRouter(prefix="/api/captures", tags=["captures"])


def get_captures(request: Request) -> CaptureManager:
    return request.app.state.captures


CapturesDep = Annotated[CaptureManager, Depends(get_captures)]


class StartBody(BaseModel):
    node: str = Field(..., min_length=1, max_length=64)
    interface: str = Field(..., min_length=1, max_length=15)
    filter: str = Field("", max_length=200)


@router.get("")
async def list_captures(captures: CapturesDep) -> dict[str, Any]:
    return {
        "captures": [c.to_dict() for c in await captures.sessions()],
        "max_sessions": captures.settings.capture_max_sessions,
        "max_mb": captures.settings.capture_max_mb,
    }


@router.get("/interfaces")
async def node_interfaces(captures: CapturesDep, node: str = Query(..., min_length=1)) -> dict[str, Any]:
    return {"node": node, "interfaces": await captures.interfaces(node)}


@router.post("")
async def start_capture(body: StartBody, captures: CapturesDep) -> dict[str, Any]:
    return (await captures.start(body.node, body.interface, body.filter)).to_dict()


@router.post("/{cid}/stop")
async def stop_capture(cid: str, captures: CapturesDep) -> dict[str, Any]:
    return (await captures.stop(cid)).to_dict()


@router.delete("/{cid}")
async def delete_capture(cid: str, captures: CapturesDep, delete_file: bool = True) -> dict[str, str]:
    await captures.delete(cid, delete_file=delete_file)
    return {"status": "deleted"}


@router.get("/{cid}/download")
async def download_capture(cid: str, captures: CapturesDep) -> FileResponse:
    capture = await captures.get(cid)
    path = captures.file_path(capture.filename)
    if not path.is_file():
        from ..errors import NotFound

        raise NotFound("fichier de capture absent")
    return FileResponse(path, media_type="application/vnd.tcpdump.pcap", filename=capture.filename)
