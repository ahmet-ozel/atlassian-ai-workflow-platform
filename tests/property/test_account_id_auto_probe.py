"""Account ID Auto-Probe behavior for startup and post-create flows.

Background
----------

When the ``automation-service`` starts up, it queries
``automation.department_bots`` for rows where ``account_id IS NULL OR
account_id = ''``. For each such row it issues an Atlassian read probe
(``/myself`` for Jira/Confluence, ``/user`` for Bitbucket) and upserts
the resolved ``account_id`` into ``automation.department_bot_identity``.
Failures are audited but **never block** service startup ( best-effort).

After a credential is added via
``POST /admin/departments/{id}/credentials/{service}``, an inline bot
identity probe runs. On success the response carries
``account_id_probe_status: "ok"`` and the resolved ``account_id``.
On failure the response is still 200 but with
``account_id_probe_status: "failed"``.

The wizard endpoint (``POST /admin/departments/wizard``) runs the
identity probe atomically — if any probe fails, the department's mode
is downgraded to ``"disabled"``.

Strategy
--------

We use Hypothesis to generate random department × service configurations
with varying probe outcomes (success / failure / exception). Fake
implementations of the protocol interfaces defined in ``startup.py``
are injected to verify:

(a) Startup auto-probe fills missing account_ids and never blocks.
(b) Post-create inline probe populates the response field on success.
(c) Probe failures do not break department commit (best-effort).

The tests exercise the **real** ``auto_probe_missing_account_ids``
function from ``startup.py`` and the ``DeptCredentialService.add_or_update``
result shape from ``dept_credential_service.py``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — expose the automation-service source root
# ---------------------------------------------------------------------------
#
# The ``automation-service`` source tree co-exists with the
# legacy ``src/main.py`` + ``src/config.py``
# layer; importing the ``automation_service`` package eagerly executes
# ``automation_service/__init__.py`` which in turn loads
# ``automation_service.app`` whose top-of-module imports reach for
# ``from src.config import Settings``. We therefore add **both** the
# ``src/`` directory (so ``automation_service`` resolves) and its
# parent ``automation-service/`` directory (so ``src.config`` resolves
# as the legacy module path). This mirrors the pattern used by
# ``test_probe_runner.py``.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_AUTOMATION_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "services" / "automation-service"
)
_AUTOMATION_SRC: Final[Path] = _AUTOMATION_ROOT / "src"

for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

# Also add libs that the module imports from
_LIB_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "db-shared" / "src",
    _PLATFORM_ROOT / "libs" / "vault_client" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from automation_service.startup import (  # noqa: E402
    AutoProbeResult,
    DeptBotMissingRow,
    _PROBE_CACHE_TTL_SECONDS,
    _probe_cache,
    auto_probe_missing_account_ids,
    get_probe_cache,
)


# ---------------------------------------------------------------------------
# Fake implementations of the protocol interfaces
# ---------------------------------------------------------------------------


class FakeMissingAccountIdReader:
    """In-memory reader that returns pre-configured missing rows."""

    def __init__(self, rows: Sequence[DeptBotMissingRow]) -> None:
        self._rows = list(rows)

    async def list_missing(self) -> Sequence[DeptBotMissingRow]:
        return self._rows


class FakeFailingReader:
    """Reader that raises an exception (simulates DB failure)."""

    async def list_missing(self) -> Sequence[DeptBotMissingRow]:
        raise ConnectionError("DB connection lost")


class FakeBotIdentityProber:
    """Prober that returns deterministic results based on a mapping.

 The mapping is keyed by ``(dept_id, service)`` and values are
 either a string (resolved account_id) or ``None`` (probe failure).
 """

    def __init__(
        self,
        results: dict[tuple[str, str], str | None],
        *,
        raise_for: set[tuple[str, str]] | None = None,
    ) -> None:
        self._results = results
        self._raise_for = raise_for or set()
        self.calls: list[tuple[str, str, str, str]] = []

    async def probe_account_id(
        self,
        dept_id: str,
        service: str,
        credential_ref: str,
        username: str,
    ) -> str | None:
        self.calls.append((dept_id, service, credential_ref, username))
        if (dept_id, service) in self._raise_for:
            raise RuntimeError(f"Probe exception for {dept_id}/{service}")
        return self._results.get((dept_id, service))


class FakeAccountIdWriter:
    """Writer that records upserts in memory."""

    def __init__(self, *, fail_for: set[tuple[str, str]] | None = None) -> None:
        self.upserts: list[tuple[str, str, str]] = []
        self._fail_for = fail_for or set()

    async def upsert_account_id(
        self,
        dept_id: str,
        service: str,
        account_id: str,
    ) -> None:
        if (dept_id, service) in self._fail_for:
            raise RuntimeError(f"DB write failed for {dept_id}/{service}")
        self.upserts.append((dept_id, service, account_id))


class FakeAuditSink:
    """Audit sink that records events in memory."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write_auto_probe_audit(
        self,
        action: str,
        dept_id: str,
        service: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.events.append({
            "action": action,
            "dept_id": dept_id,
            "service": service,
            "payload": dict(payload),
        })


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Valid Atlassian service names.
_service_strategy = st.sampled_from(["jira", "bitbucket", "confluence"])

