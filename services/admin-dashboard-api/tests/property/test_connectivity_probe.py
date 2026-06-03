"""Connectivity probe state mapping.
Connectivity probe state mapping.
For any manifest entry ``E`` and subprocess outcome ``O``, the
``_run_connectivity_probe`` helper must:
- ``E.connectivity_probe_command is None`` → no-op; ``credentials_status``
  remains ``None``; no audit event emitted.
- ``exit_code == 0`` → ``credentials_status = "ok"``;
  ``service_connectivity_probe_passed`` audit emitted.
- ``exit_code != 0`` → ``credentials_status = "failed"``;
  ``credentials_probe_detail = stderr[-500:]``;
  ``service_connectivity_probe_failed`` audit emitted.
- ``subprocess.TimeoutExpired`` → ``credentials_status = "failed"``;
  ``service_connectivity_probe_failed`` audit emitted.
In all cases the service ``state`` remains ``"running"``; probe failure
does not change the lifecycle state.
Strategy
--------
Hypothesis generates random combinations of:
1. ``probe_command`` — ``None`` or a non-empty command string.
2. ``exit_code`` — 0 (success) or non-zero (failure).
3. ``stderr_text`` — arbitrary string (may be long; we verify truncation).
4. ``failure_mode`` — ``"timeout"`` or ``"os_error"`` for subprocess
   exception paths.
All four sub-properties are exercised as separate ``@given`` tests so
Hypothesis can shrink counterexamples independently."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import AuditEntry, AuditWriteOutcome  # noqa: E402
from src.lifecycle.compose_runner import ComposeResult, TestResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    LifecycleService,
    LifecycleStateCache,
    StartResponse,
)
from src.manifest import ManagedServiceEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Fake collaborators (mirrors test_feature_flag_start_gate.py patterns)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)
    precheck_raise: BaseException | None = None

    async def precheck(self) -> None:
        self.precheck_calls += 1
        if self.precheck_raise is not None:
            raise self.precheck_raise

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """No-op Vault client."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    stored: dict[str, dict[str, str]] = field(default_factory=dict)

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def delete_env_override(self, *, service_name: str, key: str) -> None:
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Records Compose calls; always succeeds."""

    up_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        self.up_calls.append({"profile": profile, "service_name": service_name})
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append({"service_name": service_name})
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> TestResult:
        return TestResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=tuple(argv),
        )


@dataclass
class _FakeHealthProbe:
    """Always returns a healthy snapshot."""

    calls: list[ManagedServiceEntry] = field(default_factory=list)

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# Workspace + manifest helpers
# ---------------------------------------------------------------------------

_HTTP_ENV_EXAMPLE = "PORT=8080\nAPI_TOKEN=\"\"\n"


def _build_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with a .env.example for automation-service."""
    svc_dir = tmp_path / "services" / "automation-service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / ".env.example").write_text(_HTTP_ENV_EXAMPLE, encoding="utf-8")
    return tmp_path


def _entry(probe_command: str | None) -> ManagedServiceEntry:
    """Return a manifest entry with the given connectivity_probe_command."""
    return ManagedServiceEntry(
        name="automation-service",
        kind="http_service",
        compose_service_name="automation-service",
        compose_profile="automation-service",
        env_example_path="services/automation-service/.env.example",
        health_endpoint="/healthz",
        test_command=None,
        connectivity_probe_command=probe_command,
    )


def _make_service(
    *,
    workspace_root: Path,
    probe_command: str | None,
) -> tuple[LifecycleService, _FakeAuditWriter, _FakeComposeRunner]:
    """Wire a LifecycleService with the given probe command."""
    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    compose = _FakeComposeRunner()
    health = _FakeHealthProbe()

    async def _no_sleep(_: float) -> None:
        return None

    svc = LifecycleService(
        manifest=(_entry(probe_command),),
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )
    return svc, audit, compose


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Non-empty probe command strings (simple shell-like commands).
_PROBE_COMMAND_STRATEGY = st.from_regex(
    r"python -m [a-z][a-z0-9_]{1,20}(\.[a-z][a-z0-9_]{1,10}){0,2}",
    fullmatch=True,
)

# Non-zero exit codes (failure).
_NONZERO_EXIT_CODE_STRATEGY = st.integers(min_value=1, max_value=255)

