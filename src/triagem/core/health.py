from __future__ import annotations

import asyncio

from triagem.core.database import check_postgresql
from triagem.core.redis import check_redis


async def get_readiness_status() -> dict[str, bool]:
    results = await asyncio.gather(check_postgresql(), check_redis(), return_exceptions=True)
    return {
        "postgresql": not isinstance(results[0], BaseException),
        "redis": not isinstance(results[1], BaseException),
    }
