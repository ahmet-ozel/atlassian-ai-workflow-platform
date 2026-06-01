# Feature: platform-mimari-uyumluluk
# Property 15: Stop + `purge_vault` Profile Guard Matrisi (Q16)
# Validates: Requirements 14.2, 14.3, 14.4, 14.6
"""Property test: Stop + ``purge_vault`` Profile Guard Matrisi (Q16).

**Property 15: Stop + ``purge_vault`` Profile Guard Matrisi (Q16)**
**Validates: Requirements 14.2, 14.3, 14.4, 14.6**

For any tuple ``(deployment_profile, purge_vault, vault_list_outcome,
vault_delete_outcome)`` driving the ``POST /admin/services/{name}/stop``
endpoint, the observable behaviour must satisfy the following deterministic
matrix (a strict superset of ``tests/property/test_stop_lifecycle_purge_guard.py``):

1.  **Production guard** — ``purge_vault=True`` AND
    ``deployment_profile.lower() == "production"`` →
    HTTP ``403`` with ``error="purge_vault_forbidden_in_production"``;
    a ``purge_vault_blocked_in_production`` audit row is written;
    Compose stop is **never** invoked; Vault is **never** touched.

2.  **Non-production passthrough** — ``purge_vault=True`` AND
    ``deployment_profile.lower() != "production"`` (e.g. ``dev``,
    ``staging``, ``test``) → HTTP ``200``; Compose stop runs; Vault
    LIST + DELETE run (best-effort).

3.  **purge_vault=False** — for every profile (including production)
    the request proceeds normally → HTTP ``200``; Compose stop runs;
    Vault is **never** touched (R14.1 backwards-compat default).

4.  **Audit emission discipline** — the ``purge_vault_blocked_in_production``
    audit row appears **only** on the 403 path; the
    ``vault_overrides_purged`` / ``vault_purge_partial_failure`` rows
    appear **only** when the guard passed AND ``purge_vault=True``;
    they are mutually exclusive with the block row.

5.  **Vault failure best-effort** — when the guard passes and a Vault
    LIST or DELETE call fails, the HTTP response stays ``200`` (R14.4)
    and a ``vault_purge_partial_failure`` audit row carries the
    ``partial_count`` of keys deleted before the failure.

Strategy
--------
Hypothesis generates random combinations of:

* ``profile`` — sampled from a wide set covering case variants of
  ``production`` (must block), other deployment names (``dev``,
  ``staging``, ``test``, ``prod``, ``production-eu``) that must NOT
  block, and random ASCII strings (must NOT block unless they are
  exactly ``production`` mod case).
* ``purge_vault`` — booleans.
* ``vault_outcome`` — sampled from
  ``{"success", "list_fail", "delete_fail_first", "delete_fail_last"}``
  (only relevant on the non-blocked path).
* ``stored_keys`` — small dicts of override keys to populate Vault.

The tests run the full FastAPI request pipeline (URL → router →
``LifecycleService.stop`` → fake Vault) so the production guard,
``StopRequest`` schema, ``LifecycleService.stop`` purge wiring, and
audit chain are all exercised together.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.lifecycle.audit_writer import AuditEntry, AuditWriteOutcome  # noqa: E402
from src.lifecycle.compose_runner import ComposeResult, TestResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    LifecycleService,
    LifecycleStateCache,
)
from src.lifecycle.vault_client import VaultWriteError  # noqa: E402
from src.manifest import ManagedServiceEntry  # noqa: E402
from src.routers.services_lifecycle import (  # noqa: E402
    get_lifecycle_service,
    get_settings_dependency,
    router,
)


# ---------------------------------------------------------------------------
# Fakes — minimal, recording, deterministic
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction; never fails."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)

    async def precheck(self) -> None:
        self.precheck_calls += 1

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)

    @property
    def actions(self) -> list[str]:
        return [e.action for e in self.write_with_retry_calls]


@dataclass
class _FakeVaultClient:
    """Records LIST/DELETE; honours configurable failure modes."""

    stored: dict[str, dict[str, str]] = field(default_factory=dict)
    list_calls: list[str] = field(default_factory=list)
    delete_calls: list[tuple[str, str]] = field(default_factory=list)
    write_calls: list[tuple[str, str, str]] = field(default_factory=list)
    list_raise: BaseException | None = None
    raise_on_delete_after: int | None = None

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.write_calls.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def list_env_override_keys(
        self, *, service_name: str
    ) -> list[str]:
        self.list_calls.append(service_name)
        if self.list_raise is not None:
            raise self.list_raise
        return list(self.stored.get(service_name, {}).keys())

    async def delete_env_override(
        self, *, service_name: str, key: str
    ) -> None:
        self.delete_calls.append((service_name, key))
        if (
            self.raise_on_delete_after is not None
            and len(self.delete_calls) > self.raise_on_delete_after
        ):
            raise VaultWriteError(
                operation="delete",
                service_name=service_name,
                key=key,
                status_code=500,
                message="injected purge failure",
            )
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Records every Compose call; always succeeds."""

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
            exit_code=0, stdout="", stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append(
            {"service_name": service_name, "remove_volumes": remove_volumes}
        )
        return ComposeResult(
            exit_code=0, stdout="", stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(self, *, service_name: str, tail: int, follow: bool):
        return ComposeResult(
            exit_code=0, stdout="", stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> TestResult:
        return TestResult(exit_code=0, stdout="", stderr="", argv=tuple(argv))


@dataclass
class _FakeHealthProbe:
    """Always returns a healthy snapshot (not used on the stop path)."""

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200, healthz_body="ok",
            readyz_status=200, readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# Workspace + LifecycleService builder
# ---------------------------------------------------------------------------

_SERVICE_NAME = "automation-service"
_ENV_EXAMPLE = "PORT=8080\nAPI_TOKEN=\"\"\n"


def _build_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace tree the manifest entry can read."""
    svc_dir = tmp_path / "services" / _SERVICE_NAME
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / ".env.example").write_text(_ENV_EXAMPLE, encoding="utf-8")
    return tmp_path


def _entry() -> ManagedServiceEntry:
    return ManagedServiceEntry(
        name=_SERVICE_NAME,
        kind="http_service",
        compose_service_name=_SERVICE_NAME,
        compose_profile=_SERVICE_NAME,
        env_example_path=f"services/{_SERVICE_NAME}/.env.example",
        health_endpoint="/healthz",
        test_command=None,
    )


def _make_service(
    *,
    workspace_root: Path,
    vault: _FakeVaultClient,
    audit: _FakeAuditWriter | None = None,
    compose: _FakeComposeRunner | None = None,
    initial_state: str = "running",
) -> tuple[
    LifecycleService,
    _FakeAuditWriter,
    _FakeVaultClient,
    _FakeComposeRunner,
]:
    """Wire a real :class:`LifecycleService` over fakes."""

    audit = audit or _FakeAuditWriter()
    compose = compose or _FakeComposeRunner()
    health = _FakeHealthProbe()

    async def _no_sleep(_: float) -> None:
        return None

    state = {
        _SERVICE_NAME: LifecycleStateCache(
            name=_SERVICE_NAME, state=initial_state  # type: ignore[arg-type]
        )
    }
    svc = LifecycleService(
        manifest=(_entry(),),
        state=state,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )
    return svc, audit, vault, compose


def _build_app(
    svc: LifecycleService,
    *,
    deployment_profile: str,
    actor_sub: str = "ops-admin-1",
) -> FastAPI:
    """FastAPI app wired to the router with stub dependencies."""

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub, groups=("admin",)
    )
    app.dependency_overrides[get_lifecycle_service] = lambda: svc

    class _StubSettings:
        def __init__(self, profile: str) -> None:
            self.deployment_profile = profile

    _settings_stub = _StubSettings(deployment_profile)
    app.dependency_overrides[get_settings_dependency] = lambda: _settings_stub
    return app


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Every case-folded form of "production" must trigger the guard
# (Requirement 14.2 — case-insensitive match).
_PRODUCTION_PROFILES = st.sampled_from(
    ["production", "PRODUCTION", "Production", "PrOdUcTiOn"]
)

# Profiles that must NOT trigger the guard (note: "prod" is NOT
# "production" — the guard does an exact lower-case equality check,
# not a prefix match).
_NON_PRODUCTION_PROFILES = st.sampled_from(
    [
        "dev", "staging", "test", "DEV", "Staging", "TEST",
        "prod",                # exact-match guard does not block "prod"
        "production-eu",       # suffix variant
        "pre-production",      # prefix variant
        "",                    # empty string
        "qa", "stage",
    ]
)

_ANY_PROFILE = st.one_of(_PRODUCTION_PROFILES, _NON_PRODUCTION_PROFILES)

# Stored override keys: small dict of safe identifier-style keys.
_STORED_KEYS = st.dictionaries(
    keys=st.from_regex(r"^[A-Z][A-Z0-9_]{0,12}$", fullmatch=True),
    values=st.text(min_size=1, max_size=8, alphabet="abcdef0123456789"),
    min_size=0,
    max_size=4,
)

# Vault outcome on the non-blocked path.
_VAULT_OUTCOME = st.sampled_from(
    ["success", "list_fail", "delete_fail_first", "delete_fail_after_one"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_production(profile: str) -> bool:
    """Mirror of the router's case-insensitive exact-match guard."""
    return profile.lower() == "production"


def _build_vault_for_outcome(
    outcome: str,
    stored: dict[str, str],
) -> _FakeVaultClient:
    """Return a Vault fake configured for the requested outcome.

    * ``"success"`` — every LIST + DELETE succeeds.
    * ``"list_fail"`` — LIST raises :class:`VaultWriteError`.
    * ``"delete_fail_first"`` — first DELETE raises (zero deletions).
    * ``"delete_fail_after_one"`` — one DELETE succeeds, then raises.
    """

    vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
    if outcome == "list_fail":
        vault.list_raise = VaultWriteError(
            operation="list",
            service_name=_SERVICE_NAME,
            key=None,
            status_code=500,
            message="injected list failure",
        )
    elif outcome == "delete_fail_first":
        vault.raise_on_delete_after = 0
    elif outcome == "delete_fail_after_one":
        vault.raise_on_delete_after = 1
    return vault


# ---------------------------------------------------------------------------
# Property 15a — Production guard blocks purge_vault=true (case-insensitive)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(profile=_PRODUCTION_PROFILES, stored=_STORED_KEYS)
def test_production_profile_blocks_purge_vault_true(
    profile: str,
    stored: dict[str, str],
    tmp_path: Path,
) -> None:
    """Property 15a — production + purge_vault=true → 403 + block audit.

    **Validates: Requirements 14.2, 14.6**

    The router rejects the request before any Compose / Vault call.
    The 403 envelope carries ``error="purge_vault_forbidden_in_production"``.
    A ``purge_vault_blocked_in_production`` audit row is written.
    The state cache and Vault store are unchanged.
    """

    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
    svc, audit, _, compose = _make_service(
        workspace_root=workspace, vault=vault
    )

    app = _build_app(svc, deployment_profile=profile)
    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 403, (
        f"Expected 403 for production profile {profile!r}; "
        f"got {response.status_code}: {response.text}"
    )
    body = response.json()
    detail = body["detail"]
    assert detail["error"] == "purge_vault_forbidden_in_production", (
        f"Wrong error envelope for production profile {profile!r}: "
        f"{detail!r}"
    )

    # Block audit row was written exactly once.
    block_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "purge_vault_blocked_in_production"
    ]
    assert len(block_rows) == 1, (
        f"Expected exactly 1 block audit row, got {len(block_rows)}; "
        f"actions={audit.actions!r}"
    )
    assert block_rows[0].outcome == "failed"
    assert block_rows[0].service_name == _SERVICE_NAME

    # No Compose stop ran; no Vault interaction at all.
    assert compose.stop_calls == [], (
        "Compose stop must NOT be invoked when guard fires"
    )
    assert vault.list_calls == []
    assert vault.delete_calls == []
    # The state cache stays unchanged (still "running").
    assert svc.state_cache[_SERVICE_NAME].state == "running"

    # Mutually exclusive: no purge audit rows on the 403 path.
    purge_rows = [
        e for e in audit.write_with_retry_calls
        if e.action in {"vault_overrides_purged", "vault_purge_partial_failure"}
    ]
    assert purge_rows == [], (
        f"Purge audit rows must NOT appear on the 403 path; got {purge_rows!r}"
    )


# ---------------------------------------------------------------------------
# Property 15b — Non-production + purge_vault=true → 200 + Vault purge runs
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(profile=_NON_PRODUCTION_PROFILES, stored=_STORED_KEYS)
def test_non_production_profile_runs_vault_purge(
    profile: str,
    stored: dict[str, str],
    tmp_path: Path,
) -> None:
    """Property 15b — non-production + purge_vault=true → 200 + purge runs.

    **Validates: Requirements 14.3, 14.6**

    The guard does not fire for any profile that does not exactly
    case-fold to ``"production"``. The Compose stop runs, then the
    Vault LIST + DELETE chain runs (best-effort). The block audit
    row is NOT written; the ``vault_overrides_purged`` row IS written.
    """

    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
    svc, audit, _, compose = _make_service(
        workspace_root=workspace, vault=vault
    )

    app = _build_app(svc, deployment_profile=profile)
    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 200, (
        f"Expected 200 for non-production profile {profile!r}; "
        f"got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["state"] == "stopped"
    UUID(body["correlation_id"])  # parses cleanly

    # Compose stop ran exactly once.
    assert len(compose.stop_calls) == 1, (
        f"Compose stop must run exactly once on non-production path; "
        f"got {len(compose.stop_calls)}"
    )

    # Vault LIST ran once; DELETE ran once per stored key.
    assert vault.list_calls == [_SERVICE_NAME], (
        f"Vault LIST must run exactly once; got {vault.list_calls!r}"
    )
    assert len(vault.delete_calls) == len(stored), (
        f"Vault DELETE must run once per stored key "
        f"({len(stored)}); got {len(vault.delete_calls)}"
    )
    # All keys removed.
    assert vault.stored[_SERVICE_NAME] == {}

    # Audit chain: stop(success) → vault_overrides_purged(success).
    assert audit.actions == ["stop", "vault_overrides_purged"], (
        f"Wrong audit chain on non-production happy path: {audit.actions!r}"
    )
    purge_row = audit.write_with_retry_calls[1]
    assert purge_row.outcome == "success"
    assert purge_row.details_json == {
        "service_name": _SERVICE_NAME,
        "deleted_paths_count": len(stored),
    }

    # Mutually exclusive: no block row on the 200 path.
    assert "purge_vault_blocked_in_production" not in audit.actions


# ---------------------------------------------------------------------------
# Property 15c — purge_vault=false → Vault never touched on any profile
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(profile=_ANY_PROFILE, stored=_STORED_KEYS)
def test_purge_vault_false_never_touches_vault(
    profile: str,
    stored: dict[str, str],
    tmp_path: Path,
) -> None:
    """Property 15c — purge_vault=false → Vault never touched, any profile.

    **Validates: Requirements 14.1, 14.6** (default-flag backwards-compat)

    Whether the body explicitly sets ``purge_vault=False`` or omits it
    entirely, the production guard does NOT fire, Compose stop runs,
    Vault is NOT enumerated, and only the canonical ``stop`` audit
    row is written.
    """

    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
    svc, audit, _, compose = _make_service(
        workspace_root=workspace, vault=vault
    )

    app = _build_app(svc, deployment_profile=profile)
    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": False},
    )

    assert response.status_code == 200, (
        f"purge_vault=false must always succeed; got "
        f"{response.status_code} for profile {profile!r}: {response.text}"
    )
    assert response.json()["state"] == "stopped"

    # Compose stop ran exactly once on every profile.
    assert len(compose.stop_calls) == 1

    # Vault is untouched regardless of profile.
    assert vault.list_calls == [], (
        f"Vault LIST must NOT run when purge_vault=false; "
        f"profile={profile!r}, got {vault.list_calls!r}"
    )
    assert vault.delete_calls == []
    assert vault.stored[_SERVICE_NAME] == stored, (
        "Vault store must remain intact when purge_vault=false"
    )

    # Only the canonical stop audit row.
    assert audit.actions == ["stop"], (
        f"Only the 'stop' audit row is expected when purge_vault=false; "
        f"got {audit.actions!r}"
    )
    assert "purge_vault_blocked_in_production" not in audit.actions


# ---------------------------------------------------------------------------
# Property 15d — Vault failures stay best-effort (200 + partial_failure audit)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    profile=_NON_PRODUCTION_PROFILES,
    stored=st.dictionaries(
        keys=st.from_regex(r"^[A-Z][A-Z0-9_]{0,8}$", fullmatch=True),
        values=st.text(min_size=1, max_size=4, alphabet="abc"),
        min_size=2,            # need >=2 keys for delete_fail_after_one
        max_size=4,
    ),
    outcome=_VAULT_OUTCOME,
)
def test_vault_failure_is_best_effort(
    profile: str,
    stored: dict[str, str],
    outcome: str,
    tmp_path: Path,
) -> None:
    """Property 15d — Vault LIST/DELETE failures stay best-effort.

    **Validates: Requirements 14.4, 14.6**

    Compose stop has already committed by the time the Vault purge
    runs. Any LIST or DELETE failure must:
    - Stay HTTP 200 (the canonical stop succeeded).
    - Emit ``vault_purge_partial_failure`` with ``partial_count``
      reflecting the number of keys deleted before the failure.
    - Leave the lifecycle state at ``stopped``.
    - NOT emit the success ``vault_overrides_purged`` row.
    """

    workspace = _build_workspace(tmp_path)
    vault = _build_vault_for_outcome(outcome, stored)
    svc, audit, _, compose = _make_service(
        workspace_root=workspace, vault=vault
    )

    app = _build_app(svc, deployment_profile=profile)
    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 200, (
        f"Vault failures must stay 200 (best-effort); got "
        f"{response.status_code}: {response.text}"
    )
    assert response.json()["state"] == "stopped"

    # Compose stop ran once regardless of Vault outcome.
    assert len(compose.stop_calls) == 1

    if outcome == "success":
        # Sanity branch — full purge ran. partial_failure row absent.
        assert audit.actions == ["stop", "vault_overrides_purged"]
        assert (
            "vault_purge_partial_failure" not in audit.actions
        )
    else:
        # Every failure variant emits exactly one partial_failure row.
        assert audit.actions == ["stop", "vault_purge_partial_failure"], (
            f"Failure outcome={outcome!r} must emit partial_failure row; "
            f"got {audit.actions!r}"
        )
        failure_row = audit.write_with_retry_calls[1]
        assert failure_row.outcome == "failed"
        assert failure_row.details_json["service_name"] == _SERVICE_NAME
        assert failure_row.details_json["error_type"] == "VaultWriteError"

        partial_count = failure_row.details_json["partial_count"]
        if outcome == "list_fail":
            assert partial_count == 0, (
                "LIST failure → zero deletions → partial_count=0"
            )
            assert vault.delete_calls == []
        elif outcome == "delete_fail_first":
            assert partial_count == 0
            # First DELETE was attempted (recorded) before raising.
            assert len(vault.delete_calls) == 1
        elif outcome == "delete_fail_after_one":
            assert partial_count == 1
            # Two DELETEs attempted (the second one raised).
            assert len(vault.delete_calls) >= 2

        # The success row never appears on the failure path.
        assert "vault_overrides_purged" not in audit.actions

    # Lifecycle state must reach "stopped" on every variant.
    assert svc.state_cache[_SERVICE_NAME].state == "stopped"


