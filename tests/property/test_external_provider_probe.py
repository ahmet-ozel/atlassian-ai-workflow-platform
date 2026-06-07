"""External Provider Probe Status Mapping.



Background
----------

External providers (vLLM, OpenAI, Anthropic, Firecrawl) are services
running outside the Compose stack. The admin-dashboard probes them via
HTTP to determine their availability. The:func:`~lifecycle.external_probe.probe_external` helper maps HTTP
responses to one of four statuses:

* ``200`` (expected) → ``"ok"``
* ``401`` / ``403`` → ``"unauthorized"``
* ``429`` → ``"rate_limited"``
* timeout / connection refused → ``"unreachable"``

The:func:`~lifecycle.external_probe.emit_probe_audit` function tracks
consecutive failures per provider and emits:

* ``external_provider_probe_failed`` on every non-ok probe.
* ``external_provider_streak_alert`` once when 3 consecutive failures
 accumulate (mirrors the ``health_streak_alert`` pattern).

Strategy
--------

We use Hypothesis to generate random HTTP response scenarios (status
codes, timeouts, connection errors) against a fake HTTP server
(httpx mock transport). The tests verify:

(a) **Status mapping correctness**: Each HTTP response code maps to
 the correct ``ExternalProbeStatus``.
(b) **Credential injection**: Providers requiring auth get the correct
 header; missing credentials yield ``"unauthorized"`` without
 making a request.
(c) **Audit emission**: Failed probes emit
 ``external_provider_probe_failed``; 3 consecutive failures emit
 ``external_provider_streak_alert``; recovery resets the streak.
(d) **Cache behaviour**: Repeated probes within 30s return cached
 results; ``bypass_cache=True`` forces a fresh probe.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose admin-dashboard-api src
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_ADMIN_DASHBOARD_API_SRC: Final[Path] = (
    _PLATFORM_ROOT / "services" / "admin-dashboard-api" / "src"
)

if _ADMIN_DASHBOARD_API_SRC.is_dir():
    _src_str = str(_ADMIN_DASHBOARD_API_SRC)
    if _src_str not in sys.path:
        sys.path.insert(0, _src_str)

from lifecycle.external_probe import (  # noqa: E402
    ExternalProbeResult,
    ExternalProbeStatus,
    EXTERNAL_PROVIDER_STREAK_THRESHOLD,
    _map_status,
    clear_cache,
    emit_probe_audit,
    probe_external,
    reset_streak_state,
)


# ---------------------------------------------------------------------------
# Fake Audit Writer - collects audit entries in memory
# ---------------------------------------------------------------------------


@dataclass
class FakeAuditEntry:
    """Simplified audit entry for test assertions."""

    action: str
    details_json: dict[str, Any]
    outcome: str = "failed"


class FakeAuditWriter:
    """In-memory audit writer that records all entries for assertions."""

    def __init__(self) -> None:
        self.entries: list[FakeAuditEntry] = []

    async def write_with_retry(self, entry: Any) -> None:
        self.entries.append(
            FakeAuditEntry(
                action=entry.action,
                details_json=entry.details_json,
                outcome=entry.outcome,
            )
        )

    def count_by_action(self, action: str) -> int:
        return sum(1 for e in self.entries if e.action == action)

    def clear(self) -> None:
        self.entries.clear()


# ---------------------------------------------------------------------------
# Fake HTTP Transport - simulates external provider responses
# ---------------------------------------------------------------------------


class FakeTransport(httpx.AsyncBaseTransport):
    """Mock transport that returns a pre-configured response or raises."""

    def __init__(
        self,
        *,
        status_code: int | None = None,
        raise_timeout: bool = False,
        raise_connection_error: bool = False,
    ) -> None:
        self._status_code = status_code
        self._raise_timeout = raise_timeout
        self._raise_connection_error = raise_connection_error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._raise_timeout:
            raise httpx.ReadTimeout("simulated timeout")
        if self._raise_connection_error:
            raise httpx.ConnectError("simulated connection refused")
        return httpx.Response(
            status_code=self._status_code or 200,
            request=request,
        )


# ---------------------------------------------------------------------------
# Helper: build manifest entry
# ---------------------------------------------------------------------------


def _make_entry(
    name: str = "vllm",
    base_url_env: str | None = None,
    base_url_default: str | None = "http://localhost:8000/v1",
    probe_path: str = "/models",
    probe_method: str = "GET",
    probe_expected_status: int = 200,
) -> dict[str, Any]:
    """Build a manifest entry dict for testing."""
    entry: dict[str, Any] = {
        "name": name,
        "kind": "external",
        "probe_path": probe_path,
        "probe_method": probe_method,
        "probe_expected_status": probe_expected_status,
    }
    if base_url_env:
        entry["base_url_env"] = base_url_env
    if base_url_default:
        entry["base_url_default"] = base_url_default
    return entry


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Provider names matching the manifest entries.
_provider_strategy = st.sampled_from(["vllm", "openai", "anthropic", "firecrawl-cloud"])

#: HTTP status codes that the probe might encounter.
_status_code_strategy = st.sampled_from([200, 401, 403, 429, 500, 502, 503])

#: Response scenario: either a status code, timeout, or connection error.
_response_scenario = st.one_of(
    st.tuples(st.just("status"), _status_code_strategy),
    st.tuples(st.just("timeout"), st.just(None)),
    st.tuples(st.just("connection_error"), st.just(None)),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset cache and streak state before each test."""
    clear_cache()
    reset_streak_state()
    yield
    clear_cache()
    reset_streak_state()


