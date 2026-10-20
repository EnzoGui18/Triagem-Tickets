from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from triagem.api.health import router as health_router
from triagem.api.tickets import router as tickets_router
from triagem.core.config import get_settings
from triagem.core.database import engine
from triagem.core.http import CorrelationIdMiddleware
from triagem.core.metrics import metrics_endpoint
from triagem.core.redis import redis_client
from triagem.core.structured_logging import configure_structured_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await redis_client.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_structured_logging(settings.log_level)
    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.add_middleware(CorrelationIdMiddleware)
    application.include_router(health_router)
    application.include_router(tickets_router)
    application.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    return application


app = create_app()