# ---------------------------------------------------------------------------
# Property 15e — Determinism: same inputs → same outcome on repeated calls
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    profile=_ANY_PROFILE,
    purge_vault=st.booleans(),
    stored=_STORED_KEYS,
)
def test_guard_decision_is_deterministic(
    profile: str,
    purge_vault: bool,
    stored: dict[str, str],
    tmp_path: Path,
) -> None:
    """Property 15e — same ``(profile, purge_vault)`` → same status code.

    **Validates: Requirements 14.2, 14.6**

    Two independent invocations against fresh service instances must
    produce identical HTTP status codes. The guard is a pure function
    of ``(deployment_profile, purge_vault)``.
    """

    statuses: list[int] = []
    for _ in range(2):
        workspace = _build_workspace(tmp_path)
        vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
        svc, _audit, _vault, _compose = _make_service(
            workspace_root=workspace, vault=vault
        )
        app = _build_app(svc, deployment_profile=profile)
        response = TestClient(app).post(
            f"/admin/services/{_SERVICE_NAME}/stop",
            json={"purge_vault": purge_vault},
        )
        statuses.append(response.status_code)

    assert statuses[0] == statuses[1], (
        f"Non-deterministic guard outcome: first={statuses[0]}, "
        f"second={statuses[1]}; profile={profile!r}, "
        f"purge_vault={purge_vault!r}"
    )

    # The expected status is fully determined by (profile, purge_vault).
    expected = (
        403
        if (purge_vault and _is_production(profile))
        else 200
    )
    assert statuses[0] == expected, (
        f"Wrong status for profile={profile!r}, purge_vault={purge_vault!r}; "
        f"expected={expected}, got={statuses[0]}"
    )