# ---------------------------------------------------------------------------
# invariant: Status Mapping Correctness
# ---------------------------------------------------------------------------


class TestStatusMappingCorrectness:
    """HTTP responses map to the correct probe status.

 Each HTTP response code maps to the correct ExternalProbeStatus.
 The _map_status function is the core mapping logic.
 """

    @settings(
        max_examples=200,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        status_code=st.integers(min_value=100, max_value=599),
        expected_status=st.just(200),
    )
    def test_status_mapping_deterministic(
        self, status_code: int, expected_status: int
    ) -> None:
        """,: HTTP status codes map deterministically to
 ExternalProbeStatus values."""

        result = _map_status(status_code, expected_status)

        if status_code == expected_status:
            assert result == "ok", (
                f"HTTP {status_code} (expected {expected_status}) must map to 'ok'"
            )
        elif status_code in (401, 403):
            assert result == "unauthorized", (
                f"HTTP {status_code} must map to 'unauthorized'"
            )
        elif status_code == 429:
            assert result == "rate_limited", (
                f"HTTP 429 must map to 'rate_limited'"
            )
        else:
            assert result == "unreachable", (
                f"HTTP {status_code} (unexpected) must map to 'unreachable'"
            )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        expected_status=st.sampled_from([200, 201, 204]),
    )
    def test_expected_status_configurable(self, expected_status: int) -> None:
        """: The expected status code is configurable per provider.
 Only the configured expected code maps to 'ok'."""

        assert _map_status(expected_status, expected_status) == "ok"
        # A different 2xx code should NOT map to ok if it's not the expected one
        other_2xx = 200 if expected_status != 200 else 201
        result = _map_status(other_2xx, expected_status)
        # 401/403/429 have special handling, other codes map to unreachable
        assert result == "unreachable"

    @pytest.mark.asyncio
    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        scenario=_response_scenario,
    )
    async def test_probe_external_maps_responses_correctly(
        self, scenario: tuple[str, int | None]
    ) -> None:
        """,: probe_external correctly maps HTTP responses,
 timeouts, and connection errors to the right status."""

        clear_cache()
        scenario_type, status_code = scenario

        transport = FakeTransport(
            status_code=status_code if scenario_type == "status" else None,
            raise_timeout=(scenario_type == "timeout"),
            raise_connection_error=(scenario_type == "connection_error"),
        )

        entry = _make_entry(name="test-provider")
        async with httpx.AsyncClient(transport=transport) as client:
            result = await probe_external(
                entry, http_client=client, bypass_cache=True
            )

        if scenario_type == "timeout":
            assert result.status == "unreachable"
            assert result.latency_ms is None
            assert "Timeout" in (result.error or "")
        elif scenario_type == "connection_error":
            assert result.status == "unreachable"
            assert result.latency_ms is None
            assert "Connection" in (result.error or "") or "Connect" in (result.error or "")
        else:
            expected = _map_status(status_code, 200)
            assert result.status == expected