# Stderr text of varying lengths (including long strings to test truncation).
_STDERR_STRATEGY = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Zs"),
        whitelist_characters="=:/-_.\n",
    ),
    min_size=0,
    max_size=1000,
)


# ---------------------------------------------------------------------------
#  — probe_command=None → no-op; credentials_status stays None
# ---------------------------------------------------------------------------


def test_null_probe_command_is_noop(tmp_path: Path) -> None:
    """— null probe command is a no-op.
    When ``connectivity_probe_command`` is ``None``, ``_run_connectivity_probe``
    must not emit any audit event and must leave ``credentials_status=None``
    in the state cache."""
    workspace = _build_workspace(tmp_path)
    svc, audit, compose = _make_service(workspace_root=workspace, probe_command=None)

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch("subprocess.run") as mock_run:
        response = asyncio.run(_run())

    # subprocess.run must NOT have been called.
    mock_run.assert_not_called()

    # State must be running (lifecycle unaffected).
    assert response.state == "running", (
        f"Expected state='running', got {response.state!r}"
    )

    # credentials_status must remain None (no probe configured).
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status is None, (
        f"credentials_status must be None when probe_command=None, "
        f"got {slot.credentials_status!r}"
    )
    assert slot.credentials_probe_at is None, (
        "credentials_probe_at must be None when probe_command=None"
    )

    # No connectivity probe audit events.
    probe_actions = [
        e.action for e in audit.write_with_retry_calls
        if "connectivity_probe" in e.action
    ]
    assert probe_actions == [], (
        f"No probe audit events expected for null command, got {probe_actions!r}"
    )