# ---------------------------------------------------------------------------
# Property 15f — Audit emission discipline matrix
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    profile=_ANY_PROFILE,
    purge_vault=st.booleans(),
    stored=_STORED_KEYS,
)
def test_audit_emission_matrix(
    profile: str,
    purge_vault: bool,
    stored: dict[str, str],
    tmp_path: Path,
) -> None:
    """Property 15f — audit-row emission depends only on ``(profile, purge_vault)``.

    **Validates: Requirements 14.2, 14.3, 14.6**

    The four observable audit actions partition the profile/flag matrix:
    - 403 path  → exactly ``["purge_vault_blocked_in_production"]``.
    - 200 path, ``purge_vault=False`` → exactly ``["stop"]``.
    - 200 path, ``purge_vault=True``  → exactly
      ``["stop", "vault_overrides_purged"]`` (Vault success — fakes
      always succeed on this property).

    The ``purge_vault_blocked_in_production`` row never co-occurs with
    ``vault_overrides_purged`` or ``vault_purge_partial_failure``.
    """

    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(stored={_SERVICE_NAME: dict(stored)})
    svc, audit, _, compose = _make_service(
        workspace_root=workspace, vault=vault
    )

    app = _build_app(svc, deployment_profile=profile)
    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": purge_vault},
    )

    if purge_vault and _is_production(profile):
        assert response.status_code == 403
        assert audit.actions == ["purge_vault_blocked_in_production"], (
            f"403 path must emit only the block audit row; "
            f"got {audit.actions!r}"
        )
        # No purge rows on the block path.
        assert "vault_overrides_purged" not in audit.actions
        assert "vault_purge_partial_failure" not in audit.actions
        # Compose was never invoked.
        assert compose.stop_calls == []
    elif purge_vault:
        # Non-production + purge_vault=true → success purge.
        assert response.status_code == 200
        assert audit.actions == ["stop", "vault_overrides_purged"], (
            f"Non-production purge path must emit [stop, "
            f"vault_overrides_purged]; got {audit.actions!r}"
        )
        # Mutual-exclusion: block row never appears here.
        assert "purge_vault_blocked_in_production" not in audit.actions
    else:
        # purge_vault=false on any profile → only the stop row.
        assert response.status_code == 200
        assert audit.actions == ["stop"], (
            f"purge_vault=false must emit only the stop audit row; "
            f"got {audit.actions!r}"
        )
        assert "purge_vault_blocked_in_production" not in audit.actions
        assert "vault_overrides_purged" not in audit.actions
        assert "vault_purge_partial_failure" not in audit.actions


