"""Routes par nœud : état détaillé, commandes, diagnostics, logs, bootstrap."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .deps import LabDep

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


class ExecBody(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    timeout: float = Field(30, ge=1, le=300)


class TargetBody(BaseModel):
    target: str = Field(..., min_length=1, max_length=253)
    count: int = Field(3, ge=1, le=20)


@router.get("/{name}")
async def node_status(name: str, lab: LabDep) -> dict[str, Any]:
    return await lab.node_status(name, summary=False)


@router.post("/{name}/exec")
async def node_exec(name: str, body: ExecBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.running_node(name)
    res = await node.run(body.command, timeout=body.timeout)
    lab.invalidate(name)
    return {"command": body.command, **res.to_dict(), "output": res.output}


@router.post("/{name}/ping")
async def node_ping(name: str, body: TargetBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.running_node(name)
    return await node.ping(body.target, body.count)


@router.post("/{name}/traceroute")
async def node_traceroute(name: str, body: TargetBody, lab: LabDep) -> dict[str, Any]:
    node = await lab.running_node(name)
    return await node.traceroute(body.target)


@router.get("/{name}/logs")
async def node_logs(name: str, lab: LabDep, lines: int = Query(200, ge=10, le=5000)) -> dict[str, Any]:
    node = await lab.running_node(name)
    return {"logs": await node.logs(lines)}


@router.post("/{name}/bootstrap")
async def node_bootstrap(name: str, lab: LabDep) -> dict[str, Any]:
    router_node = await lab.router(name)
    async with lab.lock(name):
        res = await router_node.bootstrap()
    lab.invalidate(name)
    return res


class WireBody(BaseModel):
    up: bool


@router.post("/{name}/wires/{interface}")
async def node_wire(name: str, interface: str, body: WireBody, lab: LabDep) -> dict[str, Any]:
    return await lab.set_wire(name, interface, body.up)
