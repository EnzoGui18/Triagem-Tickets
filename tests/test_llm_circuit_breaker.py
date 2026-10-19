import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from triagem.llm.circuit_breaker import (
    CircuitOpenError,
    CircuitState,
    RedisCircuitBreaker,
)


@pytest_asyncio.fixture
async def independent_redis_clients() -> AsyncIterator[tuple[Redis, Redis]]:
    redis_url = os.getenv("REDIS_URL")
    if redis_url is None:
        pytest.skip("REDIS_URL is required for distributed circuit-breaker tests")

    first = Redis.from_url(redis_url, decode_responses=True)
    second = Redis.from_url(redis_url, decode_responses=True)
    await first.ping()
    try:
        yield first, second
    finally:
        await first.aclose()
        await second.aclose()


async def test_closed_open_half_open_closed_transitions_are_global_and_deterministic(
    independent_redis_clients: tuple[Redis, Redis],
) -> None:
    first_redis, second_redis = independent_redis_clients
    key = f"test:llm-circuit-breaker:{uuid4()}"
    now_ms = 1_000_000
    first = RedisCircuitBreaker(
        first_redis,
        key=key,
        failure_threshold=2,
        recovery_timeout_seconds=10,
        clock_milliseconds=lambda: now_ms,
    )
    second = RedisCircuitBreaker(
        second_redis,
        key=key,
        failure_threshold=2,
        recovery_timeout_seconds=10,
        clock_milliseconds=lambda: now_ms,
    )
    try:
        first_failure = await first.acquire()
        assert first_failure.state == CircuitState.CLOSED
        await first.record_failure(first_failure)

        second_failure = await second.acquire()
        assert second_failure.state == CircuitState.CLOSED
        await second.record_failure(second_failure)

        with pytest.raises(CircuitOpenError) as opened:
            await first.acquire()
        assert opened.value.retry_after_seconds == 10

        now_ms += 10_000
        probe = await second.acquire()
        assert probe.state == CircuitState.HALF_OPEN

        with pytest.raises(CircuitOpenError) as probe_in_progress:
            await first.acquire()
        assert probe_in_progress.value.retry_after_seconds == 10

        await second.record_success(probe)
        recovered = await first.acquire()
        assert recovered.state == CircuitState.CLOSED
    finally:
        await first_redis.delete(key)


async def test_failed_half_open_probe_reopens_until_next_recovery_window(
    independent_redis_clients: tuple[Redis, Redis],
) -> None:
    redis, _ = independent_redis_clients
    key = f"test:llm-circuit-breaker:{uuid4()}"
    now_ms = 2_000_000
    breaker = RedisCircuitBreaker(
        redis,
        key=key,
        failure_threshold=1,
        recovery_timeout_seconds=4,
        clock_milliseconds=lambda: now_ms,
    )
    try:
        initial = await breaker.acquire()
        await breaker.record_failure(initial)

        now_ms += 4_000
        failed_probe = await breaker.acquire()
        assert failed_probe.state == CircuitState.HALF_OPEN
        await breaker.record_failure(failed_probe)

        with pytest.raises(CircuitOpenError) as reopened:
            await breaker.acquire()
        assert reopened.value.retry_after_seconds == 4

        now_ms += 4_000
        next_probe = await breaker.acquire()
        assert next_probe.state == CircuitState.HALF_OPEN
        assert next_probe.generation > failed_probe.generation
    finally:
        await redis.delete(key)