# ---------------------------------------------------------------------------
# invariant: Credential Injection
# ---------------------------------------------------------------------------


class TestCredentialInjection:
    """Provider probes handle credentials before making requests.

 Providers requiring authentication get the correct header injected.
 Missing credentials yield 'unauthorized' without making a request.
 """

    @pytest.mark.asyncio
    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        provider=st.sampled_from(["openai", "anthropic", "firecrawl-cloud"]),
    )
    async def test_missing_credential_returns_unauthorized(
        self, provider: str
    ) -> None:
        """: When a provider requires auth but no API key is found,
 the result is 'unauthorized' without making an HTTP request."""

        clear_cache()

        # Ensure env vars are unset for this provider
        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "firecrawl-cloud": "FIRECRAWL_API_KEY",
        }
        env_var = env_var_map[provider]

        entry = _make_entry(
            name=provider,
            base_url_default="http://localhost:9999",
        )

        with patch.dict(os.environ, {env_var: ""}, clear=False):
            result = await probe_external(
                entry, vault_reader=None, bypass_cache=True
            )

        assert result.status == "unauthorized", (
            f"Provider '{provider}' with no API key must return 'unauthorized'"
        )
        assert result.latency_ms is None, (
            "No HTTP request should be made when credentials are missing"
        )

    @pytest.mark.asyncio
    async def test_vllm_no_credential_required(self) -> None:
        """: vLLM does not require authentication - probe proceeds
 without credential injection."""

        clear_cache()

        transport = FakeTransport(status_code=200)
        entry = _make_entry(
            name="vllm",
            base_url_default="http://localhost:8000/v1",
        )

        async with httpx.AsyncClient(transport=transport) as client:
            result = await probe_external(
                entry, http_client=client, bypass_cache=True
            )

        assert result.status == "ok", (
            "vLLM probe should succeed without credentials"
        )

    @pytest.mark.asyncio
    @settings(
        max_examples=30,
        deadline=10000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        provider=st.sampled_from(["openai", "anthropic", "firecrawl-cloud"]),
    )
    async def test_with_credential_probe_proceeds(self, provider: str) -> None:
        """: When credentials are available, the probe makes an
 HTTP request and returns the mapped status."""

        clear_cache()

        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "firecrawl-cloud": "FIRECRAWL_API_KEY",
        }
        env_var = env_var_map[provider]

        transport = FakeTransport(status_code=200)
        entry = _make_entry(
            name=provider,
            base_url_default="http://localhost:9999",
        )

        with patch.dict(os.environ, {env_var: "sk-test-key-12345"}, clear=False):
            async with httpx.AsyncClient(transport=transport) as client:
                result = await probe_external(
                    entry, http_client=client, bypass_cache=True
                )

        assert result.status == "ok", (
            f"Provider '{provider}' with valid credentials and 200 response "
            "must return 'ok'"
        )
        assert result.latency_ms is not None


# ---------------------------------------------------------------------------
# invariant: Audit Emission and Streak Alerting
# ---------------------------------------------------------------------------


