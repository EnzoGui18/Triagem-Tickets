from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local request_limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local request_id = ARGV[3]

local redis_time = redis.call("TIME")
local now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
local cutoff_ms = now_ms - window_ms
redis.call("ZREMRANGEBYSCORE", key, "-inf", cutoff_ms)
local request_count = redis.call("ZCARD", key)

local allowed = 0
local retry_after_ms = 0
if request_count < request_limit then
    redis.call("ZADD", key, now_ms, request_id)
    allowed = 1
else
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    retry_after_ms = math.max(1, math.ceil(tonumber(oldest[2]) + window_ms - now_ms))
end

redis.call("PEXPIRE", key, window_ms * 2)

return {allowed, retry_after_ms}
"""

_WINDOW_MILLISECONDS = 1050


class RateLimiter(Protocol):
    async def acquire(self) -> None: ...


class RateLimiterError(Exception):
    """Base error raised by rate limiting coordination."""


class RateLimiterUnavailableError(RateLimiterError):
    """Redis could not coordinate a safe LLM request rate."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: float


class RedisSlidingWindowRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        key: str = "triagem:llm:rate-limit:v1",
        rate_per_second: int = 5,
    ) -> None:
        if not key:
            raise ValueError("key cannot be empty")
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")

        self._redis = redis
        self.key = key
        self.rate_per_second = rate_per_second

    async def try_acquire(self) -> RateLimitDecision:
        try:
            evaluation = self._redis.eval(
                _SLIDING_WINDOW_SCRIPT,
                1,
                self.key,
                str(self.rate_per_second),
                str(_WINDOW_MILLISECONDS),
                str(uuid4()),
            )
            raw_result = await cast(Awaitable[object], evaluation)
        except RedisError as error:
            raise RateLimiterUnavailableError("Redis rate limiter is unavailable") from error

        if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 2:
            raise RateLimiterError("Redis rate limiter returned an invalid result")

        values = cast(tuple[object, object] | list[object], raw_result)
        try:
            allowed = bool(int(cast(int | str, values[0])))
            retry_after_ms = int(cast(int | str, values[1]))
        except (TypeError, ValueError) as error:
            raise RateLimiterError("Redis rate limiter returned invalid values") from error

        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=max(0, retry_after_ms) / 1000,
        )

    async def acquire(self) -> None:
        while True:
            decision = await self.try_acquire()
            if decision.allowed:
                return
            await asyncio.sleep(max(0.001, decision.retry_after_seconds))
