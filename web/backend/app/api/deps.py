"""Dépendances FastAPI partagées par les routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..lab import LabService


def get_lab(request: Request) -> LabService:
    return request.app.state.lab


LabDep = Annotated[LabService, Depends(get_lab)]