class TestAuditEmissionAndStreakAlerting:
    """Failed probes emit audit entries and streak alerts.

 Failed probes emit ``external_provider_probe_failed`` audit entries.
 Three consecutive failures emit ``external_provider_streak_alert``.
 Recovery resets the streak counter.
 """

    @pytest.mark.asyncio
    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        status=st.sampled_from(["unreachable", "unauthorized", "rate_limited"]),
    )
    async def test_failed_probe_emits_audit(self, status: str) -> None:
        """: Every non-ok probe emits external_provider_probe_failed."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        result = ExternalProbeResult(
            name="test-provider",
            status=status,  # type: ignore[arg-type]
            base_url="http://localhost:8000",
            latency_ms=None,
            error="test error",
            last_probed_at=time.time(),
        )

        await emit_probe_audit(result, audit_writer=audit_writer)

        assert audit_writer.count_by_action("external_provider_probe_failed") == 1, (
            f"A probe with status '{status}' must emit "
            "'external_provider_probe_failed' audit"
        )

    @pytest.mark.asyncio
    async def test_ok_probe_does_not_emit_audit(self) -> None:
        """: A successful probe does not emit any audit entry."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        result = ExternalProbeResult(
            name="test-provider",
            status="ok",
            base_url="http://localhost:8000",
            latency_ms=42.5,
            error=None,
            last_probed_at=time.time(),
        )

        await emit_probe_audit(result, audit_writer=audit_writer)

        assert len(audit_writer.entries) == 0, (
            "A successful probe must not emit any audit entry"
        )

    @pytest.mark.asyncio
    async def test_streak_alert_after_three_consecutive_failures(self) -> None:
        """,: Three consecutive failures trigger a single
 external_provider_streak_alert audit entry."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        for i in range(EXTERNAL_PROVIDER_STREAK_THRESHOLD):
            result = ExternalProbeResult(
                name="failing-provider",
                status="unreachable",
                base_url="http://localhost:8000",
                latency_ms=None,
                error=f"failure #{i+1}",
                last_probed_at=time.time(),
            )
            await emit_probe_audit(result, audit_writer=audit_writer)

        # Should have N probe_failed entries + 1 streak_alert
        assert audit_writer.count_by_action(
            "external_provider_probe_failed"
        ) == EXTERNAL_PROVIDER_STREAK_THRESHOLD

        assert audit_writer.count_by_action(
            "external_provider_streak_alert"
        ) == 1, (
            f"After {EXTERNAL_PROVIDER_STREAK_THRESHOLD} consecutive failures, "
            "exactly one streak alert must be emitted"
        )

    @pytest.mark.asyncio
    async def test_streak_alert_not_repeated(self) -> None:
        """: The streak alert is emitted only once per streak -
 additional failures do not re-emit it."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        # Emit more failures than the threshold
        for i in range(EXTERNAL_PROVIDER_STREAK_THRESHOLD + 3):
            result = ExternalProbeResult(
                name="failing-provider",
                status="unreachable",
                base_url="http://localhost:8000",
                latency_ms=None,
                error=f"failure #{i+1}",
                last_probed_at=time.time(),
            )
            await emit_probe_audit(result, audit_writer=audit_writer)

        assert audit_writer.count_by_action(
            "external_provider_streak_alert"
        ) == 1, (
            "Streak alert must be emitted only once per streak, "
            "not on every subsequent failure"
        )

    @pytest.mark.asyncio
    async def test_recovery_resets_streak(self) -> None:
        """,: A successful probe resets the streak counter.
 After recovery, a new streak of failures triggers a new alert."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        # First streak: 3 failures → alert
        for i in range(EXTERNAL_PROVIDER_STREAK_THRESHOLD):
            result = ExternalProbeResult(
                name="flaky-provider",
                status="unreachable",
                base_url="http://localhost:8000",
                latency_ms=None,
                error=f"failure #{i+1}",
                last_probed_at=time.time(),
            )
            await emit_probe_audit(result, audit_writer=audit_writer)

        assert audit_writer.count_by_action("external_provider_streak_alert") == 1

        # Recovery
        ok_result = ExternalProbeResult(
            name="flaky-provider",
            status="ok",
            base_url="http://localhost:8000",
            latency_ms=50.0,
            error=None,
            last_probed_at=time.time(),
        )
        await emit_probe_audit(ok_result, audit_writer=audit_writer)

        # Second streak: 3 more failures → second alert
        for i in range(EXTERNAL_PROVIDER_STREAK_THRESHOLD):
            result = ExternalProbeResult(
                name="flaky-provider",
                status="rate_limited",
                base_url="http://localhost:8000",
                latency_ms=None,
                error=f"second streak failure #{i+1}",
                last_probed_at=time.time(),
            )
            await emit_probe_audit(result, audit_writer=audit_writer)

        assert audit_writer.count_by_action("external_provider_streak_alert") == 2, (
            "After recovery and a new streak, a second alert must be emitted"
        )

    @pytest.mark.asyncio
    @settings(
        max_examples=50,
        deadline=10000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        failure_count=st.integers(min_value=1, max_value=10),
    )
    async def test_streak_threshold_boundary(self, failure_count: int) -> None:
        """: Streak alert is emitted exactly when the threshold
 is reached - not before, not after (per streak)."""

        reset_streak_state()
        audit_writer = FakeAuditWriter()

        for i in range(failure_count):
            result = ExternalProbeResult(
                name="boundary-provider",
                status="unauthorized",
                base_url="http://localhost:8000",
                latency_ms=None,
                error=f"failure #{i+1}",
                last_probed_at=time.time(),
            )
            await emit_probe_audit(result, audit_writer=audit_writer)

        expected_alerts = (
            1 if failure_count >= EXTERNAL_PROVIDER_STREAK_THRESHOLD else 0
        )
        actual_alerts = audit_writer.count_by_action(
            "external_provider_streak_alert"
        )
        assert actual_alerts == expected_alerts, (
            f"With {failure_count} failures (threshold={EXTERNAL_PROVIDER_STREAK_THRESHOLD}), "
            f"expected {expected_alerts} streak alerts but got {actual_alerts}"
        )


# ---------------------------------------------------------------------------
# invariant: Cache Behaviour
# ---------------------------------------------------------------------------


class TestCacheBehaviour:
    """Probe results are cached and can be bypassed explicitly.

 Probe results are cached for 30 seconds. Repeated probes within
 the TTL return cached results. bypass_cache forces a fresh probe.
 """

    @pytest.mark.asyncio
    async def test_cached_result_returned_within_ttl(self) -> None:
        """: Within the 30s TTL, probe_external returns the cached
 result without making a new HTTP request."""

        clear_cache()

        # First probe - returns 200
        transport_ok = FakeTransport(status_code=200)
        entry = _make_entry(name="cache-test-provider")

        async with httpx.AsyncClient(transport=transport_ok) as client:
            result1 = await probe_external(entry, http_client=client)

        assert result1.status == "ok"

        # Second probe - transport would return 500, but cache should
        # return the previous 200 result
        transport_fail = FakeTransport(status_code=500)
        async with httpx.AsyncClient(transport=transport_fail) as client:
            result2 = await probe_external(entry, http_client=client)

        assert result2.status == "ok", (
            "Within cache TTL, the cached 'ok' result must be returned"
        )
        assert result2.last_probed_at == result1.last_probed_at

    @pytest.mark.asyncio
    async def test_bypass_cache_forces_fresh_probe(self) -> None:
        """: bypass_cache=True skips the cache and makes a fresh
 HTTP request."""

        clear_cache()

        # First probe - returns 200
        transport_ok = FakeTransport(status_code=200)
        entry = _make_entry(name="bypass-test-provider")

        async with httpx.AsyncClient(transport=transport_ok) as client:
            result1 = await probe_external(entry, http_client=client)

        assert result1.status == "ok"

        # Second probe with bypass_cache - should hit the new transport
        transport_fail = FakeTransport(status_code=429)
        async with httpx.AsyncClient(transport=transport_fail) as client:
            result2 = await probe_external(
                entry, http_client=client, bypass_cache=True
            )

        assert result2.status == "rate_limited", (
            "With bypass_cache=True, a fresh probe must be made"
        )

    @pytest.mark.asyncio
    async def test_no_base_url_returns_unreachable(self) -> None:
        """: When no base URL is configured (env unset, no default),
 the result is 'unreachable' with a descriptive error."""

        clear_cache()

        entry = _make_entry(
            name="no-url-provider",
            base_url_env="NONEXISTENT_ENV_VAR_12345",
            base_url_default=None,
        )

        with patch.dict(os.environ, {}, clear=False):
            # Ensure the env var doesn't exist
            os.environ.pop("NONEXISTENT_ENV_VAR_12345", None)
            result = await probe_external(entry, bypass_cache=True)

        assert result.status == "unreachable"
        assert "No base URL" in (result.error or "")
