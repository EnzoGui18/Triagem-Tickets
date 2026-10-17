import asyncio
import os
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from triagem.llm.rate_limiter import RedisSlidingWindowRateLimiter


@pytest_asyncio.fixture
async def independent_redis_clients() -> AsyncIterator[tuple[Redis, Redis]]:
    redis_url = os.getenv("REDIS_URL")
    if redis_url is None:
        pytest.skip("REDIS_URL is required for distributed rate limiter tests")

    first = Redis.from_url(redis_url, decode_responses=True)
    second = Redis.from_url(redis_url, decode_responses=True)
    await first.ping()
    try:
        yield first, second
    finally:
        await first.aclose()
        await second.aclose()


async def test_first_five_calls_are_immediate_and_sixth_waits(
    independent_redis_clients: tuple[Redis, Redis],
) -> None:
    redis, _ = independent_redis_clients
    key = f"test:llm-rate-limit:{uuid4()}"
    limiter = RedisSlidingWindowRateLimiter(redis, key=key, rate_per_second=5)
    try:
        first_decisions = [await limiter.try_acquire() for _ in range(5)]
        sixth_decision = await limiter.try_acquire()

        assert all(decision.allowed for decision in first_decisions)
        assert sixth_decision.allowed is False
        assert 1 <= sixth_decision.retry_after_seconds <= 1.05

        started_at = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - started_at
        assert elapsed >= max(0, sixth_decision.retry_after_seconds - 0.03)
    finally:
        await redis.delete(key)


async def test_independent_workers_share_the_same_window(
    independent_redis_clients: tuple[Redis, Redis],
) -> None:
    first_redis, second_redis = independent_redis_clients
    key = f"test:llm-rate-limit:{uuid4()}"
    first_worker = RedisSlidingWindowRateLimiter(
        first_redis,
        key=key,
        rate_per_second=5,
    )
    second_worker = RedisSlidingWindowRateLimiter(
        second_redis,
        key=key,
        rate_per_second=5,
    )
    try:
        decisions = await asyncio.gather(
            *(first_worker.try_acquire() for _ in range(5)),
            *(second_worker.try_acquire() for _ in range(5)),
        )

        assert sum(decision.allowed for decision in decisions) == 5
        assert sum(not decision.allowed for decision in decisions) == 5
    finally:
        await first_redis.delete(key)
