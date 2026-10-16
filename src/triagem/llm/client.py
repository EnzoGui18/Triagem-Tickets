from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

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

        self._generate_url = f"{base_url.rstrip('/')}/v1/generate"
        self._api_key = api_key
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self._http_client = http_client

    async def generate(self, *, system: str, input_text: str) -> GenerationResult:
        request = GenerateRequest(model=self.model, system=system, input=input_text)
        if self._http_client is not None:
            response = await self._send(self._http_client, request)
        else:
            async with httpx.AsyncClient() as client:
                response = await self._send(client, request)
        return self._handle_response(response)

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

    def _handle_response(self, response: httpx.Response) -> GenerationResult:
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
        )
