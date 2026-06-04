"""— Connection_Tester emits the exact upstream envelope per provider.
the project spec: the per-provider URL, method, headers
and body produced by :class:`ConnectionTester` exactly match the
design table. ``Authorization`` / ``OpenAI-Organization`` headers are
present iff the corresponding credential is non-empty."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from hypothesis import given, settings, strategies as st


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.llm_providers.connection_tester import (  # noqa: E402
    TOKEN_CAP,
    ConnectionTester,
    TestRequest,
    TEST_PROMPT,
)


def _capture_transport() -> tuple[
    httpx.MockTransport, list[httpx.Request]
]:
    """Return a transport that records every request and returns a 2xx echo."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            # vLLM /models — return a single-model list so the model
            # echo extraction has something to project from.
            return httpx.Response(
                200,
                json={"data": [{"id": "vllm-echo-model"}]},
            )
        return httpx.Response(200, json={"model": "echo-model"})

    return httpx.MockTransport(handler), captured


def _run(
    req: TestRequest,
) -> tuple[Any, list[httpx.Request]]:
    transport, captured = _capture_transport()
    client = httpx.AsyncClient(transport=transport)
    try:
        tester = ConnectionTester(client)
        result = asyncio.get_event_loop().run_until_complete(tester.run(req))
    finally:
        asyncio.get_event_loop().run_until_complete(client.aclose())
    return result, captured


@given(api_key=st.sampled_from(["", "sk-test-1234567890ABCDEFGH"]))
@settings(max_examples=100, deadline=None)
def test_vllm_uses_get_models_with_optional_bearer(api_key: str) -> None:
    """vLLM probe: ``GET {base_url}/models``, optional Bearer header."""

    req = TestRequest(
        provider_type="vllm",
        model="meta-llama/Llama-3.1-8B",
        base_url="http://vllm:8000",
        api_key=api_key or None,
        org_id=None,
        provider_id=uuid4(),
    )
    result, captured = _run(req)

    assert result.success is True
    assert len(captured) == 1
    sent = captured[0]
    assert sent.method == "GET"
    assert str(sent.url) == "http://vllm:8000/models"
    if api_key:
        assert sent.headers.get("Authorization") == f"Bearer {api_key}"
    else:
        assert "Authorization" not in sent.headers


@given(
    api_key=st.sampled_from(["sk-test-1234567890ABCDEFGH", "sk-live-xyz"]),
    org_id=st.sampled_from([None, "org-xyz", "org-payment-ops"]),
)
@settings(max_examples=100, deadline=None)
def test_openai_envelope_carries_credentials_and_token_cap(
    api_key: str, org_id: str | None
) -> None:
    """OpenAI probe: POST /v1/responses with fixed prompt + cap."""

    req = TestRequest(
        provider_type="openai",
        model="gpt-4o-mini",
        base_url=None,
        api_key=api_key,
        org_id=org_id,
        provider_id=uuid4(),
    )
    result, captured = _run(req)
    assert result.success is True
    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.openai.com/v1/responses"
    assert sent.headers.get("Authorization") == f"Bearer {api_key}"
    if org_id:
        assert sent.headers.get("OpenAI-Organization") == org_id
    else:
        assert "OpenAI-Organization" not in sent.headers
    body = json.loads(sent.content)
    assert body["model"] == "gpt-4o-mini"
    assert body["max_output_tokens"] == TOKEN_CAP
    assert body["input"] == TEST_PROMPT


@given(api_key=st.sampled_from(["sk-ant-abcdef1234567890ABCDEF"]))
@settings(max_examples=50, deadline=None)
def test_anthropic_envelope(api_key: str) -> None:
    """Anthropic probe: POST /v1/messages with x-api-key + version header."""

    req = TestRequest(
        provider_type="anthropic",
        model="claude-3-5-sonnet",
        base_url=None,
        api_key=api_key,
        org_id=None,
        provider_id=uuid4(),
    )
    _, captured = _run(req)
    sent = captured[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers.get("x-api-key") == api_key
    assert sent.headers.get("anthropic-version") == "2023-06-01"
    body = json.loads(sent.content)
    assert body["model"] == "claude-3-5-sonnet"
    assert body["max_tokens"] == TOKEN_CAP
    assert body["messages"] == [
        {"role": "user", "content": TEST_PROMPT}
    ]


@given(api_key=st.sampled_from(["AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"]))
@settings(max_examples=50, deadline=None)
def test_gemini_envelope(api_key: str) -> None:
    """Gemini probe: POST generateContent?key=<api_key>."""

    req = TestRequest(
        provider_type="gemini",
        model="gemini-1.5-flash",
        base_url=None,
        api_key=api_key,
        org_id=None,
        provider_id=uuid4(),
    )
    _, captured = _run(req)
    sent = captured[0]
    assert sent.method == "POST"
    url = str(sent.url)
    assert url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-1.5-flash:generateContent"
    )
    assert f"key={api_key}" in url
    body = json.loads(sent.content)
    assert body["contents"] == [{"parts": [{"text": TEST_PROMPT}]}]
    assert body["generationConfig"]["maxOutputTokens"] == TOKEN_CAP


def test_openai_forwards_tuning_for_capable_model() -> None:
    """gpt-5 model → probe body carries reasoning + verbosity knobs."""

    req = TestRequest(
        provider_type="openai",
        model="gpt-5.5",
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        reasoning_effort="high",
        verbosity="low",
        provider_id=uuid4(),
    )
    result, captured = _run(req)
    assert result.success is True
    body = json.loads(captured[0].content)
    assert body["reasoning"] == {"effort": "high"}
    assert body["text"] == {"verbosity": "low"}


def test_openai_omits_tuning_for_incapable_model() -> None:
    """gpt-4o-mini → tuning knobs are dropped even if configured."""

    req = TestRequest(
        provider_type="openai",
        model="gpt-4o-mini",
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        reasoning_effort="high",
        verbosity="low",
        provider_id=uuid4(),
    )
    result, captured = _run(req)
    assert result.success is True
    body = json.loads(captured[0].content)
    assert "reasoning" not in body
    assert "text" not in body
