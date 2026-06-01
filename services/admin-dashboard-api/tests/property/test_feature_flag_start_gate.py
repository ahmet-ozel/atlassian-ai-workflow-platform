"""Property test: Feature-Flag Start Gate Determinism (Q12).

**Property 11: Feature-Flag Start Gate Determinism (Q12)**
**Validates: Requirements 10.1, 10.2, 10.4, 10.6**

For any manifest entry ``E`` and ``feature_flags`` table state ``F``,
``LifecycleService.start(E.name)`` at Step 1.5:

- ``E.feature_flag_dependency`` empty → flag check passes, normal flow continues.
- All flags in ``E.feature_flag_dependency`` are ``enabled=true`` in ``F``
  → flag check passes, normal flow continues (200 / state="running").
- At least one flag in ``E.feature_flag_dependency`` is ``enabled=false``
  or absent from ``F`` → ``FeatureFlagDisabledError(blocking_flag=<name>)``
  raised + ``service_start_blocked_feature_flag`` audit + HTTP 409.

When multiple flags are disabled, ``blocking_flag`` is deterministically
the **first** disabled flag in manifest order.

Strategy
--------
Hypothesis generates random combinations of:

1. ``flag_names`` — a non-empty tuple of flag name strings (1-4 flags).
2. ``flag_states`` — a dict mapping each flag name to ``True`` / ``False``
   or absent (treated as disabled).
3. ``first_disabled_index`` — which flag in the tuple is the first disabled
   one (used to verify determinism of ``blocking_flag``).

All four sub-properties are exercised as separate ``@given`` tests so
Hypothesis can shrink counterexamples independently.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

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
    FeatureFlagDisabledError,
    LifecycleService,
    LifecycleStateCache,
    StartResponse,
)
from src.manifest import ManagedServiceEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes (mirrors test_lifecycle_service.py patterns)
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


@dataclass
class _FakeFeatureFlagReader:
    """Returns a pre-canned flags map; records every call.

    Mirrors the pattern from ``tests/unit/test_lifecycle_service.py``.
    Missing keys are absent from the returned dict so
    ``_check_feature_flags`` exercises the "missing row → disabled" branch.
    """

    flags: dict[str, bool] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    async def fetch_enabled_flags(
        self, names: Sequence[str]
    ) -> dict[str, bool]:
        self.calls.append(list(names))
        return {name: self.flags[name] for name in names if name in self.flags}


# ---------------------------------------------------------------------------
# Workspace + manifest helpers
# ---------------------------------------------------------------------------

_HTTP_ENV_EXAMPLE = "PORT=8080\nAPI_TOKEN=\"\"\n"


def _build_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with a .env.example for automation-service.

    Uses ``exist_ok=True`` so Hypothesis can call the same test function
    multiple times with the same ``tmp_path`` fixture without raising
    ``FileExistsError`` on the second invocation.
    """
    svc_dir = tmp_path / "services" / "automation-service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / ".env.example").write_text(_HTTP_ENV_EXAMPLE, encoding="utf-8")
    return tmp_path


def _entry_with_flags(flag_names: tuple[str, ...]) -> ManagedServiceEntry:
    """Return a manifest entry with the given feature_flag_dependency."""
    return ManagedServiceEntry(
        name="automation-service",
        kind="http_service",
        compose_service_name="automation-service",
        compose_profile="automation-service",
        env_example_path="services/automation-service/.env.example",
        health_endpoint="/healthz",
        test_command=None,
        feature_flag_dependency=flag_names,
    )


def _make_service(
    *,
    workspace_root: Path,
    flag_names: tuple[str, ...],
    flag_states: dict[str, bool],
) -> tuple[LifecycleService, _FakeAuditWriter, _FakeComposeRunner, _FakeFeatureFlagReader]:
    """Wire a LifecycleService with the given flag configuration."""
    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    compose = _FakeComposeRunner()
    health = _FakeHealthProbe()
    reader = _FakeFeatureFlagReader(flags=flag_states)

    async def _no_sleep(_: float) -> None:
        return None

    svc = LifecycleService(
        manifest=(_entry_with_flags(flag_names),),
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        feature_flag_reader=reader,  # type: ignore[arg-type]
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )
    return svc, audit, compose, reader


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Valid flag name characters: uppercase letters, digits, underscores.
_FLAG_NAME_STRATEGY = st.from_regex(
    r"FEATURE_FLAG_[A-Z][A-Z0-9_]{1,20}",
    fullmatch=True,
)