# ---------------------------------------------------------------------------
#  — exit_code=0 → credentials_status="ok" + passed audit
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(probe_command=_PROBE_COMMAND_STRATEGY)
def test_exit_code_zero_sets_credentials_ok(
    probe_command: str,
    tmp_path: Path,
) -> None:
    """— exit_code=0 → credentials_status='ok' + passed audit.
    For any non-null ``connectivity_probe_command``, when ``subprocess.run``
    returns exit_code=0, the state cache must have ``credentials_status="ok"``
    and a ``service_connectivity_probe_passed`` audit row must be emitted.
    The service ``state`` must remain ``"running"``."""
    workspace = _build_workspace(tmp_path)
    svc, audit, compose = _make_service(
        workspace_root=workspace, probe_command=probe_command
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""
    mock_proc.stderr = ""

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch("subprocess.run", return_value=mock_proc):
        response = asyncio.run(_run())

    # Service state must be running.
    assert response.state == "running", (
        f"Service state must be 'running' after successful probe; "
        f"got {response.state!r}"
    )

    # credentials_status must be "ok".
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "ok", (
        f"exit_code=0 must set credentials_status='ok', "
        f"got {slot.credentials_status!r}"
    )
    assert slot.credentials_probe_at is not None, (
        "credentials_probe_at must be set after a successful probe"
    )
    assert slot.credentials_probe_detail is None, (
        "credentials_probe_detail must be None after a successful probe"
    )

    # Exactly one passed audit row.
    passed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert len(passed_rows) == 1, (
        f"Expected exactly 1 'service_connectivity_probe_passed' audit row, "
        f"got {len(passed_rows)}"
    )
    passed = passed_rows[0]
    assert passed.outcome == "success", (
        f"Passed probe audit outcome must be 'success', got {passed.outcome!r}"
    )
    assert passed.details_json["exit_code"] == 0, (
        f"Passed probe audit exit_code must be 0, got {passed.details_json.get('exit_code')!r}"
    )
    assert passed.details_json["service_name"] == "automation-service", (
        f"Passed probe audit service_name mismatch: {passed.details_json!r}"
    )

    # No failed audit rows.
    failed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert failed_rows == [], (
        f"No 'service_connectivity_probe_failed' audit expected for exit_code=0, "
        f"got {failed_rows!r}"
    )


# ---------------------------------------------------------------------------
#  — exit_code!=0 → credentials_status="failed" + failed audit
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    probe_command=_PROBE_COMMAND_STRATEGY,
    exit_code=_NONZERO_EXIT_CODE_STRATEGY,
    stderr_text=_STDERR_STRATEGY,
)
def test_nonzero_exit_code_sets_credentials_failed(
    probe_command: str,
    exit_code: int,
    stderr_text: str,
    tmp_path: Path,
) -> None:
    """— exit_code!=0 → credentials_status='failed' + failed audit.
    For any non-null ``connectivity_probe_command`` and any non-zero exit code,
    the state cache must have ``credentials_status="failed"`` and a
    ``service_connectivity_probe_failed`` audit row must be emitted.
    The service ``state`` must remain ``"running"``; probe failure does not
    change the lifecycle state.
    ``credentials_probe_detail`` must be the last 500 chars of stderr."""
    workspace = _build_workspace(tmp_path)
    svc, audit, compose = _make_service(
        workspace_root=workspace, probe_command=probe_command
    )

    mock_proc = MagicMock()
    mock_proc.returncode = exit_code
    mock_proc.stdout = ""
    mock_proc.stderr = stderr_text

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch("subprocess.run", return_value=mock_proc):
        response = asyncio.run(_run())

    # Service state must remain running (probe failure ≠ lifecycle failure).
    assert response.state == "running", (
        f"Service state must be 'running' even after failed probe; "
        f"got {response.state!r}"
    )

    # credentials_status must be "failed".
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "failed", (
        f"exit_code={exit_code} must set credentials_status='failed', "
        f"got {slot.credentials_status!r}"
    )
    assert slot.credentials_probe_at is not None, (
        "credentials_probe_at must be set after a failed probe"
    )

    # credentials_probe_detail must be last 500 chars of stderr.
    expected_detail = stderr_text[-500:] if stderr_text else ""
    assert slot.credentials_probe_detail == expected_detail, (
        f"credentials_probe_detail must be stderr[-500:], "
        f"expected {expected_detail!r}, got {slot.credentials_probe_detail!r}"
    )

    # Exactly one failed audit row.
    failed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert len(failed_rows) == 1, (
        f"Expected exactly 1 'service_connectivity_probe_failed' audit row, "
        f"got {len(failed_rows)}"
    )
    failed = failed_rows[0]
    assert failed.outcome == "failed", (
        f"Failed probe audit outcome must be 'failed', got {failed.outcome!r}"
    )
    assert failed.details_json["exit_code"] == exit_code, (
        f"Failed probe audit exit_code must be {exit_code}, "
        f"got {failed.details_json.get('exit_code')!r}"
    )
    assert failed.details_json["service_name"] == "automation-service", (
        f"Failed probe audit service_name mismatch: {failed.details_json!r}"
    )
    assert "stderr_summary" in failed.details_json, (
        "Failed probe audit must include 'stderr_summary' in details_json"
    )

    # No passed audit rows.
    passed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert passed_rows == [], (
        f"No 'service_connectivity_probe_passed' audit expected for exit_code={exit_code}, "
        f"got {passed_rows!r}"
    )