# ---------------------------------------------------------------------------
# Concrete regression tests (carrying over the legacy
# test_stop_lifecycle_purge_guard.py invariants as a strict superset)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile",
    ["production", "PRODUCTION", "Production", "PrOdUcTiOn"],
)
def test_case_insensitive_production_match_blocks(
    profile: str,
    tmp_path: Path,
) -> None:
    """Carry-over: the legacy single-helper test from
    ``test_stop_lifecycle_purge_guard.py`` exercised case folding on
    a stand-alone ``_guard`` function. This regression test pins the
    same invariant on the real router.
    """

    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(stored={_SERVICE_NAME: {}})
    svc, _audit, _, _ = _make_service(workspace_root=workspace, vault=vault)
    app = _build_app(svc, deployment_profile=profile)

    response = TestClient(app).post(
        f"/admin/services/{_SERVICE_NAME}/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]["error"]
        == "purge_vault_forbidden_in_production"
    )


def test_legacy_guard_helper_remains_pure_function() -> None:
    """Document the guard's pure-function shape (R14.2 invariant).

    The legacy ``_guard`` helper was a stand-alone function. This
    test re-establishes the same property here so refactors that
    accidentally introduce side effects into the production check
    fail fast.
    """

    def _guard(*, deployment_profile: str, purge_vault: bool) -> bool:
        """Return True iff the stop request should be blocked."""
        return purge_vault and deployment_profile.lower() == "production"

    # Production + purge_vault=True → blocked, regardless of casing.
    for prof in ("production", "PRODUCTION", "Production", "PrOdUcTiOn"):
        assert _guard(deployment_profile=prof, purge_vault=True) is True

    # Every other combination must be a pass-through.
    for prof in ("dev", "staging", "test", "prod", "production-eu", ""):
        assert _guard(deployment_profile=prof, purge_vault=True) is False
        assert _guard(deployment_profile=prof, purge_vault=False) is False

    # Production with purge_vault=False is also a pass-through.
    for prof in ("production", "PRODUCTION", "Production"):
        assert _guard(deployment_profile=prof, purge_vault=False) is False
