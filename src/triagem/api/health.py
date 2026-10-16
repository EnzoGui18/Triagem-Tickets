from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from triagem.core.health import get_readiness_status

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["ok"]


class DependencyChecks(BaseModel):
    postgresql: Literal["ok", "error"]
    redis: Literal["ok", "error"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "error"]
    checks: DependencyChecks


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    return LivenessResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    checks: Annotated[dict[str, bool], Depends(get_readiness_status)],
) -> ReadinessResponse | JSONResponse:
    dependencies = DependencyChecks(
        postgresql="ok" if checks["postgresql"] else "error",
        redis="ok" if checks["redis"] else "error",
    )
    response = ReadinessResponse(
        status="ok" if all(checks.values()) else "error",
        checks=dependencies,
    )
    if response.status == "error":
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(),
        )
    return response
