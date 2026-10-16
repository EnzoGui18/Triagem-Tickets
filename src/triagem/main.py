from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from triagem.api.health import router as health_router
from triagem.api.tickets import router as tickets_router
from triagem.core.config import get_settings
from triagem.core.database import engine
from triagem.core.redis import redis_client


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await redis_client.aclose()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(tickets_router)
    return application


app = create_app()
