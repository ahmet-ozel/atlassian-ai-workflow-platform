"""``Connection_Tester`` — per-provider upstream connectivity probe.

Implements the design's "Components › connection_tester.py" section:
a pure dispatcher keyed on ``provider_type`` that issues a single,
timeout-bounded round-trip per provider, extracts the upstream model
echo, and returns a structured :class:`ConnectionTestResult`.

The probe is intentionally cheap — vLLM gets a ``GET /models``; OpenAI,
Anthropic and Gemini get a single ``hi`` chat completion capped at 5
tokens — so operators can validate credentials without burning budget
(Requirements 7.* / 8.* / 5.4 — 5.6).

The tester **never** raises out to the caller; every exceptional path
(transport error, JSON decode failure, timeout) is converted into a
``success=false`` result. This keeps the router and service layer free
of provider-specific error-handling logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from http_shared.redaction import redact_text

from .schemas import ConnectionTestError, ConnectionTestResult, ProviderType


__all__ = [
    "TestRequest",
    "ConnectionTester",
    "TEST_PROMPT",
    "TOKEN_CAP",
    "DEFAULT_BUDGET_SECONDS",
]


_LOG = logging.getLogger(__name__)


#: Fixed test prompt (R8.1). The tester never accepts a caller-supplied
#: prompt — the operator's input is restricted to credentials and
#: configuration so a probe cannot drive a large bill or smuggle a
#: different prompt past the budget.
TEST_PROMPT: str = "hi"

#: Per-test token cap (R8.2). 5 tokens is enough for the provider to
#: confirm credential acceptance while keeping the marginal cost at or
#: below a tenth of a cent on every supported provider.
TOKEN_CAP: int = 5

#: Hard timeout for the entire round-trip (R5.5). Past this point the
#: in-flight HTTP request is cancelled (httpx honours cancellation) and
#: the tester returns a ``success=false`` result with
#: ``latency_ms=10000`` and ``error.message="timeout"``.
DEFAULT_BUDGET_SECONDS: float = 10.0


# Upstream default base URLs (overridable by the operator for vLLM
# and OpenAI; Anthropic / Gemini are pinned to the SaaS endpoints).
_DEFAULT_OPENAI_BASE: str = "https://api.openai.com"
_ANTHROPIC_BASE: str = "https://api.anthropic.com"
_GEMINI_BASE: str = "https://generativelanguage.googleapis.com/v1beta"

#: Max length of the error message surfaced via ``ConnectionTestError.message``
#: (R5.6 — keep payloads short so a misbehaving upstream cannot blow
#: up audit rows or UI toasts).
_MAX_ERROR_BODY: int = 200


@dataclass(frozen=True)
class TestRequest:
    """Minimal test-request shape consumed by :meth:`ConnectionTester.run`.

    Built by :mod:`llm_providers.service` from either the persisted
    provider config + Vault credentials (saved variant) or the unsaved
    request body (unsaved variant); the tester treats both identically.
    """

    provider_type: ProviderType
    model: str
    base_url: str | None
    api_key: str | None
    org_id: str | None
    provider_id: UUID | None = None  # for log correlation only


class ConnectionTester:
    """Per-provider connection probe.

    Construction takes the lifespan-managed shared
    :class:`httpx.AsyncClient` and a per-test budget; :meth:`run`
    dispatches to one of the four provider-specific branches based on
    :attr:`TestRequest.provider_type` and returns a
    :class:`ConnectionTestResult` envelope.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    ) -> None:
        self._client = http_client
        self._budget = float(budget_seconds)

    async def run(self, req: TestRequest) -> ConnectionTestResult:
        """Execute the probe and return a structured result.

        The whole dispatch is wrapped in :func:`asyncio.wait_for`; on
        timeout the result is the spec-mandated
        ``success=false, latency_ms=10000, model=null,
        error.message="timeout"`` envelope.
        """

        start = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._dispatch(req, start), timeout=self._budget
            )
        except asyncio.TimeoutError:
            return ConnectionTestResult(
                success=False,
                latency_ms=int(self._budget * 1000),
                model=None,
                error=ConnectionTestError(
                    status_code=None, message="timeout"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — tester never raises out
            _LOG.warning(
                "llm_provider_test_unexpected_error provider=%s err=%s",
                req.provider_type,
                exc.__class__.__name__,
            )
            return ConnectionTestResult(
                success=False,
                latency_ms=_elapsed_ms(start),
                model=None,
                error=ConnectionTestError(
                    status_code=None,
                    message=redact_text(_truncate(str(exc), _MAX_ERROR_BODY)),
                ),
            )

    # -----------------------------------------------------------------
    # Per-provider branches
    # -----------------------------------------------------------------

    async def _dispatch(
        self, req: TestRequest, start: float
    ) -> ConnectionTestResult:
        """Route the request to the matching provider branch."""

        if req.provider_type == "vllm":
            return await self._test_vllm(req, start)
        if req.provider_type == "openai":
            return await self._test_openai(req, start)
        if req.provider_type == "anthropic":
            return await self._test_anthropic(req, start)
        if req.provider_type == "gemini":
            return await self._test_gemini(req, start)
        # Pydantic guarantees this never fires; the branch exists so
        # static checkers can prove the function covers every provider.
        return ConnectionTestResult(
            success=False,
            latency_ms=_elapsed_ms(start),
            model=None,
            error=ConnectionTestError(
                status_code=None,
                message=f"unsupported provider {req.provider_type!r}",
            ),
        )

    async def _test_vllm(
        self, req: TestRequest, start: float
    ) -> ConnectionTestResult:
        base = (req.base_url or "").rstrip("/")
        url = f"{base}/models"
        headers: dict[str, str] = {}
        if req.api_key:
            headers["Authorization"] = f"Bearer {req.api_key}"
        response = await self._safe_request("GET", url, headers=headers)
        if isinstance(response, ConnectionTestResult):
            return response
        if not _is_success(response.status_code):
            return _non_2xx(response, start)
        # vLLM's ``/models`` returns ``{"data": [{"id": ...}, ...]}``.
        # The model echo lives on ``data[0].id``; fall back to the
        # configured model when the upstream payload is shaped
        # differently (e.g. an empty data array).
        model_echo = _extract_vllm_model_echo(response, req.model)
        self._log_token_usage(req, success=True)
        return ConnectionTestResult(
            success=True,
            latency_ms=_elapsed_ms(start),
            model=model_echo,
            error=None,
        )

    async def _test_openai(
        self, req: TestRequest, start: float
    ) -> ConnectionTestResult:
        base = (req.base_url or _DEFAULT_OPENAI_BASE).rstrip("/")
        url = f"{base}/v1/chat/completions"
        headers: dict[str, str] = {
            "Authorization": f"Bearer {req.api_key or ''}",
            "Content-Type": "application/json",
        }
        if req.org_id:
            headers["OpenAI-Organization"] = req.org_id
        body = {
            "model": req.model,
            "messages": [{"role": "user", "content": TEST_PROMPT}],
            "max_tokens": TOKEN_CAP,
        }
        response = await self._safe_request(
            "POST", url, headers=headers, json=body
        )
        if isinstance(response, ConnectionTestResult):
            return response
        if not _is_success(response.status_code):
            return _non_2xx(response, start)
        model_echo = _extract_top_level_model(response, req.model)
        self._log_token_usage(req, success=True)
        return ConnectionTestResult(
            success=True,
            latency_ms=_elapsed_ms(start),
            model=model_echo,
            error=None,
        )

    async def _test_anthropic(
        self, req: TestRequest, start: float
    ) -> ConnectionTestResult:
        url = f"{_ANTHROPIC_BASE}/v1/messages"
        headers = {
            "x-api-key": req.api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {
            "model": req.model,
            "max_tokens": TOKEN_CAP,
            "messages": [{"role": "user", "content": TEST_PROMPT}],
        }
        response = await self._safe_request(
            "POST", url, headers=headers, json=body
        )
        if isinstance(response, ConnectionTestResult):
            return response
        if not _is_success(response.status_code):
            return _non_2xx(response, start)
        model_echo = _extract_top_level_model(response, req.model)
        self._log_token_usage(req, success=True)
        return ConnectionTestResult(
            success=True,
            latency_ms=_elapsed_ms(start),
            model=model_echo,
            error=None,
        )

    async def _test_gemini(
        self, req: TestRequest, start: float
    ) -> ConnectionTestResult:
        # Gemini puts the API key in the query string per Google's
        # ``generativelanguage`` REST surface; the ``model`` lives in
        # the URL path component and the body never carries credentials.
        url = (
            f"{_GEMINI_BASE}/models/{req.model}:generateContent"
            f"?key={req.api_key or ''}"
        )
        headers = {"Content-Type": "application/json"}
        body = {
            "contents": [{"parts": [{"text": TEST_PROMPT}]}],
            "generationConfig": {"maxOutputTokens": TOKEN_CAP},
        }
        response = await self._safe_request(
            "POST", url, headers=headers, json=body
        )
        if isinstance(response, ConnectionTestResult):
            return response
        if not _is_success(response.status_code):
            return _non_2xx(response, start)
        model_echo = _extract_gemini_model_echo(response, req.model)
        self._log_token_usage(req, success=True)
        return ConnectionTestResult(
            success=True,
            latency_ms=_elapsed_ms(start),
            model=model_echo,
            error=None,
        )

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    async def _safe_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> "httpx.Response | ConnectionTestResult":
        """Issue an HTTP request, converting transport errors to results.

        Returns the :class:`httpx.Response` on success; on any
        :class:`httpx.TransportError` (DNS, TCP, TLS, read timeout)
        returns a pre-built :class:`ConnectionTestResult` so the
        per-provider branches can short-circuit cleanly.
        """

        request_start = time.monotonic()
        try:
            return await self._client.request(
                method, url, headers=headers, json=json
            )
        except httpx.TransportError as exc:
            return ConnectionTestResult(
                success=False,
                latency_ms=_elapsed_ms(request_start),
                model=None,
                error=ConnectionTestError(
                    status_code=None,
                    message=redact_text(
                        _truncate(
                            f"{exc.__class__.__name__}: {exc}",
                            _MAX_ERROR_BODY,
                        )
                    ),
                ),
            )

    def _log_token_usage(self, req: TestRequest, *, success: bool) -> None:
        """Emit a single INFO log line for token-usage observability (R7.6).

        The redaction filter installed on the root logger by
        ``main.py`` filters any inadvertent credential leak; the line
        here intentionally carries no key material.
        """

        _LOG.info(
            "llm_provider_test_token_usage provider=%s model=%s "
            "prompt_tokens=%d max_output_tokens=%d success=%s",
            req.provider_type,
            req.model,
            len(TEST_PROMPT),
            TOKEN_CAP,
            success,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_success(status_code: int) -> bool:
    return 200 <= status_code < 300


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _non_2xx(
    response: "httpx.Response", start: float
) -> ConnectionTestResult:
    """Build the documented non-2xx failure envelope (R5.6)."""

    try:
        body = response.text
    except Exception:  # pragma: no cover - httpx body decoding edge
        body = ""
    return ConnectionTestResult(
        success=False,
        latency_ms=_elapsed_ms(start),
        model=None,
        error=ConnectionTestError(
            status_code=response.status_code,
            message=redact_text(_truncate(body, _MAX_ERROR_BODY)),
        ),
    )


def _safe_json(response: "httpx.Response") -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _extract_vllm_model_echo(
    response: "httpx.Response", fallback: str
) -> str:
    """Pick ``data[0].id`` out of a vLLM ``/models`` response."""

    payload = _safe_json(response)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                identifier = first.get("id")
                if isinstance(identifier, str) and identifier:
                    return identifier
    return fallback


def _extract_top_level_model(
    response: "httpx.Response", fallback: str
) -> str:
    """Pick the top-level ``model`` field from an OpenAI / Anthropic response."""

    payload = _safe_json(response)
    if isinstance(payload, dict):
        value = payload.get("model")
        if isinstance(value, str) and value:
            return value
    return fallback


def _extract_gemini_model_echo(
    response: "httpx.Response", fallback: str
) -> str:
    """Pick ``modelVersion`` (fallback ``model``) from a Gemini response."""

    payload = _safe_json(response)
    if isinstance(payload, dict):
        for key in ("modelVersion", "model"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback
