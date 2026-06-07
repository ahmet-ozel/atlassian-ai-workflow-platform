"""- ConnectionTestResult shape covers timeout, non-2xx, 2xx.
spec. Three upstream outcomes are exercised:
* **Slow handler** exceeding the budget → result is
  ``success=false, latency_ms=10000, model=null,
  error.message="timeout"``.
* **Random non-2xx** with a body containing credential markers →
  result has the upstream status code on ``error.status_code`` and an
  ``error.message`` that is (a) ≤ 200 characters and (b) carries no
  unredacted credential pattern.
* **2xx with model echo** → ``success=true``, ``error=None``,
  ``model`` equal to the upstream-echoed identifier."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from hypothesis import given, settings, strategies as st


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.llm_providers.connection_tester import (  # noqa: E402
    ConnectionTester,
    TestRequest,
)


_CREDENTIAL_MARKERS = (
    "sk-ant-abcdef1234567890ABCDEF",
    "sk-proj-1234567890abcdefABCDEF",
    "sk-1234567890abcdefABCDEF1234",
    "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
)


def _run_with_handler(
    handler, req: TestRequest, *, budget: float = 10.0
):
    async def _go():
        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        try:
            tester = ConnectionTester(client, budget_seconds=budget)
            return await tester.run(req)
        finally:
            await client.aclose()

    return asyncio.new_event_loop().run_until_complete(_go())


def test_timeout_path_returns_spec_envelope() -> None:
    """A handler exceeding the budget returns the timeout envelope."""

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.0)
        return httpx.Response(200, json={"model": "x"})

    req = TestRequest(
        provider_type="openai",
        model="gpt-4o-mini",
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        provider_id=uuid4(),
    )
    # Use a 0.05s budget so the test finishes in milliseconds while
    # still exercising the ``asyncio.wait_for`` cancellation path.
    result = _run_with_handler(slow_handler, req, budget=0.05)
    assert result.success is False
    assert result.model is None
    assert result.error is not None
    assert result.error.message == "timeout"
    #  The budget * 1000 is what surfaces on timeout per.
    assert result.latency_ms == int(0.05 * 1000)


@given(
    status=st.sampled_from([400, 401, 403, 429, 500, 502, 503]),
    marker=st.sampled_from(_CREDENTIAL_MARKERS),
)
@settings(max_examples=100, deadline=None)
def test_non_2xx_returns_redacted_error_message(
    status: int, marker: str
) -> None:
    """Non-2xx responses surface the status + redacted body, ≤ 200 chars."""

    def handler(_request: httpx.Request) -> httpx.Response:
        # Embed the credential marker inside a long body so the
        # truncation + redaction paths both run.
        body = (
            "an upstream error with key " + marker + " trailing context " * 5
        )
        return httpx.Response(status, text=body)

    req = TestRequest(
        provider_type="openai",
        model="gpt-4o-mini",
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        provider_id=uuid4(),
    )
    result = _run_with_handler(handler, req)
    assert result.success is False
    assert result.error is not None
    assert result.error.status_code == status
    assert len(result.error.message) <= 200
    assert marker not in result.error.message


@given(
    provider_type=st.sampled_from(
        ["openai", "anthropic", "gemini", "vllm"]
    ),
    echo_model=st.sampled_from(
        ["echoed-model-1", "echoed-model-2", "gpt-4o-mini-2024-07-18"]
    ),
)
@settings(max_examples=100, deadline=None)
def test_2xx_returns_model_echo(provider_type: str, echo_model: str) -> None:
    """2xx with model echo → ``success=true`` and the echoed model surfaces."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # vLLM ``/models`` shape.
            return httpx.Response(
                200, json={"data": [{"id": echo_model}]}
            )
        return httpx.Response(200, json={"model": echo_model})

    req = TestRequest(
        provider_type=provider_type,  # type: ignore[arg-type]
        model="configured-model",
        base_url="http://upstream:8000" if provider_type == "vllm" else None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        provider_id=uuid4(),
    )
    result = _run_with_handler(handler, req)
    assert result.success is True
    assert result.error is None
    assert result.model == echo_model


def test_transport_error_returns_redacted_envelope() -> None:
    """Transport-level errors fall through to the redacted-shape envelope."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure: vault unreachable")

    req = TestRequest(
        provider_type="openai",
        model="gpt-4o-mini",
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
        provider_id=uuid4(),
    )
    result = _run_with_handler(handler, req)
    assert result.success is False
    assert result.error is not None
    assert result.error.status_code is None
    assert len(result.error.message) <= 200
