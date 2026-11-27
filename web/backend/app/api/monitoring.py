"""Route de la métrologie : informations pour l'onglet « Métrologie » de l'interface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/monitoring")
async def monitoring(request: Request) -> dict[str, Any]:
    return await request.app.state.monitoring.info()
