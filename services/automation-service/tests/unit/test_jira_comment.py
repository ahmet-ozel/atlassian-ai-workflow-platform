"""Unit tests for ``automation_service.budget.jira_comment``.

Validates: Requirements 5.6 (cost prediction comment) and 5.7
(global-fallback disclosure note) — task **7.4** of
``.kiro/specs/platform-mimari-ops/tasks.md``.

The tests exercise :func:`post_cost_prediction_comment` against:

* an in-memory :class:`AuditLogger` (so we can assert on the
  ``cost_prediction_comment_*`` event shape),
* a hand-rolled credential resolver (so we never touch Vault), and
* an ``httpx.MockTransport``-backed factory (so the MCP path stays
  in-process and deterministic).

The MCP wiring used by ``jira_comment.py`` mirrors the worker's
``jira_add_comment`` activity (same ``/mcp`` path, same
``X-Atlassian-Jira-*`` headers, same JSON-RPC envelope), so the
tests assert on the **wire shape** as the regression guardrail —
the Spec 1 R1.2 contract is "every Atlassian call goes through the
``atlassian_unified`` MCP", and the wire shape is the externally
observable manifestation of that contract.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_budget_policy.py so ``automation_service``,
# ``audit_logger``, and ``http_shared`` resolve without an editable install.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.budget.jira_comment import (  # noqa: E402
    CostCommentOutcome,
    post_cost_prediction_comment,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakePrediction:
    """Minimal :class:`CostPredictionLike` satisfier for tests."""

    predicted_usd: Decimal
    confidence_low: Decimal
    confidence_high: Decimal
    source: str


@dataclass(frozen=True, slots=True)
class _FakeAtlassianCredential:
    """Mirror of :class:`AtlassianCredential` (keeps the test self-contained)."""

    url: str = "https://example.atlassian.net"
    username: str = "bot.payment.jira"
    personal_token: str = "secret-pat-value"


class _FakeCredentialResolver:
    """Duck-typed :class:`CredentialResolver` for ``with_atlassian_creds``."""

    def __init__(
        self, credential: _FakeAtlassianCredential | None = None
    ) -> None:
        self._credential = credential or _FakeAtlassianCredential()
        self.calls: list[tuple[str, str, str]] = []

    async def get(
        self, dept_id: str, service: str, scope: str = "bot"
    ) -> _FakeAtlassianCredential:
        self.calls.append((dept_id, service, scope))
        return self._credential


@dataclass
class _RecordingAuditWriter:
    """Append-only writer compatible with :class:`AuditLogger`."""

    events: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


_FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN_NOW


# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------


def _mock_transport_factory(
    handler: "httpx._types.AsyncByteStream | Any",
) -> Any:
    """Return a mcp_client_factory wired around an ``httpx.MockTransport``.

    The returned callable matches the :data:`McpClientFactory`
    Protocol (``client_source=``, ``timeout=``, ``base_url=``) and
    constructs an :class:`httpx.AsyncClient` that uses the supplied
    handler instead of a real network roundtrip. The handler may be
    a callable or a ``MockTransport`` instance.
    """

    transport = (
        handler
        if isinstance(handler, httpx.MockTransport)
        else httpx.MockTransport(handler)
    )

    def factory(*, client_source: str, timeout: float, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            timeout=timeout,
            headers={"X-Client-Source": client_source},
        )

    return factory


def _ok_jsonrpc_handler(
    captured: list[httpx.Request],
) -> "Any":
    """Build a MockTransport handler that returns a successful JSON-RPC response."""

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            status_code=200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "comment created"}],
                    "isError": False,
                },
            },
        )

    return _handler


# ---------------------------------------------------------------------------
# Tests — body composition (R5.6, R5.7)
# ---------------------------------------------------------------------------


class TestBodyComposition:
    """The Jira body must follow the format from task 7.4 verbatim."""

    @pytest.mark.asyncio
    async def test_dept_source_renders_header_only(self) -> None:
        captured: list[httpx.Request] = []
        prediction = _FakePrediction(
            predicted_usd=Decimal("1.234"),  # quantises to "1.23"
            confidence_low=Decimal("0.80"),
            confidence_high=Decimal("1.50"),
            source="dept",
        )

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-100",
            prediction=prediction,
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            clock=_frozen_clock,
        )

        assert outcome.status == "posted"
        assert len(captured) == 1
        body = captured[0].read().decode()
        # The exact header line lives in the body; assert on its salient
        # markers individually so the test is robust to small wording
        # tweaks elsewhere (for instance, swapping the em-dash).
        assert "🤖 Tahmini maliyet: $1.23" in body
        assert "CI %80: $0.80–$1.50" in body
        assert "Kaynak: dept." in body
        # Global-fallback note must NOT appear for ``dept`` source.
        assert "global ortalamadan üretildi" not in body

    @pytest.mark.asyncio
    async def test_global_fallback_appends_disclosure_note(self) -> None:
        captured: list[httpx.Request] = []
        prediction = _FakePrediction(
            predicted_usd=Decimal("0.50"),
            confidence_low=Decimal("0.20"),
            confidence_high=Decimal("0.80"),
            source="global_fallback",
        )

        outcome = await post_cost_prediction_comment(
            issue_key="NEW-1",
            prediction=prediction,
            dept_id="newdept",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            clock=_frozen_clock,
        )

        assert outcome.status == "posted"
        body = captured[0].read().decode()
        assert "Kaynak: global_fallback." in body
        assert (
            "Bu departmanın geçmiş verisi henüz az; tahmin global "
            "ortalamadan üretildi."
        ) in body

    @pytest.mark.asyncio
    async def test_deeplink_appended_when_linker_resolves(self) -> None:
        captured: list[httpx.Request] = []
        prediction = _FakePrediction(
            predicted_usd=Decimal("2.00"),
            confidence_low=Decimal("1.50"),
            confidence_high=Decimal("2.50"),
            source="dept",
        )

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-200",
            prediction=prediction,
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            dept_cost_panel_linker=lambda d: f"https://admin.example/costs?dept_id={d}",
            clock=_frozen_clock,
        )

        assert outcome.status == "posted"
        body = captured[0].read().decode()
        assert (
            "Departman maliyet paneli: "
            "https://admin.example/costs?dept_id=payment"
        ) in body

    @pytest.mark.asyncio
    async def test_deeplink_skipped_when_linker_returns_none(self) -> None:
        captured: list[httpx.Request] = []
        outcome = await post_cost_prediction_comment(
            issue_key="PAY-300",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.10"),
                confidence_low=Decimal("0.05"),
                confidence_high=Decimal("0.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            dept_cost_panel_linker=lambda d: None,
            clock=_frozen_clock,
        )
        assert outcome.status == "posted"
        body = captured[0].read().decode()
        assert "Departman maliyet paneli" not in body

    @pytest.mark.asyncio
    async def test_misbehaving_linker_does_not_break_call(self) -> None:
        captured: list[httpx.Request] = []

        def _broken_linker(_dept_id: str) -> str:
            raise RuntimeError("DNS unavailable")

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-400",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.10"),
                confidence_low=Decimal("0.05"),
                confidence_high=Decimal("0.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            dept_cost_panel_linker=_broken_linker,
            clock=_frozen_clock,
        )
        # Linker failure must not turn a successful comment into a
        # "failed" outcome — the comment still went out.
        assert outcome.status == "posted"


# ---------------------------------------------------------------------------
# Tests — MCP wire shape (R1.2 parity with worker activity)
# ---------------------------------------------------------------------------


class TestMcpWireShape:
    """Assertions on the JSON-RPC envelope and credential headers."""

    @pytest.mark.asyncio
    async def test_post_targets_mcp_endpoint_with_jsonrpc_envelope(
        self,
    ) -> None:
        captured: list[httpx.Request] = []
        await post_cost_prediction_comment(
            issue_key="PAY-500",
            prediction=_FakePrediction(
                predicted_usd=Decimal("1.00"),
                confidence_low=Decimal("0.80"),
                confidence_high=Decimal("1.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            clock=_frozen_clock,
        )

        assert len(captured) == 1
        request = captured[0]
        # Endpoint must be ``/mcp`` — the streamable-http JSON-RPC path.
        assert request.url.path == "/mcp"
        assert request.method == "POST"
        envelope = json.loads(request.read())
        assert envelope["jsonrpc"] == "2.0"
        assert envelope["method"] == "tools/call"
        params = envelope["params"]
        assert params["name"] == "jira_add_comment"
        assert params["arguments"]["issue_key"] == "PAY-500"
        # The body field is the comment text — assert the expected
        # opening token; full-body assertions live in the body-composition
        # test class above.
        assert params["arguments"]["comment"].startswith("🤖 Tahmini maliyet:")

    @pytest.mark.asyncio
    async def test_credentials_injected_via_atlassian_jira_headers(
        self,
    ) -> None:
        captured: list[httpx.Request] = []
        cred = _FakeAtlassianCredential(
            url="https://payment.atlassian.net",
            username="bot.payment.jira",
            personal_token="pat-deadbeef",
        )

        await post_cost_prediction_comment(
            issue_key="PAY-600",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.40"),
                confidence_low=Decimal("0.20"),
                confidence_high=Decimal("0.60"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(cred),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            clock=_frozen_clock,
        )

        request = captured[0]
        # Headers must match :func:`http_shared.with_atlassian_creds`'s
        # canonical naming so the MCP routes the call as a Jira
        # request from the dept's bot.
        assert request.headers["X-Atlassian-Jira-Url"] == cred.url
        assert request.headers["X-Atlassian-Jira-Username"] == cred.username
        assert (
            request.headers["X-Atlassian-Jira-Personal-Token"]
            == cred.personal_token
        )
        # And the X-Client-Source identifier — set by the production
        # factory wrapper — must distinguish the caller from the
        # worker for MCP-side observability.
        assert request.headers["X-Client-Source"] == "automation-service"


# ---------------------------------------------------------------------------
# Tests — best-effort failure semantics (Spec 2 partition)
# ---------------------------------------------------------------------------


class TestBestEffortFailureSemantics:
    """Transport, credential, and MCP errors must be swallowed."""

    @pytest.mark.asyncio
    async def test_5xx_response_is_swallowed_and_audited(self) -> None:
        writer = _RecordingAuditWriter()

        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=503, text="MCP overloaded")

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-700",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.10"),
                confidence_low=Decimal("0.05"),
                confidence_high=Decimal("0.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(_handler),
            audit_logger=AuditLogger(writer=writer),
            clock=_frozen_clock,
        )

        # Best-effort: function must NOT raise.
        assert outcome.status == "failed"
        assert outcome.error is not None
        # Audit must record the failure exactly once.
        assert len(writer.events) == 1
        ev = writer.events[0]
        assert ev.action == "cost_prediction_comment_failed"
        assert ev.actor_role == "system"
        assert ev.dept_id == "payment"
        assert ev.resource == "jira:PAY-700"
        assert ev.result == "error"
        assert ev.payload is not None
        assert ev.payload["source"] == "dept"
        assert "error" in ev.payload

    @pytest.mark.asyncio
    async def test_jsonrpc_error_member_is_swallowed_and_audited(
        self,
    ) -> None:
        writer = _RecordingAuditWriter()

        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-800",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.10"),
                confidence_low=Decimal("0.05"),
                confidence_high=Decimal("0.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(_handler),
            audit_logger=AuditLogger(writer=writer),
            clock=_frozen_clock,
        )

        assert outcome.status == "failed"
        assert "Method not found" in (outcome.error or "")
        assert len(writer.events) == 1
        assert writer.events[0].action == "cost_prediction_comment_failed"

    @pytest.mark.asyncio
    async def test_credential_resolution_error_is_swallowed(self) -> None:
        writer = _RecordingAuditWriter()

        class _BrokenResolver:
            async def get(
                self, dept_id: str, service: str, scope: str = "bot"
            ) -> Any:
                raise RuntimeError(f"vault path missing for {dept_id}/{service}")

        outcome = await post_cost_prediction_comment(
            issue_key="PAY-900",
            prediction=_FakePrediction(
                predicted_usd=Decimal("0.10"),
                confidence_low=Decimal("0.05"),
                confidence_high=Decimal("0.20"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_BrokenResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            # Even with a working transport, the cred resolver fails
            # before the request is built. The handler should never
            # be invoked.
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler([])
            ),
            audit_logger=AuditLogger(writer=writer),
            clock=_frozen_clock,
        )

        assert outcome.status == "failed"
        assert len(writer.events) == 1
        assert writer.events[0].action == "cost_prediction_comment_failed"

    @pytest.mark.asyncio
    async def test_returns_skipped_when_prediction_is_none(self) -> None:
        writer = _RecordingAuditWriter()
        outcome = await post_cost_prediction_comment(
            issue_key="PAY-1000",
            prediction=None,
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler([])
            ),
            audit_logger=AuditLogger(writer=writer),
            clock=_frozen_clock,
        )
        assert outcome == CostCommentOutcome(
            status="skipped",
            issue_key="PAY-1000",
            body_chars=0,
            error=None,
        )
        assert len(writer.events) == 1
        assert writer.events[0].action == "cost_prediction_comment_skipped"
        assert writer.events[0].result == "ok"


# ---------------------------------------------------------------------------
# Tests — happy-path audit shape
# ---------------------------------------------------------------------------


class TestAuditShape:
    """The success audit row must carry the prediction summary."""

    @pytest.mark.asyncio
    async def test_posted_event_payload_carries_prediction_summary(
        self,
    ) -> None:
        writer = _RecordingAuditWriter()
        captured: list[httpx.Request] = []

        await post_cost_prediction_comment(
            issue_key="PAY-1100",
            prediction=_FakePrediction(
                predicted_usd=Decimal("1.234"),
                confidence_low=Decimal("0.800"),
                confidence_high=Decimal("1.500"),
                source="dept",
            ),
            dept_id="payment",
            credential_resolver=_FakeCredentialResolver(),
            mcp_base_url="http://atlassian-mcp:8090",
            mcp_client_factory=_mock_transport_factory(
                _ok_jsonrpc_handler(captured)
            ),
            audit_logger=AuditLogger(writer=writer),
            actor_id="user-42",
            clock=_frozen_clock,
        )

        assert len(writer.events) == 1
        ev = writer.events[0]
        assert ev.action == "cost_prediction_comment_posted"
        assert ev.actor_id == "user-42"
        assert ev.actor_role == "system"
        assert ev.dept_id == "payment"
        assert ev.resource == "jira:PAY-1100"
        assert ev.result == "ok"
        assert ev.timestamp == _FROZEN_NOW
        assert ev.payload is not None
        # Decimals must be quantised to two places before audit
        # serialisation so the ``/costs`` panel and the comment body
        # carry identical strings.
        assert ev.payload["predicted_usd"] == "1.23"
        assert ev.payload["confidence_low"] == "0.80"
        assert ev.payload["confidence_high"] == "1.50"
        assert ev.payload["source"] == "dept"
        assert ev.payload["body_chars"] > 0


# ---------------------------------------------------------------------------
# Tests — argument validation
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    """The function rejects empty issue_key / dept_id immediately."""

    @pytest.mark.asyncio
    async def test_empty_issue_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="issue_key"):
            await post_cost_prediction_comment(
                issue_key="",
                prediction=_FakePrediction(
                    predicted_usd=Decimal("0.10"),
                    confidence_low=Decimal("0.05"),
                    confidence_high=Decimal("0.20"),
                    source="dept",
                ),
                dept_id="payment",
                credential_resolver=_FakeCredentialResolver(),
                mcp_base_url="http://atlassian-mcp:8090",
            )

    @pytest.mark.asyncio
    async def test_empty_dept_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="dept_id"):
            await post_cost_prediction_comment(
                issue_key="PAY-1",
                prediction=_FakePrediction(
                    predicted_usd=Decimal("0.10"),
                    confidence_low=Decimal("0.05"),
                    confidence_high=Decimal("0.20"),
                    source="dept",
                ),
                dept_id="",
                credential_resolver=_FakeCredentialResolver(),
                mcp_base_url="http://atlassian-mcp:8090",
            )
