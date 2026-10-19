from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import ValidationError

from triagem.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitPermit,
    CircuitProbeFailureError,
)
from triagem.llm.rate_limiter import RateLimiter
from triagem.llm.schemas import (
    GenerateRequest,
    GenerationResult,
    ProviderGenerationResponse,
)


class LLMClientError(Exception):
    """Base error raised by the LLM client."""


class LLMTransportError(LLMClientError):
    """The provider could not be reached."""


class LLMTimeoutError(LLMTransportError):
    """The provider request exceeded its configured timeout."""


class LLMHTTPError(LLMClientError):
    def __init__(
        self,
        *,
        status_code: int,
        error_type: str,
        provider_message: str,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"LLM provider returned HTTP {status_code}: {error_type}")
        self.status_code = status_code
        self.error_type = error_type
        self.provider_message = provider_message
        self.retry_after = retry_after


class LLMAuthenticationError(LLMHTTPError):
    """The provider rejected the configured credentials."""


class LLMInvalidRequestError(LLMHTTPError):
    """The provider rejected a non-retryable request."""


class LLMRateLimitError(LLMHTTPError):
    """The provider rate limit was exceeded."""


class LLMTransientProviderError(LLMHTTPError):
    """The provider returned a retryable server failure."""


class LLMInvalidResponseError(LLMClientError):
    """A successful provider response did not satisfy its response contract."""


def _error_details(response: httpx.Response) -> tuple[str, str]:
    error_type = "http_error"
    provider_message = "LLM provider request failed"
    try:
        payload: object = response.json()
    except ValueError:
        return error_type, provider_message

    if not isinstance(payload, dict):
        return error_type, provider_message
    error: Any = payload.get("error")
    if not isinstance(error, dict):
        return error_type, provider_message
    if isinstance(error.get("type"), str):
        error_type = error["type"]
    if isinstance(error.get("message"), str):
        provider_message = error["message"]
    return error_type, provider_message


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "triagem-sim-1",
        timeout_seconds: float = 10.0,
        rate_limiter: RateLimiter,
        circuit_breaker: CircuitBreaker,
        max_concurrency: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url cannot be empty")
        if not api_key:
            raise ValueError("api_key cannot be empty")
        if not model.strip():
            raise ValueError("model cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")

        self._generate_url = f"{base_url.rstrip('/')}/v1/generate"
        self._api_key = api_key
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self._rate_limiter = rate_limiter
        self._circuit_breaker = circuit_breaker
        self._concurrency_limiter = asyncio.Semaphore(max_concurrency)
        self._http_client = http_client

    async def generate(self, *, system: str, input_text: str) -> GenerationResult:
        request = GenerateRequest(model=self.model, system=system, input=input_text)
        async with self._concurrency_limiter:
            await self._rate_limiter.acquire()
            permit = await self._circuit_breaker.acquire()
            started_at = time.perf_counter()
            try:
                if self._http_client is not None:
                    response = await self._send(self._http_client, request)
                else:
                    async with httpx.AsyncClient() as client:
                        response = await self._send(client, request)
                latency_ms = max(0, round((time.perf_counter() - started_at) * 1000))
                result = self._handle_response(response, latency_ms=latency_ms)
            except (LLMTransientProviderError, LLMTransportError) as error:
                await self._record_failure(permit)
                if permit.is_probe:
                    raise CircuitProbeFailureError(
                        retry_after_seconds=self._circuit_breaker.recovery_timeout_seconds
                    ) from error
                raise
            except Exception:
                await self._record_success(permit)
                raise
            await self._record_success(permit)
            return result

    async def _record_success(self, permit: CircuitPermit) -> None:
        try:
            await self._circuit_breaker.record_success(permit)
        except CircuitBreakerError:
            # The provider result remains authoritative for this attempt. A later acquire
            # still fails closed if Redis coordination remains unavailable.
            return

    async def _record_failure(self, permit: CircuitPermit) -> None:
        try:
            await self._circuit_breaker.record_failure(permit)
        except CircuitBreakerError:
            # Preserve the original provider failure and let the durable job retry it.
            return

    async def _send(
        self,
        client: httpx.AsyncClient,
        request: GenerateRequest,
    ) -> httpx.Response:
        try:
            return await client.post(
                self._generate_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=request.model_dump(),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as error:
            raise LLMTimeoutError("LLM provider request timed out") from error
        except httpx.RequestError as error:
            raise LLMTransportError("LLM provider request failed") from error

    def _handle_response(self, response: httpx.Response, *, latency_ms: int) -> GenerationResult:
        if response.is_error:
            error_type, provider_message = _error_details(response)
            error_class: type[LLMHTTPError]
            if response.status_code in {401, 403}:
                error_class = LLMAuthenticationError
            elif response.status_code in {400, 422}:
                error_class = LLMInvalidRequestError
            elif response.status_code == 429:
                error_class = LLMRateLimitError
            elif response.status_code >= 500:
                error_class = LLMTransientProviderError
            else:
                error_class = LLMHTTPError
            raise error_class(
                status_code=response.status_code,
                error_type=error_type,
                provider_message=provider_message,
                retry_after=response.headers.get("Retry-After"),
            )

        try:
            payload: object = response.json()
            provider_response = ProviderGenerationResponse.model_validate(payload)
        except (ValueError, ValidationError) as error:
            raise LLMInvalidResponseError(
                "LLM provider returned an invalid success response"
            ) from error

        return GenerationResult(
            **provider_response.model_dump(),
            status_code=response.status_code,
            latency_ms=latency_ms,
        )
