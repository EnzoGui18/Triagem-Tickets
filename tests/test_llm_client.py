import json

import httpx
import pytest

from triagem.llm.client import (
    LLMAuthenticationError,
    LLMClient,
    LLMHTTPError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientProviderError,
    LLMTransportError,
)


def _success_payload() -> dict[str, object]:
    return {
        "id": "gen_123",
        "model": "triagem-sim-1",
        "output_text": '{"categoria":"outros"}',
        "usage": {"input_tokens": 42, "output_tokens": 9},
    }


async def test_client_sends_official_contract_and_captures_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://llm-sim:8080/v1/generate"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "triagem-sim-1",
            "system": "system instructions",
            "input": "untrusted ticket data",
        }
        return httpx.Response(200, json=_success_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            base_url="http://llm-sim:8080/",
            api_key="test-key",
            timeout_seconds=9,
            http_client=http_client,
        )
        result = await client.generate(
            system="system instructions",
            input_text="untrusted ticket data",
        )

    assert result.status_code == 200
    assert result.id == "gen_123"
    assert result.model == "triagem-sim-1"
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 9


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (400, LLMInvalidRequestError),
        (401, LLMAuthenticationError),
        (429, LLMRateLimitError),
        (500, LLMTransientProviderError),
        (503, LLMTransientProviderError),
        (418, LLMHTTPError),
    ],
)
async def test_client_maps_http_failures_to_domain_errors(
    status_code: int,
    expected_error: type[LLMHTTPError],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"type": "provider_error", "message": "provider message"}},
            headers={"Retry-After": "1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            base_url="http://llm-sim:8080",
            api_key="test-key",
            http_client=http_client,
        )
        with pytest.raises(expected_error) as captured:
            await client.generate(system="system", input_text="input")

    assert captured.value.status_code == status_code
    assert captured.value.error_type == "provider_error"
    assert captured.value.provider_message == "provider message"
    assert captured.value.retry_after == "1"


async def test_client_maps_httpx_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            base_url="http://llm-sim:8080",
            api_key="test-key",
            timeout_seconds=0.1,
            http_client=http_client,
        )
        with pytest.raises(LLMTimeoutError):
            await client.generate(system="system", input_text="input")


async def test_client_maps_httpx_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            base_url="http://llm-sim:8080",
            api_key="test-key",
            http_client=http_client,
        )
        with pytest.raises(LLMTransportError):
            await client.generate(system="system", input_text="input")


async def test_client_rejects_invalid_success_response() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "gen_without_required_fields"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMClient(
            base_url="http://llm-sim:8080",
            api_key="test-key",
            http_client=http_client,
        )
        with pytest.raises(LLMInvalidResponseError):
            await client.generate(system="system", input_text="input")
