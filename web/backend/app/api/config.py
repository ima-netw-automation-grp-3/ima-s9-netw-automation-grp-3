"""Routes de configuration des routeurs : lecture, aperçu, push, sauvegarde, snapshots."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..errors import BadRequest
from ..nodes.sonic import SonicNode
from ..store import DOMAINS
from .deps import LabDep

router = APIRouter(prefix="/api/nodes/{name}", tags=["config"])

Domain = Literal["configdb", "frr"]


class PushBody(BaseModel):
    domain: Domain
    mode: Literal["merge", "replace"] = "merge"
    content: str = Field(..., max_length=5_000_000)


class RestoreBody(BaseModel):
    source: str = "startup"
    domains: list[Domain] | None = None


class SnapshotBody(BaseModel):
    label: str = Field("", max_length=80)


async def _sonic(lab: Any, name: str) -> SonicNode:
    """Routeur SONiC, déployé ou non (pour lire la conf de démarrage et les snapshots)."""
    node = await lab.get_node(name)
    if not isinstance(node, SonicNode):
        raise BadRequest(f"{name} n'est pas un routeur SONiC")
    return node


@router.get("/config")
async def get_config(
    name: str,
    lab: LabDep,
    domain: Domain = "configdb",
    source: str = Query("running", max_length=120),
) -> dict[str, Any]:
    node = await lab.router(name) if source == "running" else await _sonic(lab, name)
    return await node.get_config(domain, source)


@router.post("/config/preview")
async def preview(name: str, body: PushBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.router(name)
    return await node.preview(body.domain, body.mode, body.content)


@router.post("/config/push")
async def push(name: str, body: PushBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.router(name)
    async with lab.lock(name):
        res = await node.push(body.domain, body.mode, body.content)
    lab.invalidate(name)
    return res


@router.post("/config/save")
async def save(name: str, lab: LabDep) -> dict[str, Any]:
    """Running → conf de démarrage : lab/configs/<routeur>/, ou merge request en mode GitOps."""
    await lab.router(name)
    res = await lab.save_startup([name])
    return res["results"][name] if not res.get("gitops") else res


@router.post("/config/restore")
async def restore(name: str, body: RestoreBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.router(name)
    async with lab.lock(name):
        res = await node.restore(body.source, list(body.domains) if body.domains else None)
    lab.invalidate(name)
    return res


@router.get("/config/diff")
async def diff(name: str, lab: LabDep, against: str = Query("startup", max_length=120)) -> dict[str, Any]:
    node = await lab.router(name)
    return await node.diff_against(against)


@router.get("/snapshots")
async def list_snapshots(name: str, lab: LabDep) -> dict[str, Any]:
    await _sonic(lab, name)
    return {"snapshots": lab.store.list_snapshots(name)}


@router.post("/snapshots")
async def create_snapshot(name: str, lab: LabDep, body: SnapshotBody | None = None) -> dict[str, Any]:
    node = await lab.router(name)
    contents = await node.snapshot_contents()
    return lab.store.create_snapshot(name, contents, label=(body or SnapshotBody()).label)


@router.get("/snapshots/{snap_id}")
async def get_snapshot(name: str, snap_id: str, lab: LabDep) -> dict[str, Any]:
    await _sonic(lab, name)
    meta = lab.store.get_snapshot(name, snap_id)
    meta["contents"] = {d: lab.store.read_snapshot(name, snap_id, d) for d in DOMAINS if d in meta["domains"]}
    return meta


@router.delete("/snapshots/{snap_id}")
async def delete_snapshot(name: str, snap_id: str, lab: LabDep) -> dict[str, Any]:
    await _sonic(lab, name)
    lab.store.delete_snapshot(name, snap_id)
    return {"ok": True}