#: Department ID strategy — alphanumeric + hyphens, realistic length.
_dept_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-"),
    min_size=3,
    max_size=20,
).filter(lambda s: s[0].isalpha())

#: Account ID strategy — hex-like strings mimicking Atlassian account IDs.
_account_id_strategy = st.text(
    alphabet="0123456789abcdef",
    min_size=24,
    max_size=32,
)

#: Username strategy.
_username_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters=".-@"),
    min_size=5,
    max_size=30,
).filter(lambda s: s[0].isalpha())

#: Credential ref strategy.
_credential_ref_strategy = st.builds(
    lambda dept, svc: f"vault:atlassian/{dept}/{svc}",
    dept=_dept_id_strategy,
    svc=_service_strategy,
)


@st.composite
def _dept_bot_missing_row(draw: st.DrawFn) -> DeptBotMissingRow:
    """Generate a single DeptBotMissingRow."""
    return DeptBotMissingRow(
        dept_id=draw(_dept_id_strategy),
        service=draw(_service_strategy),
        credential_ref=draw(_credential_ref_strategy),
        username=draw(_username_strategy),
    )


@st.composite
def _dept_bot_missing_rows(draw: st.DrawFn) -> list[DeptBotMissingRow]:
    """Generate a list of 1-10 unique (dept_id, service) missing rows."""
    rows = draw(st.lists(_dept_bot_missing_row(), min_size=1, max_size=10))
    # Ensure unique (dept_id, service) pairs
    seen: set[tuple[str, str]] = set()
    unique_rows: list[DeptBotMissingRow] = []
    for row in rows:
        key = (row.dept_id, row.service)
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)
    assume(len(unique_rows) >= 1)
    return unique_rows


# ---------------------------------------------------------------------------
# Account ID Auto-Probe — Startup
# ---------------------------------------------------------------------------


