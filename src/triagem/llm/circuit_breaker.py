from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitPermit:
    state: CircuitState
    generation: int

    @property
    def is_probe(self) -> bool:
        return self.state == CircuitState.HALF_OPEN


class CircuitBreaker(Protocol):
    recovery_timeout_seconds: float

    async def acquire(self) -> CircuitPermit: ...

    async def record_success(self, permit: CircuitPermit) -> None: ...

    async def record_failure(self, permit: CircuitPermit) -> None: ...


class CircuitBreakerError(Exception):
    """Base error raised by circuit-breaker coordination."""


class CircuitBreakerUnavailableError(CircuitBreakerError):
    """Redis could not coordinate the provider circuit safely."""


class CircuitOpenError(CircuitBreakerError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        super().__init__("LLM provider circuit is open")
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class CircuitProbeFailureError(CircuitBreakerError):
    def __init__(self, *, retry_after_seconds: float) -> None:
        super().__init__("LLM provider half-open probe failed")
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class AlwaysClosedCircuitBreaker:
    """No-op implementation for isolated client use and tests."""

    recovery_timeout_seconds = 0.0

    async def acquire(self) -> CircuitPermit:
        return CircuitPermit(state=CircuitState.CLOSED, generation=0)

    async def record_success(self, permit: CircuitPermit) -> None:
        del permit

    async def record_failure(self, permit: CircuitPermit) -> None:
        del permit


_ACQUIRE_SCRIPT = """
local key = KEYS[1]
local recovery_ms = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])
local supplied_now_ms = ARGV[3]

local now_ms
if supplied_now_ms ~= "" then
    now_ms = tonumber(supplied_now_ms)
else
    local redis_time = redis.call("TIME")
    now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
end

local state = redis.call("HGET", key, "state") or "closed"
local generation = tonumber(redis.call("HGET", key, "generation") or "0")

if state == "closed" then
    redis.call("HSET", key, "state", "closed", "generation", generation)
    redis.call("PEXPIRE", key, ttl_ms)
    return {1, "closed", generation, 0}
end

local state_started_at = tonumber(redis.call("HGET", key, "state_started_at_ms") or now_ms)
local elapsed_ms = math.max(0, now_ms - state_started_at)

if state == "open" then
    if elapsed_ms < recovery_ms then
        redis.call("PEXPIRE", key, ttl_ms)
        return {0, "open", generation, recovery_ms - elapsed_ms}
    end

    generation = generation + 1
    redis.call(
        "HSET",
        key,
        "state", "half_open",
        "generation", generation,
        "state_started_at_ms", now_ms
    )
    redis.call("PEXPIRE", key, ttl_ms)
    return {1, "half_open", generation, 0}
end

if state == "half_open" then
    if elapsed_ms >= recovery_ms then
        generation = generation + 1
        redis.call(
            "HSET",
            key,
            "state", "half_open",
            "generation", generation,
            "state_started_at_ms", now_ms
        )
        redis.call("PEXPIRE", key, ttl_ms)
        return {1, "half_open", generation, 0}
    end

    redis.call("PEXPIRE", key, ttl_ms)
    return {0, "half_open", generation, recovery_ms - elapsed_ms}
end

return redis.error_reply("invalid circuit state")
"""


_SUCCESS_SCRIPT = """
local key = KEYS[1]
local permit_state = ARGV[1]
local permit_generation = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])

local state = redis.call("HGET", key, "state") or "closed"
local generation = tonumber(redis.call("HGET", key, "generation") or "0")
if generation ~= permit_generation or state ~= permit_state then
    return 0
end

generation = generation + 1
redis.call(
    "HSET",
    key,
    "state", "closed",
    "failure_count", 0,
    "generation", generation
)
redis.call("HDEL", key, "state_started_at_ms")
redis.call("PEXPIRE", key, ttl_ms)
return 1
"""


_FAILURE_SCRIPT = """
local key = KEYS[1]
local permit_state = ARGV[1]
local permit_generation = tonumber(ARGV[2])
local failure_threshold = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local supplied_now_ms = ARGV[5]

local now_ms
if supplied_now_ms ~= "" then
    now_ms = tonumber(supplied_now_ms)
else
    local redis_time = redis.call("TIME")
    now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
end

local state = redis.call("HGET", key, "state") or "closed"
local generation = tonumber(redis.call("HGET", key, "generation") or "0")
if generation ~= permit_generation or state ~= permit_state then
    return 0
end

if state == "half_open" then
    generation = generation + 1
    redis.call(
        "HSET",
        key,
        "state", "open",
        "failure_count", failure_threshold,
        "generation", generation,
        "state_started_at_ms", now_ms
    )
    redis.call("PEXPIRE", key, ttl_ms)
    return 1
end

local failure_count = tonumber(redis.call("HGET", key, "failure_count") or "0") + 1
if failure_count >= failure_threshold then
    generation = generation + 1
    redis.call(
        "HSET",
        key,
        "state", "open",
        "failure_count", failure_count,
        "generation", generation,
        "state_started_at_ms", now_ms
    )
else
    redis.call("HSET", key, "failure_count", failure_count)
end
redis.call("PEXPIRE", key, ttl_ms)
return 1
"""


class RedisCircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        *,
        key: str = "triagem:llm:circuit-breaker:v1",
        failure_threshold: int = 2,
        recovery_timeout_seconds: float = 10.0,
        clock_milliseconds: Callable[[], int] | None = None,
    ) -> None:
        if not key:
            raise ValueError("key cannot be empty")
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")

        self._redis = redis
        self.key = key
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._recovery_milliseconds = max(1, round(recovery_timeout_seconds * 1000))
        self._ttl_milliseconds = max(60_000, self._recovery_milliseconds * 10)
        self._clock_milliseconds = clock_milliseconds

    def _now_argument(self) -> str:
        if self._clock_milliseconds is None:
            return ""
        return str(self._clock_milliseconds())

    async def acquire(self) -> CircuitPermit:
        raw_result = await self._evaluate(
            _ACQUIRE_SCRIPT,
            str(self._recovery_milliseconds),
            str(self._ttl_milliseconds),
            self._now_argument(),
        )
        if not isinstance(raw_result, (list, tuple)) or len(raw_result) != 4:
            raise CircuitBreakerError("Redis circuit breaker returned an invalid decision")

        try:
            allowed = bool(int(cast(int | str, raw_result[0])))
            state = CircuitState(self._decode(raw_result[1]))
            generation = int(cast(int | str, raw_result[2]))
            retry_after_ms = int(cast(int | str, raw_result[3]))
        except (TypeError, ValueError) as error:
            raise CircuitBreakerError(
                "Redis circuit breaker returned invalid decision values"
            ) from error

        if not allowed:
            raise CircuitOpenError(retry_after_seconds=max(0, retry_after_ms) / 1000)
        return CircuitPermit(state=state, generation=generation)

    async def record_success(self, permit: CircuitPermit) -> None:
        await self._evaluate(
            _SUCCESS_SCRIPT,
            permit.state.value,
            str(permit.generation),
            str(self._ttl_milliseconds),
        )

    async def record_failure(self, permit: CircuitPermit) -> None:
        await self._evaluate(
            _FAILURE_SCRIPT,
            permit.state.value,
            str(permit.generation),
            str(self.failure_threshold),
            str(self._ttl_milliseconds),
            self._now_argument(),
        )

    async def _evaluate(self, script: str, *arguments: str) -> object:
        try:
            evaluation = self._redis.eval(script, 1, self.key, *arguments)
            return await cast(Awaitable[object], evaluation)
        except RedisError as error:
            raise CircuitBreakerUnavailableError(
                "Redis circuit breaker is unavailable"
            ) from error

    @staticmethod
    def _decode(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        raise TypeError("circuit state must be text")