# A non-empty tuple of 1-4 distinct flag names.
_FLAG_NAMES_STRATEGY = st.lists(
    _FLAG_NAME_STRATEGY,
    min_size=1,
    max_size=4,
    unique=True,
).map(tuple)


# ---------------------------------------------------------------------------
# Property 11a — empty feature_flag_dependency → gate is a no-op
# ---------------------------------------------------------------------------


def test_empty_flag_dependency_skips_gate(tmp_path: Path) -> None:
    """Property 11a — empty feature_flag_dependency skips the gate entirely.

    **Validates: Requirements 10.4**

    When ``feature_flag_dependency`` is empty, no SELECT is issued and
    the start proceeds normally (state="running").
    """
    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader(flags={})

    async def _no_sleep(_: float) -> None:
        return None

    svc = LifecycleService(
        manifest=(_entry_with_flags(()),),
        audit=_FakeAuditWriter(),  # type: ignore[arg-type]
        vault=_FakeVaultClient(),  # type: ignore[arg-type]
        compose=_FakeComposeRunner(),  # type: ignore[arg-type]
        health=_FakeHealthProbe(),  # type: ignore[arg-type]
        workspace_root=workspace,
        feature_flag_reader=reader,  # type: ignore[arg-type]
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    response = asyncio.run(_run())
    assert response.state == "running", (
        f"Empty flag dependency should allow start; got state={response.state!r}"
    )
    # No SELECT was issued.
    assert reader.calls == [], (
        "No fetch_enabled_flags call expected for empty dependency list"
    )


# ---------------------------------------------------------------------------
# Property 11b — all flags enabled → start succeeds (200 / state="running")
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(flag_names=_FLAG_NAMES_STRATEGY)
def test_all_flags_enabled_allows_start(
    flag_names: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Property 11b — all flags enabled → start proceeds normally.

    **Validates: Requirements 10.1, 10.4**

    For any non-empty ``flag_names`` tuple where every flag is
    ``enabled=true`` in the reader, ``start`` must succeed with
    ``state="running"`` and no ``service_start_blocked_feature_flag``
    audit row.
    """
    workspace = _build_workspace(tmp_path)
    # All flags enabled.
    flag_states = {name: True for name in flag_names}
    svc, audit, compose, reader = _make_service(
        workspace_root=workspace,
        flag_names=flag_names,
        flag_states=flag_states,
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    response = asyncio.run(_run())

    assert response.state == "running", (
        f"All flags enabled should allow start; got state={response.state!r}, "
        f"flags={flag_names!r}"
    )
    # Exactly one SELECT was issued (Requirement 10.5 — single SELECT).
    assert len(reader.calls) == 1, (
        f"Expected exactly 1 fetch_enabled_flags call, got {len(reader.calls)}"
    )
    assert sorted(reader.calls[0]) == sorted(flag_names), (
        f"fetch_enabled_flags called with wrong names: {reader.calls[0]!r}"
    )
    # No block audit row.
    block_actions = [
        e.action
        for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert block_actions == [], (
        f"No block audit expected when all flags enabled; got {block_actions!r}"
    )
    # Compose.up was called (start proceeded past Step 1.5).
    assert len(compose.up_calls) == 1, (
        "compose.up should have been called when all flags are enabled"
    )


# ---------------------------------------------------------------------------
# Property 11c — at least one flag disabled → FeatureFlagDisabledError + 409
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    flag_names=_FLAG_NAMES_STRATEGY,
    disabled_index=st.integers(min_value=0, max_value=3),
)
def test_any_disabled_flag_blocks_start(
    flag_names: tuple[str, ...],
    disabled_index: int,
    tmp_path: Path,
) -> None:
    """Property 11c — any disabled flag raises FeatureFlagDisabledError.

    **Validates: Requirements 10.1, 10.2, 10.6**

    For any non-empty ``flag_names`` tuple where at least one flag is
    ``enabled=false``, ``start`` must raise ``FeatureFlagDisabledError``
    and write a ``service_start_blocked_feature_flag`` audit row.
    Compose.up must NOT be called (gate fires before Step 8).
    """
    workspace = _build_workspace(tmp_path)

    # Clamp disabled_index to valid range.
    actual_disabled_idx = disabled_index % len(flag_names)
    disabled_flag = flag_names[actual_disabled_idx]

    # All flags enabled except the one at actual_disabled_idx.
    flag_states = {name: True for name in flag_names}
    flag_states[disabled_flag] = False

    svc, audit, compose, reader = _make_service(
        workspace_root=workspace,
        flag_names=flag_names,
        flag_states=flag_states,
    )

    async def _run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with pytest.raises(FeatureFlagDisabledError) as exc_info:
        asyncio.run(_run())

    # The blocking_flag must be the disabled one.
    assert exc_info.value.blocking_flag == disabled_flag, (
        f"Expected blocking_flag={disabled_flag!r}, "
        f"got {exc_info.value.blocking_flag!r}"
    )

    # Audit row must be written.
    block_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert len(block_rows) == 1, (
        f"Expected exactly 1 block audit row, got {len(block_rows)}"
    )
    block = block_rows[0]
    assert block.outcome == "failed", (
        f"Block audit outcome must be 'failed', got {block.outcome!r}"
    )
    assert block.details_json["blocking_flag"] == disabled_flag, (
        f"Audit blocking_flag mismatch: {block.details_json!r}"
    )
    assert block.details_json["flag_state"] in ("disabled", "missing"), (
        f"Audit flag_state must be 'disabled' or 'missing', got "
        f"{block.details_json.get('flag_state')!r}"
    )

    # Compose.up must NOT have been called (gate fires before Step 8).
    assert compose.up_calls == [], (
        "compose.up must not be called when a flag is disabled"
    )


# ---------------------------------------------------------------------------
# Property 11d — missing flag row treated as disabled
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(flag_names=_FLAG_NAMES_STRATEGY)
def test_missing_flag_row_treated_as_disabled(
    flag_names: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Property 11d — flag absent from feature_flags table → treated as disabled.

    **Validates: Requirements 10.1, 10.6**

    When the reader returns an empty dict (zero rows from
    ``shared.feature_flags``), every flag in ``feature_flag_dependency``
    is treated as disabled. The first flag in manifest order becomes
    ``blocking_flag``.
    """
    workspace = _build_workspace(tmp_path)
    # Reader returns no rows at all (simulates missing/typo flag names).
    svc, audit, compose, reader = _make_service(
        workspace_root=workspace,
        flag_names=flag_names,
        flag_states={},  # zero rows
    )

    async def _run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with pytest.raises(FeatureFlagDisabledError) as exc_info:
        asyncio.run(_run())

    # The first flag in manifest order is the blocking_flag.
    expected_blocking = flag_names[0]
    assert exc_info.value.blocking_flag == expected_blocking, (
        f"Expected blocking_flag={expected_blocking!r} (first in manifest order), "
        f"got {exc_info.value.blocking_flag!r}"
    )

    # Audit row must be written with flag_state="missing".
    block_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert len(block_rows) == 1, (
        f"Expected exactly 1 block audit row, got {len(block_rows)}"
    )
    assert block_rows[0].details_json["flag_state"] == "missing", (
        f"flag_state must be 'missing' for absent rows, "
        f"got {block_rows[0].details_json.get('flag_state')!r}"
    )

    # Compose.up must NOT have been called.
    assert compose.up_calls == [], (
        "compose.up must not be called when flag row is missing"
    )


# ---------------------------------------------------------------------------
# Property 11e — multiple disabled flags → first in manifest order wins
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    flag_names=st.lists(
        _FLAG_NAME_STRATEGY,
        min_size=2,
        max_size=4,
        unique=True,
    ).map(tuple),
)
def test_multiple_disabled_flags_first_manifest_order_wins(
    flag_names: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Property 11e — multiple disabled flags → blocking_flag is first in manifest order.

    **Validates: Requirements 10.1, 10.2**

    When all flags are disabled, ``blocking_flag`` must be the first
    flag in the manifest's ``feature_flag_dependency`` tuple — not the
    first alphabetically or by any other ordering.
    """
    workspace = _build_workspace(tmp_path)
    # All flags disabled.
    flag_states = {name: False for name in flag_names}

    svc, audit, compose, reader = _make_service(
        workspace_root=workspace,
        flag_names=flag_names,
        flag_states=flag_states,
    )

    async def _run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with pytest.raises(FeatureFlagDisabledError) as exc_info:
        asyncio.run(_run())

    # Deterministic: first manifest entry wins regardless of dict ordering.
    expected_blocking = flag_names[0]
    assert exc_info.value.blocking_flag == expected_blocking, (
        f"With all flags disabled, blocking_flag must be the first in manifest "
        f"order ({expected_blocking!r}), got {exc_info.value.blocking_flag!r}. "
        f"flag_names={flag_names!r}"
    )

    # Exactly one audit row (only the first disabled flag is reported).
    block_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert len(block_rows) == 1, (
        f"Expected exactly 1 block audit row (first disabled flag only), "
        f"got {len(block_rows)}"
    )
    assert block_rows[0].details_json["blocking_flag"] == expected_blocking

    # Compose.up must NOT have been called.
    assert compose.up_calls == [], (
        "compose.up must not be called when flags are disabled"
    )


# ---------------------------------------------------------------------------
# Property 11f — determinism: same input → same outcome (idempotency)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    flag_names=_FLAG_NAMES_STRATEGY,
    # True = all enabled, False = first flag disabled
    all_enabled=st.booleans(),
)
def test_start_gate_is_deterministic(
    flag_names: tuple[str, ...],
    all_enabled: bool,
    tmp_path: Path,
) -> None:
    """Property 11f — start gate is deterministic: same input → same outcome.

    **Validates: Requirements 10.1, 10.2, 10.4, 10.6**

    Calling ``start`` twice with the same flag state must produce the
    same outcome (both succeed or both raise ``FeatureFlagDisabledError``
    with the same ``blocking_flag``). This confirms the gate is a pure
    function of the flag state.
    """
    workspace = _build_workspace(tmp_path)

    if all_enabled:
        flag_states = {name: True for name in flag_names}
    else:
        # First flag disabled, rest enabled.
        flag_states = {name: True for name in flag_names}
        flag_states[flag_names[0]] = False

    outcomes: list[str] = []
    blocking_flags: list[str | None] = []

    for _ in range(2):
        svc, _audit, _compose, _reader = _make_service(
            workspace_root=workspace,
            flag_names=flag_names,
            flag_states=flag_states,
        )

        async def _run() -> str:
            try:
                resp = await svc.start(
                    name="automation-service",
                    env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
                    actor="admin@test",
                )
                return resp.state
            except FeatureFlagDisabledError as exc:
                return f"blocked:{exc.blocking_flag}"

        result = asyncio.run(_run())
        if result.startswith("blocked:"):
            outcomes.append("blocked")
            blocking_flags.append(result[len("blocked:"):])
        else:
            outcomes.append(result)
            blocking_flags.append(None)

    assert outcomes[0] == outcomes[1], (
        f"Non-deterministic outcome: first call={outcomes[0]!r}, "
        f"second call={outcomes[1]!r}. flag_names={flag_names!r}, "
        f"all_enabled={all_enabled!r}"
    )
    assert blocking_flags[0] == blocking_flags[1], (
        f"Non-deterministic blocking_flag: first={blocking_flags[0]!r}, "
        f"second={blocking_flags[1]!r}"
    )
