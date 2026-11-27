"""Routes globales du lab : état, logs du déploiement, actions de lab."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from .deps import LabDep

router = APIRouter(prefix="/api", tags=["lab"])


class ConnectivityBody(BaseModel):
    count: int = Field(2, ge=1, le=10)


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@router.get("/lab")
async def lab_state(lab: LabDep) -> dict[str, Any]:
    return await lab.state()


@router.get("/lab/runner/logs")
async def runner_logs(lab: LabDep, tail: int = Query(300, ge=10, le=5000)) -> dict[str, Any]:
    return {"logs": await lab.runner_logs(tail)}


@router.post("/lab/start")
async def start(lab: LabDep) -> dict[str, Any]:
    return await lab.start()


@router.post("/lab/stop")
async def stop(lab: LabDep) -> dict[str, Any]:
    return await lab.stop()


@router.post("/lab/redeploy")
async def redeploy(lab: LabDep) -> dict[str, Any]:
    return await lab.redeploy()


@router.post("/lab/connectivity")
async def connectivity(lab: LabDep, body: ConnectivityBody | None = None) -> dict[str, Any]:
    return await lab.connectivity((body or ConnectivityBody()).count)


@router.post("/lab/save-all")
async def save_all(lab: LabDep) -> dict[str, Any]:
    return await lab.save_all()


@router.post("/lab/reset-all")
async def reset_all(lab: LabDep) -> dict[str, Any]:
    return await lab.reset_all()