# ---------------------------------------------------------------------------
#  — TimeoutExpired → credentials_status="failed" + failed audit
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(probe_command=_PROBE_COMMAND_STRATEGY)
def test_timeout_sets_credentials_failed(
    probe_command: str,
    tmp_path: Path,
) -> None:
    """— subprocess.TimeoutExpired → credentials_status='failed'.
    When ``subprocess.run`` raises ``subprocess.TimeoutExpired`` (the 30-second
    timeout fires), the state cache must have ``credentials_status="failed"``
    and a ``service_connectivity_probe_failed`` audit row must be emitted.
    The service ``state`` must remain ``"running"``."""
    workspace = _build_workspace(tmp_path)
    svc, audit, compose = _make_service(
        workspace_root=workspace, probe_command=probe_command
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    # Simulate a 30-second timeout.
    timeout_exc = subprocess.TimeoutExpired(cmd=probe_command, timeout=30)
    with patch("subprocess.run", side_effect=timeout_exc):
        response = asyncio.run(_run())

    # Service state must remain running.
    assert response.state == "running", (
        f"Service state must be 'running' after timeout; got {response.state!r}"
    )

    # credentials_status must be "failed".
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "failed", (
        f"TimeoutExpired must set credentials_status='failed', "
        f"got {slot.credentials_status!r}"
    )
    assert slot.credentials_probe_at is not None, (
        "credentials_probe_at must be set after a timeout"
    )
    assert slot.credentials_probe_detail is not None, (
        "credentials_probe_detail must be set after a timeout"
    )
    assert "TimeoutExpired" in slot.credentials_probe_detail, (
        f"credentials_probe_detail must mention 'TimeoutExpired', "
        f"got {slot.credentials_probe_detail!r}"
    )

    # Exactly one failed audit row.
    failed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert len(failed_rows) == 1, (
        f"Expected exactly 1 'service_connectivity_probe_failed' audit row "
        f"after timeout, got {len(failed_rows)}"
    )
    failed = failed_rows[0]
    assert failed.outcome == "failed", (
        f"Timeout probe audit outcome must be 'failed', got {failed.outcome!r}"
    )
    # exit_code for timeout is -1 (sentinel used by _run_connectivity_probe).
    assert failed.details_json["exit_code"] == -1, (
        f"Timeout probe audit exit_code must be -1, "
        f"got {failed.details_json.get('exit_code')!r}"
    )

    # No passed audit rows.
    passed_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert passed_rows == [], (
        f"No 'service_connectivity_probe_passed' audit expected after timeout, "
        f"got {passed_rows!r}"
    )


# ---------------------------------------------------------------------------
# detail is always ≤ 500 chars
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    probe_command=_PROBE_COMMAND_STRATEGY,
    stderr_text=st.text(min_size=501, max_size=2000),
)
def test_stderr_truncated_to_500_chars(
    probe_command: str,
    stderr_text: str,
    tmp_path: Path,
) -> None:
    """— credentials_probe_detail is always ≤ 500 chars.
    When stderr is longer than 500 characters, ``credentials_probe_detail``
    must be exactly ``stderr[-500:]`` — the last 500 characters."""
    workspace = _build_workspace(tmp_path)
    svc, audit, compose = _make_service(
        workspace_root=workspace, probe_command=probe_command
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = stderr_text

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch("subprocess.run", return_value=mock_proc):
        asyncio.run(_run())

    slot = svc.state_cache["automation-service"]
    assert slot.credentials_probe_detail is not None, (
        "credentials_probe_detail must be set for non-zero exit code"
    )
    assert len(slot.credentials_probe_detail) <= 500, (
        f"credentials_probe_detail must be ≤ 500 chars, "
        f"got {len(slot.credentials_probe_detail)} chars"
    )
    assert slot.credentials_probe_detail == stderr_text[-500:], (
        "credentials_probe_detail must be the last 500 chars of stderr"
    )


# ---------------------------------------------------------------------------
# same input → same outcome
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    probe_command=st.one_of(st.none(), _PROBE_COMMAND_STRATEGY),
    exit_code=st.one_of(st.just(0), _NONZERO_EXIT_CODE_STRATEGY),
)
def test_probe_state_mapping_is_deterministic(
    probe_command: str | None,
    exit_code: int,
    tmp_path: Path,
) -> None:
    """— probe state mapping is deterministic.
    Calling ``start`` twice with the same probe command and subprocess
    outcome must produce the same ``credentials_status`` both times.
    This confirms the mapping is a pure function of the inputs."""
    workspace = _build_workspace(tmp_path)

    statuses: list[str | None] = []

    for _ in range(2):
        svc, _audit, _compose = _make_service(
            workspace_root=workspace, probe_command=probe_command
        )

        mock_proc = MagicMock()
        mock_proc.returncode = exit_code
        mock_proc.stdout = ""
        mock_proc.stderr = "error detail"

        async def _run() -> None:
            await svc.start(
                name="automation-service",
                env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
                actor="admin@test",
            )

        if probe_command is None:
            with patch("subprocess.run") as mock_run:
                asyncio.run(_run())
                mock_run.assert_not_called()
        else:
            with patch("subprocess.run", return_value=mock_proc):
                asyncio.run(_run())

        statuses.append(svc.state_cache["automation-service"].credentials_status)

    assert statuses[0] == statuses[1], (
        f"Non-deterministic credentials_status: first={statuses[0]!r}, "
        f"second={statuses[1]!r}. probe_command={probe_command!r}, "
        f"exit_code={exit_code!r}"
    )
