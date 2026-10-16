from __future__ import annotations

from redis.asyncio import Redis

from triagem.core.config import get_settings

settings = get_settings()
redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


async def check_redis() -> None:
    await redis_client.ping()