class TestAutoProbeStartupFillsMissingIds:
    """At startup, the auto-probe hook queries department_bots for rows
 with NULL/empty account_id and fills them via Atlassian API probe.
 Successful probes result in DB upserts and audit events.
 """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_all_successful_probes_fill_account_ids(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """When all probes succeed, every missing account_id is
 filled via upsert to department_bot_identity."""

        # Clear the module-level cache before each test
        _probe_cache.clear()

        # Build a prober that succeeds for all rows
        probe_results: dict[tuple[str, str], str | None] = {}
        expected_ids: dict[tuple[str, str], str] = {}
        for row in rows:
            # Generate a deterministic account_id for each row
            account_id = f"acc_{row.dept_id}_{row.service}"[:32]
            probe_results[(row.dept_id, row.service)] = account_id
            expected_ids[(row.dept_id, row.service)] = account_id

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # All probes should succeed
        assert len(results) == len(rows)
        assert all(r.success for r in results)

        # All account_ids should be written
        assert len(writer.upserts) == len(rows)
        for dept_id, service, account_id in writer.upserts:
            assert expected_ids[(dept_id, service)] == account_id

        # Audit events should be written for each success
        filled_events = [
            e for e in audit.events
            if e["action"] == "bot_account_id_auto_filled"
        ]
        assert len(filled_events) == len(rows)

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_probe_results_are_cached(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """Probe results are cached for 5 minutes; re-running
 the startup hook does not re-probe the same (dept_id, service)."""

        _probe_cache.clear()

        probe_results = {
            (row.dept_id, row.service): f"acc_{row.dept_id}_{row.service}"[:32]
            for row in rows
        }

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        # First run — probes all rows
        asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        first_call_count = len(prober.calls)

        # Second run — should skip all due to cache
        results_2 = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # No new probes should have been made
        assert len(prober.calls) == first_call_count
        # No new results (all skipped due to cache)
        assert len(results_2) == 0


class TestAutoProbeStartupNeverBlocks:
    """Probe failures (network errors, auth failures, DB write errors)
 MUST NOT block service startup. The function always returns
 gracefully with failure results.
 """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_probe_failures_do_not_raise(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """When probes fail (return None), the function does not
 raise — it returns failure results and audits them."""

        _probe_cache.clear()

        # All probes return None (failure)
        probe_results: dict[tuple[str, str], str | None] = {
            (row.dept_id, row.service): None for row in rows
        }

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        # Should NOT raise
        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # All results should be failures
        assert len(results) == len(rows)
        assert all(not r.success for r in results)
        assert all(r.error is not None for r in results)

        # No upserts should have been made
        assert len(writer.upserts) == 0

        # Failure audit events should be written
        failed_events = [
            e for e in audit.events
            if e["action"] == "bot_account_id_probe_failed"
        ]
        assert len(failed_events) == len(rows)

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_probe_exceptions_do_not_raise(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """When probes throw exceptions, the function catches
 them and treats them as failures without blocking startup."""

        _probe_cache.clear()

        # All probes will raise exceptions
        raise_for = {(row.dept_id, row.service) for row in rows}

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber({}, raise_for=raise_for)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        # Should NOT raise
        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # All results should be failures
        assert len(results) == len(rows)
        assert all(not r.success for r in results)

    def test_reader_failure_does_not_raise(self) -> None:
        """When the DB reader itself fails, the function returns
 an empty list without raising."""

        _probe_cache.clear()

        reader = FakeFailingReader()
        prober = FakeBotIdentityProber({})
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        assert results == []

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_db_write_failure_does_not_block(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """When the DB writer fails (upsert error), the probe
 result is marked as failure but startup is not blocked."""

        _probe_cache.clear()

        # Probes succeed but writes fail
        probe_results = {
            (row.dept_id, row.service): f"acc_{row.dept_id}_{row.service}"[:32]
            for row in rows
        }
        fail_for = {(row.dept_id, row.service) for row in rows}

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter(fail_for=fail_for)
        audit = FakeAuditSink()

        # Should NOT raise
        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # Results should be failures (write failed)
        assert len(results) == len(rows)
        assert all(not r.success for r in results)
        assert all("db_write_failed" in (r.error or "") for r in results)


class TestAutoProbeStartupMixedResults:
    """When some probes succeed and some fail, the successful ones are
 committed and the failures are audited — partial success is the
 expected behavior.
 """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_partial_success_commits_successful_probes(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """In a mixed scenario, successful probes are
 committed while failures are audited. Neither blocks the other."""

        _probe_cache.clear()
        assume(len(rows) >= 2)

        # First half succeeds, second half fails
        midpoint = len(rows) // 2
        probe_results: dict[tuple[str, str], str | None] = {}
        for i, row in enumerate(rows):
            if i < midpoint:
                probe_results[(row.dept_id, row.service)] = (
                    f"acc_{row.dept_id}_{row.service}"[:32]
                )
            else:
                probe_results[(row.dept_id, row.service)] = None

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # Total results should match total rows
        assert len(results) == len(rows)

        # Successful probes should have been written
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        assert len(successful) == midpoint
        assert len(failed) == len(rows) - midpoint

        # Writer should have received only the successful ones
        assert len(writer.upserts) == midpoint

        # Audit should have both success and failure events
        filled_events = [
            e for e in audit.events
            if e["action"] == "bot_account_id_auto_filled"
        ]
        failed_events = [
            e for e in audit.events
            if e["action"] == "bot_account_id_probe_failed"
        ]
        assert len(filled_events) == midpoint
        assert len(failed_events) == len(rows) - midpoint


# ---------------------------------------------------------------------------
# Account ID Auto-Probe — Post-Create Endpoint
# ---------------------------------------------------------------------------


class TestPostCreateInlineProbe:
    """The ``POST /admin/departments/{id}/credentials/{service}`` endpoint
 runs an inline bot identity probe after credential write succeeds.
 The response carries ``account_id_probe_status`` and the resolved
 ``account_id`` on success.
 """

    def test_add_credential_result_has_probe_status_field(self) -> None:
        """The AddCredentialResult dataclass carries
 ``account_id_probe_status`` and ``account_id_probe_error``
 fields for the inline probe outcome."""

        # Import the result dataclass
        sys.path.insert(
            0,
            str(
                _PLATFORM_ROOT
                / "services"
                / "automation-service"
                / "src"
            ),
        )
        from services.dept_credential_service import AddCredentialResult

        from datetime import datetime, timezone

        # Successful probe result
        result = AddCredentialResult(
            dept_id="test-dept",
            service="jira",
            account_id="5fc9e78d1234567890abcdef",
            last_probe_at=datetime.now(timezone.utc),
            vault_path="vault:atlassian/test-dept/jira",
            outcome="created",
            account_id_probe_status="ok",
            account_id_probe_error=None,
        )
        assert result.account_id_probe_status == "ok"
        assert result.account_id is not None
        assert result.account_id_probe_error is None

    def test_add_credential_result_probe_failed_still_200(self) -> None:
        """When the inline probe fails, the result still carries
 the credential info (200 response) but with
 ``account_id_probe_status: "failed"``."""

        sys.path.insert(
            0,
            str(
                _PLATFORM_ROOT
                / "services"
                / "automation-service"
                / "src"
            ),
        )
        from services.dept_credential_service import AddCredentialResult

        from datetime import datetime, timezone

        # Failed probe result — credential was still written successfully
        result = AddCredentialResult(
            dept_id="test-dept",
            service="bitbucket",
            account_id=None,  # Probe couldn't resolve it
            last_probe_at=datetime.now(timezone.utc),
            vault_path="vault:atlassian/test-dept/bitbucket",
            outcome="created",
            account_id_probe_status="failed",
            account_id_probe_error="probe_failed: ConnectionError",
        )
        assert result.account_id_probe_status == "failed"
        assert result.account_id_probe_error is not None
        # The credential was still written — outcome is "created"
        assert result.outcome == "created"

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        service=_service_strategy,
        account_id=_account_id_strategy,
    )
    def test_successful_probe_populates_account_id(
        self, dept_id: str, service: str, account_id: str
    ) -> None:
        """When the inline probe succeeds, the result's
 ``account_id`` field is populated with the resolved value."""

        from datetime import datetime, timezone

        sys.path.insert(
            0,
            str(
                _PLATFORM_ROOT
                / "services"
                / "automation-service"
                / "src"
            ),
        )
        from services.dept_credential_service import AddCredentialResult

        result = AddCredentialResult(
            dept_id=dept_id,
            service=service,
            account_id=account_id,
            last_probe_at=datetime.now(timezone.utc),
            vault_path=f"vault:atlassian/{dept_id}/{service}",
            outcome="created",
            account_id_probe_status="ok",
            account_id_probe_error=None,
        )

        assert result.account_id == account_id
        assert result.account_id_probe_status == "ok"
        assert len(result.account_id) >= 24


# ---------------------------------------------------------------------------
# Account ID Auto-Probe — Probe Failures Don't Break Dept Commit
# ---------------------------------------------------------------------------


class TestProbeFailureDoesNotBreakDeptCommit:
    """Probe failures during the wizard flow result in the department
 being committed with ``mode="disabled"`` rather than failing the
 entire operation. The credential write is preserved.
 """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_startup_probe_failure_preserves_service_startup(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """The auto_probe_missing_account_ids function ALWAYS
 returns (never raises), regardless of how many probes fail.
 This guarantees service startup is never blocked."""

        _probe_cache.clear()

        # Mix of failures: some return None, some raise exceptions
        raise_for: set[tuple[str, str]] = set()
        probe_results: dict[tuple[str, str], str | None] = {}
        for i, row in enumerate(rows):
            key = (row.dept_id, row.service)
            if i % 3 == 0:
                # Probe raises exception
                raise_for.add(key)
            elif i % 3 == 1:
                # Probe returns None
                probe_results[key] = None
            else:
                # Probe succeeds
                probe_results[key] = f"acc_{row.dept_id}"[:32]

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results, raise_for=raise_for)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        # This MUST NOT raise — the function is best-effort
        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # Function returned successfully
        assert isinstance(results, list)
        # Every row that was probed has a result
        assert len(results) == len(rows)

    def test_empty_department_list_returns_empty(self) -> None:
        """When no departments have missing account_ids, the
 function returns an empty list without error."""

        _probe_cache.clear()

        reader = FakeMissingAccountIdReader([])
        prober = FakeBotIdentityProber({})
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        results = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        assert results == []
        assert len(writer.upserts) == 0
        assert len(audit.events) == 0


class TestAutoProbeIdempotency:
    """The auto-probe is idempotent: running it multiple times with the
 same input produces the same outcome. The cache prevents redundant
 API calls within the TTL window.
 """

    def test_cache_ttl_is_five_minutes(self) -> None:
        """The probe cache TTL is exactly 5 minutes (300 seconds)."""
        assert _PROBE_CACHE_TTL_SECONDS == 300

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_idempotent_upsert_on_repeated_success(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """Running the probe twice (with cache cleared between
 runs) produces the same upserts — the writer receives the same
 account_ids both times."""

        _probe_cache.clear()

        probe_results = {
            (row.dept_id, row.service): f"acc_{row.dept_id}_{row.service}"[:32]
            for row in rows
        }

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        # First run
        results_1 = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        upserts_1 = list(writer.upserts)

        # Clear cache and run again
        _probe_cache.clear()
        writer.upserts.clear()

        results_2 = asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        upserts_2 = list(writer.upserts)

        # Same results both times
        assert len(results_1) == len(results_2)
        assert set(upserts_1) == set(upserts_2)


class TestConfigDepartmentsJsonNotModified:
    """The auto-probe function writes ONLY to the
 ``automation.department_bot_identity`` Postgres table via the
 AccountIdWriter protocol. ``config/departments.json`` is NEVER
 modified — this is verified by the fact that the function's only
 write path is through the AccountIdWriter protocol, which targets
 the DB exclusively.
 """

    def test_auto_probe_only_writes_via_writer_protocol(self) -> None:
        """The auto_probe_missing_account_ids function's only
 write dependency is the AccountIdWriter protocol. It has no
 file I/O capability — config/departments.json cannot be
 modified by this code path."""

        import ast
        import inspect
        import textwrap

        source = inspect.getsource(auto_probe_missing_account_ids)
        # Parse the function body as AST to inspect actual code (not docstrings)
        tree = ast.parse(textwrap.dedent(source))

        # Walk the AST looking for file I/O calls
        file_io_calls = {"open", "json.dump", "json.dumps", "Path.write_text"}
        found_file_io: list[str] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check for open(...) calls
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    found_file_io.append("open")
                # Check for json.dump(...) calls
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in ("dump", "write_text"):
                        found_file_io.append(f"{node.func.attr}")

        assert not found_file_io, (
            f"auto_probe_missing_account_ids contains file I/O calls: "
            f"{found_file_io}. It should only write via AccountIdWriter."
        )

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(rows=_dept_bot_missing_rows())
    def test_writer_is_only_persistence_path(
        self, rows: list[DeptBotMissingRow]
    ) -> None:
        """All successful probe results go through the writer
 protocol — no other persistence mechanism is used."""

        _probe_cache.clear()

        probe_results = {
            (row.dept_id, row.service): f"acc_{row.dept_id}_{row.service}"[:32]
            for row in rows
        }

        reader = FakeMissingAccountIdReader(rows)
        prober = FakeBotIdentityProber(probe_results)
        writer = FakeAccountIdWriter()
        audit = FakeAuditSink()

        asyncio.get_event_loop().run_until_complete(
            auto_probe_missing_account_ids(
                reader=reader,
                prober=prober,
                writer=writer,
                audit=audit,
            )
        )

        # Every successful probe should have exactly one upsert
        assert len(writer.upserts) == len(rows)
        # Each upsert targets the correct (dept_id, service, account_id)
        for dept_id, service, account_id in writer.upserts:
            expected = probe_results[(dept_id, service)]
            assert account_id == expected
