"""What can be run, and with which options."""

from __future__ import annotations

from fastapi import APIRouter, Request

from kvasir.config import Settings
from kvasir.methods import METHODS
from kvasir.schemas.requests import MethodInfo

router = APIRouter()


@router.get("/v1/methods")
async def methods(http: Request) -> list[MethodInfo]:
    """Every method, with its options and their configured defaults, for a client to show."""
    settings: Settings = http.app.state.settings
    return [
        MethodInfo(
            name=method.name,
            clarifies=method.clarifies,
            stages=method.stages(method.Options()),
            options=method.schema(settings),
        )
        for method in METHODS.values()
    ]
