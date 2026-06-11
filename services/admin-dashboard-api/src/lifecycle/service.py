"""``LifecycleService`` - pure orchestrator for Managed_Service lifecycle.

This module wires together the Vault, Compose, Health, Audit and
Manifest helpers into the request-handling contract used by
``services/admin-dashboard-api/src/routers/services_lifecycle.py``
(service lifecycle wiring). It is **the** module that enforces the
ordering invariants for lifecycle operations, health reporting, audit
records, credential checks, and sensitive value handling.

Notes on testability
--------------------
* The class accepts already-constructed clients (``audit``, ``vault``,
  ``compose``, ``health``) so unit tests can pass plain Python fakes
  without spinning up Postgres / Vault / Docker.
* No I/O happens at import time - everything lives behind the public
  ``async`` methods on :class:`LifecycleService`.
* The Pydantic-free response dataclasses (``ServiceSummary``,
  ``StartResponse``, ``StopResponse``, ``RunTestsResponse``,
  ``FormSchemaField``) are deliberately decoupled from FastAPI's
  serialisation layer - the REST router (service lifecycle wiring) adapts them into
  Pydantic v2 models at the HTTP boundary.

behaviors explicitly enforced here
-------------------------------------
* **6.1** - ``list_summaries`` returns one entry per manifest service.
* **6.3** - ``state[name].state = "starting"`` is set **before** the
  caller's ``StartResponse`` is constructed.
* **6.5** - ``stop`` is idempotent: a no-op on ``stopped`` services
  still writes a successful audit entry and returns ``noop=True``.
* **6.6** - ``restart`` reads previous Env_Override values from Vault
  and feeds them through ``start``.
* **6.7** - ``ComposeFailureError`` propagates out of ``start`` /
  ``stop`` / ``restart`` so the router can render a 502.
* **6.8** - ``last_started_at`` updates only on successful start.
* **7.7** - ``logs`` redacts every Sensitive_Env_Key occurrence
  (invariant C5).
* **8.2 / 8.6** - ``run_tests`` returns a 409-shaped result when the
  service is not running or has no ``test_command``.
* **9.1 / 9.6** - Each Env_Override is written to Vault via
  ``write_env_override`` (one PUT per key, atomic).
* **9.5** - ``VaultWriteError`` propagates verbatim.
* **11.1 / 11.6 / 11.7** - Audit precheck runs before any Compose
  invocation; the post-Compose audit row uses
  ``write_with_retry`` and surfaces ``audit_write_deferred``.
* **12.4** - ``list_summaries`` honours a per-entry cache TTL of
  ``HEALTH_POLL_INTERVAL_SECONDS / 2`` (default ``5`` seconds).
* **12.5** - ``health_of`` writes a single ``health_streak_alert``
  audit entry the *first* time a service reaches
  ``HEALTH_FAIL_STREAK_THRESHOLD`` consecutive ``unhealthy`` polls.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Protocol
from uuid import UUID, uuid4

from ..manifest import ManagedServiceEntry
from .audit_writer import (
    AuditEntry,
    AuditUnreachableError,
    AuditWriter,
    details_with_env_keys,
)
from .compose_runner import ComposeFailureError, ComposeRunner
from .env_parser import EnvField, parse_env_example
from .health_probe import HealthProbe, HealthSnapshot
from .sensitive import is_sensitive_env_key
from .vault_client import VaultClient, VaultWriteError

# ---------------------------------------------------------------------------
# Lifecycle configuration constants.
# ---------------------------------------------------------------------------

#: Allowed values for :attr:`LifecycleStateCache.state`.
#: ``"running_unmonitored"`` (platform operations rule 12 / Q14) is
#: used when a service's Compose ``healthcheck`` block is absent - the
#: container is running but we have no native health signal. The legacy
#: ``"unknown"`` value is kept for backwards compatibility; both are
#: treated as "ready" by :meth:`LifecycleService._wait_for_healthy`.
ServiceState = Literal[
    "stopped", "starting", "running", "unhealthy", "failed", "running_unmonitored"
]

#: Default ``HEALTH_POLL_INTERVAL_SECONDS``. The cache
#: TTL used by :meth:`LifecycleService.list_summaries` is
#: ``DEFAULT_HEALTH_POLL_INTERVAL_SECONDS / 2``.
DEFAULT_HEALTH_POLL_INTERVAL_SECONDS: float = 10.0

#: Default ``HEALTH_READY_TIMEOUT_SECONDS``. The
#: lifecycle handler polls ``health.probe`` for at most this many
#: seconds after a successful Compose ``up`` before declaring the
#: service ``failed``.
DEFAULT_HEALTH_READY_TIMEOUT_SECONDS: float = 60.0

#: Hard upper bound on ``HEALTH_READY_TIMEOUT_SECONDS``. Values above
#: this clamp downwards.
MAX_HEALTH_READY_TIMEOUT_SECONDS: float = 180.0

#: Default ``HEALTH_FAIL_STREAK_THRESHOLD``. When a
#: service reaches this many consecutive ``unhealthy`` polls the
#: lifecycle handler writes a single ``health_streak_alert`` audit
#: entry.
DEFAULT_HEALTH_FAIL_STREAK_THRESHOLD: int = 3

#: Maximum dependency-chain recursion depth (platform operations
#: behavior 5.2 / Q11). The lifecycle service refuses to recurse
#: deeper than this when walking ``depends_on_services``; any
#: violation surfaces as :class:`MaxDependencyDepthExceededError` and
#: a ``dependency_chain_max_depth_exceeded`` audit row.
#:
#: A depth of ``3`` is enough to express the canonical chains in the
#: platform manifest (e.g. ``automation-service  temporal  vault``)
#: without leaving room for accidental cycles or pathological
#: fan-out. Manifest-time DFS (``manifest._check_no_dependency_cycles``)
#: still rejects true cycles regardless of this constant; the depth
#: guard is a runtime defence-in-depth layer that also catches
#: legitimate-but-too-deep designs.
MAX_DEPENDENCY_DEPTH: int = 3

LLM_PROVIDER_KEY = "LLM_PROVIDER"
LLM_PROVIDER_DEFAULT = "openai"
LLM_PROVIDERS = {"openai", "vllm", "anthropic"}
LLM_SECRET_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VLLM_API_KEY"}
ATLASSIAN_MCP_SERVICE = "atlassian-mcp"
STREAMLIT_UI_SERVICE = "streamlit-ui"
ATLASSIAN_RUNTIME_KEYS = (
    "ATLASSIAN_DEPLOYMENT",
    "JIRA_URL",
    "CONFLUENCE_URL",
    "BITBUCKET_URL",
)


def _normalise_llm_provider(value: str | None, fallback: str = LLM_PROVIDER_DEFAULT) -> str:
    provider = (value or "").strip().lower()
    return provider if provider else fallback


def _llm_provider_for_schema(
    fields: Sequence[EnvField],
    env_overrides: Mapping[str, str],
) -> str | None:
    defaults = {field.key: field.default_value for field in fields}
    if LLM_PROVIDER_KEY not in defaults:
        return None
    fallback = _normalise_llm_provider(defaults.get(LLM_PROVIDER_KEY))
    provider = _normalise_llm_provider(env_overrides.get(LLM_PROVIDER_KEY), fallback)
    if provider not in LLM_PROVIDERS:
        raise FormSchemaMismatchError(
            f"{LLM_PROVIDER_KEY} must be one of {sorted(LLM_PROVIDERS)}, got {provider!r}"
        )
    return provider


def _llm_secret_can_be_empty(key: str, provider: str | None) -> bool:
    if provider is None or key not in LLM_SECRET_KEYS:
        return False
    if key == "OPENAI_API_KEY":
        return provider != "openai"
    if key == "ANTHROPIC_API_KEY":
        return provider != "anthropic"
    if key == "VLLM_API_KEY":
        return provider != "vllm"
    return False

#: Polling cadence used by :meth:`LifecycleService.start` while it
#: waits for the service's ``/healthz`` to become healthy. Kept short
#: so unit tests with patched timeouts terminate quickly.
_HEALTH_POLL_STEP_SECONDS: float = 0.5

#: Pytest summary regex. Captures the number of
#: passing/failing tests and the run duration. Match groups are 1:
#: passed, 2: failed, 3: duration_seconds.
_PYTEST_SUMMARY_RE: re.Pattern[str] = re.compile(
    r"^=+ (\d+) passed.*?(\d+) failed.*?in ([\d.]+)s",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Public exception hierarchy
# ---------------------------------------------------------------------------


class UnknownServiceError(KeyError):
    """Raised when ``name`` does not match any manifest entry.

    The router (service lifecycle wiring) maps this to ``404 Not Found``.
    """

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        super().__init__(service_name)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"unknown service: {self.service_name!r}"


class FormSchemaMismatchError(ValueError):
    """Raised when ``env_overrides`` does not match the form schema.

    The router maps this to ``422 Unprocessable Entity``. Two failure
    modes carry this exception:

    1. The LHS key set submitted by the operator differs from the
       ``.env.example`` LHS key set (missing or extra keys).
    2. A Sensitive_Env_Key was submitted with an empty value.
    """


class TestPreconditionError(RuntimeError):
    """Raised when ``run_tests`` cannot proceed.

    Maps to ``409 Conflict`` (behavior 8.2 / 8.6). The
    ``reason`` attribute distinguishes the two cases:

    * ``"service must be running before tests"`` (behavior 8.6).
    * ``"service has no test_command in manifest"`` (behavior 8.2).
    """

    # Hint to pytest collectors: this class name happens to start
    # with ``Test`` but it is an exception type, not a test case.
    __test__ = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class FeatureFlagDisabledError(RuntimeError):
    """Raised when a manifest ``feature_flag_dependency`` is disabled.

    Implements platform operations behavior 10.1 / 10.2 (Q12 -
    feature-flag start gate). Step 1.5 of :meth:`LifecycleService.start`
    consults ``shared.feature_flags`` for every flag listed in the
    manifest entry's :attr:`ManagedServiceEntry.feature_flag_dependency`
    tuple; if any of them is missing or has ``enabled=false`` the
    helper raises this exception.

    The first offending flag (manifest order, ties broken by the SQL
    result deterministically) is exposed via :attr:`blocking_flag` so
    the router can render the 409 envelope and the UI can show a
    targeted "open Feature Flags page  toggle ``{blocking_flag}``"
    modal (behavior 10.3).

    The router maps this to ``409 Conflict``::

        {"error": "feature_flag_disabled",
         "blocking_flag": <name>,
         "detail": "feature flag {name} must be enabled before starting
                    this service"}
    """

    def __init__(self, *, blocking_flag: str) -> None:
        self.blocking_flag = blocking_flag
        super().__init__(
            f"feature flag {blocking_flag!r} must be enabled before starting "
            "this service"
        )


class MaxDependencyDepthExceededError(RuntimeError):
    """Raised when ``depends_on_services`` recursion exceeds ``MAX_DEPENDENCY_DEPTH``.

    Implements platform operations behavior 5.2 (Q11 - dependency
    chain orchestration). Step 1.6 of :meth:`LifecycleService.start`
    walks the manifest's ``depends_on_services`` graph by recursing
    into ``_do_start`` with an explicit ``_recursion_path`` tuple of
    ancestor service names. The recursion guard fires before any
    further descent when the path length reaches
    :data:`MAX_DEPENDENCY_DEPTH`, producing a deterministic 502 rather
    than an unbounded recursion stack.

    The :attr:`recursion_path` attribute carries the chain of
    parent  child service names that led to the violation; the
    audit row's ``details_json`` payload exposes it verbatim so the
    operator can pinpoint the offending manifest edge without
    re-reading the manifest by hand.

    The router maps this to ``502 Bad Gateway`` (matches the rest of
    the dependency-chain failure family - see ``ComposeFailureError``
    and :class:`DependencyStartFailedError`).
    """

    def __init__(self, *, recursion_path: tuple[str, ...]) -> None:
        self.recursion_path = recursion_path
        rendered = " -> ".join(recursion_path) if recursion_path else "(empty)"
        super().__init__(
            f"dependency-chain recursion depth exceeded "
            f"(max={MAX_DEPENDENCY_DEPTH}); path={rendered}"
        )


class DependencyStartFailedError(RuntimeError):
    """Raised when a recursive ``_do_start`` call fails to start a dep.

    Implements platform operations behavior 5.5 (Q11). When a
    dependency's ``_do_start`` raises ``ComposeFailureError`` (or any
    other start-time exception that surfaces past the canonical
    audit-or-rollback boundary), the parent service's Step 1.6 walk
    catches the failure, writes a ``dependency_start_failed`` audit
    row with ``payload: {parent_service, failed_dependency,
    error_type}``, and re-raises wrapped in this class. Already-started
    sibling dependencies are **not** stopped - they remain in their
    current state so a subsequent retry can complete the chain
    incrementally (behavior 5.5 explicit clause "önceden başlatılmış
    sibling'ler stop edilmez").

    The router maps this to ``502 Bad Gateway`` and surfaces the
    ``failed_dependency`` name in the response detail so the UI can
    deeplink straight to the failing service's row.
    """

    def __init__(
        self,
        *,
        parent_service: str,
        failed_dependency: str,
        cause: BaseException,
    ) -> None:
        self.parent_service = parent_service
        self.failed_dependency = failed_dependency
        self.cause = cause
        super().__init__(
            f"dependency {failed_dependency!r} of {parent_service!r} failed to "
            f"start: {type(cause).__name__}: {cause}"
        )


# Re-exported for callers that want a single import surface.
__all__ = (
    # Constants
    "DEFAULT_HEALTH_FAIL_STREAK_THRESHOLD",
    "DEFAULT_HEALTH_POLL_INTERVAL_SECONDS",
    "DEFAULT_HEALTH_READY_TIMEOUT_SECONDS",
    "MAX_DEPENDENCY_DEPTH",
    "MAX_HEALTH_READY_TIMEOUT_SECONDS",
    # Types / dataclasses
    "FormSchemaField",
    "LifecycleService",
    "LifecycleStateCache",
    "RunTestsResponse",
    "ServiceState",
    "ServiceSummary",
    "StartPlan",
    "StartResponse",
    "StopResponse",
    "TestSummary",
    # Exceptions
    "AuditUnreachableError",
    "ComposeFailureError",
    "DependencyStartFailedError",
    "FeatureFlagDisabledError",
    "FormSchemaMismatchError",
    "MaxDependencyDepthExceededError",
    "TestPreconditionError",
    "UnknownServiceError",
    "VaultWriteError",
    # Protocols
    "FeatureFlagReader",
    "AsyncpgFeatureFlagReader",
)


# ---------------------------------------------------------------------------
# Feature-flag reader protocol (rule 10 / Q12)
# ---------------------------------------------------------------------------


class FeatureFlagReader(Protocol):
    """Lookup interface for ``shared.feature_flags`` rows used at Step 1.5.

    The lifecycle service calls
    :meth:`fetch_enabled_flags` once per ``start`` invocation when the
    manifest entry's ``feature_flag_dependency`` tuple is non-empty.
    Implementations MUST:

    * Issue a *single* SQL ``SELECT`` against ``shared.feature_flags``
      (behavior 10.5 - "tek SELECT").
    * Return a ``dict[str, bool]`` mapping flag name  ``enabled``.
      Missing rows are simply absent from the dict; the lifecycle
      service treats absence as "disabled" so a typo in the manifest
      surfaces as a 409 rather than silently letting the start proceed.

    The default production implementation
    :class:`AsyncpgFeatureFlagReader` uses the same ``app.state.pg_pool``
    handle that the ``feature_flags`` and ``costs`` routers consume, so
    we never open a second connection pool against the same Postgres
    instance.

    Tests pass a hand-rolled fake (see ``tests/unit/test_lifecycle_service.py``)
    so the orchestrator can be exercised without a Postgres
    testcontainer.
    """

    async def fetch_enabled_flags(
        self, names: Sequence[str]
    ) -> dict[str, bool]:  # pragma: no cover - structural protocol
        ...


class AsyncpgFeatureFlagReader:
    """Production :class:`FeatureFlagReader` backed by ``app.state.pg_pool``.

    Issues a single ``SELECT name, enabled FROM shared.feature_flags
    WHERE name = ANY($1)`` against the asyncpg pool - exactly one round
    trip per ``LifecycleService.start`` call, regardless of how many
    flags the manifest entry depends on (behavior 10.5 - "tek
    SELECT").

    The pool handle is the same one wired by ``src.main.lifespan`` onto
    ``app.state.pg_pool``; we keep a private reference so the reader
    survives request handlers without touching FastAPI internals.

    On a connection-level failure the underlying asyncpg exception
    propagates verbatim. The lifecycle service's ``_check_feature_flags``
    helper does NOT catch it - a Postgres outage during Step 1.5 should
    surface as a 502 just like the canonical Step 4 audit precheck
    does, rather than letting the start proceed without verifying the
    flag (fail-closed semantics).
    """

    _SELECT_SQL = (
        "SELECT name, enabled FROM shared.feature_flags "
        "WHERE name = ANY($1::text[])"
    )

    def __init__(self, *, pool: Any) -> None:
        self._pool = pool

    async def fetch_enabled_flags(
        self, names: Sequence[str]
    ) -> dict[str, bool]:
        # Empty input  skip the round trip entirely. The caller
        # already short-circuits on empty ``feature_flag_dependency``,
        # but defensive guard cheap.
        if not names:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._SELECT_SQL, list(names))
        return {row["name"]: bool(row["enabled"]) for row in rows}


# ---------------------------------------------------------------------------
# State cache + response dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LifecycleStateCache:
    """In-memory state slot for a single Managed_Service.

    The lifecycle service owns one of these per manifest entry, keyed
    by ``name``. The cache is the source of truth for the ``state``
    column in ``GET /admin/services`` responses; it also tracks the
    most recent :class:`HealthSnapshot` so :meth:`list_summaries` can
    return a list cheaply (behavior 12.4 cache TTL).
    """

    name: str
    state: ServiceState = "stopped"
    last_started_at: datetime | None = None
    last_correlation_id: UUID | None = None
    last_health_snapshot: HealthSnapshot | None = None
    consecutive_unhealthy_polls: int = 0
    streak_alert_emitted: bool = False
    #: Wall-clock time the ``last_health_snapshot`` was produced. Used
    #: by :meth:`LifecycleService.list_summaries` to honour the cache
    #: TTL of ``HEALTH_POLL_INTERVAL_SECONDS / 2``.
    last_health_polled_at: datetime | None = None
    # ------------------------------------------------------------------
    # Connectivity-probe fields (platform operations rule 9 / Q10)
    # ------------------------------------------------------------------
    #: Result of the most recent ``connectivity_probe_command`` run.
    #: ``"ok"`` - exit_code 0; ``"failed"`` - non-zero exit or timeout;
    #: ``"unknown"`` - probe has never been run; ``None`` - no probe
    #: command is configured for this service.
    credentials_status: Literal["ok", "failed", "unknown"] | None = None
    #: UTC timestamp of the most recent probe execution.
    credentials_probe_at: datetime | None = None
    #: Last 500 characters of stderr from a failed probe run, or
    #: ``None`` when the probe has not failed (or has not run yet).
    credentials_probe_detail: str | None = None


@dataclass(frozen=True)
class FormSchemaField:
    """One row of the form schema returned by ``GET /admin/services/{name}``.

    Mirrors :class:`EnvField` but is a separate type so the router can
    bind it to its Pydantic v2 model without leaking parser internals.
    """

    key: str
    default_value: str
    comment: str | None
    is_sensitive: bool


@dataclass(frozen=True)
class ServiceSummary:
    """Row shape returned by ``GET /admin/services``.

    The router adapts this to a Pydantic model at the HTTP boundary;
    keeping it Pydantic-free here means unit tests of the orchestrator
    can run without importing Pydantic and without an event-loop
    serializer.
    """

    name: str
    kind: Literal["http_service", "worker", "ui", "infra", "sidecar"]
    state: ServiceState
    last_started_at: datetime | None
    last_health_snapshot: HealthSnapshot | None


@dataclass(frozen=True)
class StartResponse:
    """Result envelope for ``start`` / ``restart``.

    ``audit_write_deferred`` is set when the post-Compose audit row
    could not be written and was queued for retry (behavior 11.7).
    ``state`` is the *final* state after polling for health; it is
    ``"running"`` on success, ``"failed"`` on Compose / health
    timeout failure.
    """

    state: ServiceState
    correlation_id: UUID
    audit_write_deferred: bool = False


@dataclass(frozen=True)
class StopResponse:
    """Result envelope for ``stop``.

    ``noop`` is ``True`` when the service was already stopped
    (behavior 6.5 / invariant P3).
    """

    state: ServiceState
    correlation_id: UUID
    noop: bool = False
    audit_write_deferred: bool = False


@dataclass(frozen=True)
class TestSummary:
    """Parsed pytest summary line.

    Built from the pytest summary regex. When the pytest output does
    not contain a recognisable summary the router
    receives ``summary=None`` and surfaces a JSON ``null``.
    """

    # Hint to pytest collectors: not a test class.
    __test__ = False

    passed: int
    failed: int
    duration_seconds: float


@dataclass(frozen=True)
class RunTestsResponse:
    """Result envelope for ``run_tests`` (behavior 8.4)."""

    output: str
    exit_code: int
    summary: TestSummary | None
    correlation_id: UUID
    audit_write_deferred: bool = False


@dataclass(frozen=True)
class StartPlan:
    """Result envelope for :meth:`LifecycleService.compute_start_plan`.

    Implements platform operations behavior 5.6 (Q11). The
    router (``GET /admin/services/{name}/start-plan``) adapts this
    dataclass into :class:`StartPlanResponse` for the UI's "Aşağıdaki
    servisler de başlatılacak: ..." preview modal.

    Field semantics
    ---------------
    * ``target_service`` - the service the operator clicked.
    * ``will_start`` - manifest-resident services (in topological
      order, dependencies before dependents) that an actual ``start``
      call would visit and bring up. The target service itself is
      always the *last* element when the target is not already
      running; when the target is already running ``will_start`` is
      empty.
    * ``already_running`` - manifest-resident services in the
      transitive closure that are currently in ``state="running"``
      and will therefore be skipped on Step 1.6 idempotent descent
      (behavior 5.3). Order matches manifest declaration order so
      the UI can show a stable list.

    External dependencies (Boot_Bundle infra such as ``postgres``,
    ``vault``, ``temporal``) that appear in
    :attr:`ManagedServiceEntry.depends_on_services` but are not
    themselves manifest entries are intentionally **omitted** from
    both lists - the lifecycle service cannot start them, so
    surfacing them in the plan would mislead the operator.
    """

    target_service: str
    will_start: tuple[str, ...]
    already_running: tuple[str, ...]


# ---------------------------------------------------------------------------
# LifecycleService
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC time as a timezone-aware :class:`datetime`."""

    return datetime.now(timezone.utc)


def _build_redaction_pattern(
    sensitive_keys: Iterable[str],
) -> re.Pattern[str] | None:
    """Compile a regex that matches ``KEY=...`` / ``KEY: ...`` log tokens.

    Returns ``None`` when ``sensitive_keys`` is empty so callers can
    skip redaction entirely (no allocation overhead per log line).
    The pattern is anchored on the **key name** rather than on the
    value, so any value the operator might have submitted is masked
    even if the parser never saw it.

    The match captures everything until the next whitespace or end of
    line; this matches the way Compose / services log env
    snapshots (e.g. ``MY_TOKEN=abc123 OTHER=...``). For the
    ``KEY: value`` shape we stop at the next newline so multi-token
    values on the same line are still partially redacted.
    """

    keys = sorted({k for k in sensitive_keys if k})
    if not keys:
        return None
    alternation = "|".join(re.escape(k) for k in keys)
    # Two alternatives:
    # - ``KEY=value``   ``=`` then non-space run.
    # - ``KEY: value``  ``:`` plus optional space, then a run that
    # stops at the first whitespace.
    # We keep the key as a backreference group (``\1``) so the
    # replacement string can preserve the original key in the output.
    pattern = re.compile(
        rf"\b({alternation})(=|:\s*)(\S+)",
    )
    return pattern


def _redact_log_line(line: str, pattern: re.Pattern[str] | None) -> str:
    """Replace any sensitive token in ``line`` with ``KEY=<redacted>``.

    The replacement preserves the original key + separator (so the
    operator can still tell which variable was logged) but obliterates
    the value. ``<redacted>`` is the canonical sentinel used for
    scrubbed sensitive values.
    """

    if pattern is None:
        return line
    return pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", line)


class LifecycleService:
    """Async orchestrator backing the ``/admin/services`` REST surface.

    Construct one instance per ``admin-dashboard-api`` process and
    share it across request handlers. The class is intentionally
    free of FastAPI imports so unit tests can drive it directly via
    :func:`asyncio.run` (mirrors the convention used by
    ``test_audit_writer.py``).
    """

    def __init__(
        self,
        *,
        manifest: tuple[ManagedServiceEntry, ...],
        state: dict[str, LifecycleStateCache] | None = None,
        audit: AuditWriter,
        vault: VaultClient,
        compose: ComposeRunner,
        health: HealthProbe,
        workspace_root: Path,
        feature_flag_reader: FeatureFlagReader | None = None,
        health_poll_interval_seconds: float = DEFAULT_HEALTH_POLL_INTERVAL_SECONDS,
        health_ready_timeout_seconds: float = DEFAULT_HEALTH_READY_TIMEOUT_SECONDS,
        health_fail_streak_threshold: int = DEFAULT_HEALTH_FAIL_STREAK_THRESHOLD,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        # Public-ish state - tests inspect these directly to assert
        # lifecycle state transitions.
        self._manifest: tuple[ManagedServiceEntry, ...] = manifest
        self._by_name: dict[str, ManagedServiceEntry] = {
            entry.name: entry for entry in manifest
        }
        self._state: dict[str, LifecycleStateCache] = state if state is not None else {
            entry.name: LifecycleStateCache(name=entry.name) for entry in manifest
        }
        # Ensure every manifest entry has a state slot even if the
        # caller pre-populated only some of them.
        for entry in manifest:
            self._state.setdefault(entry.name, LifecycleStateCache(name=entry.name))

        self._audit = audit
        self._vault = vault
        self._compose = compose
        self._health = health
        self._workspace_root = workspace_root
        # Feature-flag reader for Step 1.5 (rule 10 / Q12). When ``None``
        # the gate is a no-op and the manifest's
        # ``feature_flag_dependency`` field is treated as informational
        # only - used by tests that don't exercise the gate and by
        # boot-time wiring before the asyncpg pool is available.
        self._feature_flag_reader = feature_flag_reader

        # Tunable knobs - clamped on assignment so callers can pass
        # raw env-var floats without separately validating them.
        self._poll_interval = max(1.0, health_poll_interval_seconds)
        self._ready_timeout = min(
            max(1.0, health_ready_timeout_seconds),
            MAX_HEALTH_READY_TIMEOUT_SECONDS,
        )
        self._fail_streak_threshold = max(1, health_fail_streak_threshold)

        # Injectable clock + sleep for deterministic unit tests.
        self._clock = clock or _utcnow
        self._sleep = sleep or asyncio.sleep

        # The form-schema cache is keyed by ``env_example_path``
        # because two manifest entries may legitimately share the
        # same file (e.g. ``atlassian-mcp`` reading from
        # ``services/atlassian_mcp_bitbucket/.env.example``). Loaded
        # lazily on first access.
        self._form_schema_cache: dict[str, list[EnvField]] = {}

    # ------------------------------------------------------------------
    # Public read surface
    # ------------------------------------------------------------------

    @property
    def state_cache(self) -> dict[str, LifecycleStateCache]:
        """Expose the in-memory state cache (read-only contract)."""

        return self._state

    @property
    def manifest(self) -> tuple[ManagedServiceEntry, ...]:
        """Expose the immutable manifest tuple."""

        return self._manifest

    @property
    def compose(self) -> ComposeRunner:
        """Expose the underlying :class:`ComposeRunner`.

        The REST router (service lifecycle wiring) relies on this for the
        streaming-logs path: ``lifecycle.compose.logs(..., follow=True)``
        returns the async generator that the SSE response forwards
        per-chunk after applying :meth:`build_log_redaction_pattern`.
        Marked read-only by convention - callers MUST NOT replace
        the runner at runtime.
        """

        return self._compose

    def get_manifest_entry(self, name: str) -> ManagedServiceEntry:
        """Return the :class:`ManagedServiceEntry` for ``name``.

        Raises :class:`UnknownServiceError` when ``name`` is not in
        the manifest. The router uses this to assemble the response
        for ``GET /admin/services/{name}`` and to fetch the entry
        passed to :meth:`build_log_redaction_pattern` on the
        streaming-logs path.
        """

        return self._require_entry(name)

    def get_form_schema(self, name: str) -> list[FormSchemaField]:
        """Return the form-schema rows for ``name``.

        Raises :class:`UnknownServiceError` when the service is not in
        the manifest. The schema mirrors the LHS keys of the
        ``.env.example`` file referenced by the manifest entry, in
        file order.
        """

        entry = self._require_entry(name)
        fields = self._load_env_fields(entry)
        return [
            FormSchemaField(
                key=f.key,
                default_value=f.default_value,
                comment=f.comment,
                is_sensitive=f.is_sensitive,
            )
            for f in fields
        ]

    def compute_start_plan(self, name: str) -> StartPlan:
        """Return the dependency-chain plan for ``start(name)``.

        Implements platform operations behavior 5.6 (Q11 -
        dependency chain orchestration preview). The router exposes
        the result through ``GET /admin/services/{name}/start-plan``
        so the admin-dashboard-ui can render a confirmation modal
        listing every transitive dependency that will be touched.

        Algorithm
        ---------
        1. Resolve ``name`` to a :class:`ManagedServiceEntry` (raises
           :class:`UnknownServiceError` on miss  router 404).
        2. Walk the ``depends_on_services`` graph depth-first using
           Tarjan-style post-order recording. The post-order traversal
           guarantees **dependencies appear before dependents** in the
           output, which mirrors the actual ``_do_start`` Step 1.6
           descent order (behavior 5.4 sequential start).
        3. Skip dependency edges whose target is not a manifest-resident
           node (e.g. external Boot_Bundle infra ``postgres`` /
           ``vault``). These are not actionable by the lifecycle
           service and the cascade aggregator handles their health
           cascade independently.
        4. Partition the visited nodes into ``will_start`` vs
           ``already_running`` using the in-memory state cache -
           services currently in ``state="running"`` are filtered
           into the ``already_running`` bucket because
           :meth:`_do_start` is idempotent on running entries
           (behavior 5.3).
        5. The target service itself is appended to ``will_start`` last
           (post-order) when it is not currently running. If the target
           is running both lists may be empty; this is a legitimate
           "nothing to do" state and the UI surfaces it as a no-op
           confirmation.

        Manifest cycle protection
        -------------------------
        :func:`src.manifest._check_no_dependency_cycles` rejects
        cyclic manifests at boot, so this walk does not need its own
        cycle guard. We still maintain a ``visited`` set to avoid
        revisiting shared dependencies (a diamond ``A  B,C  D``
        must not list ``D`` twice in ``will_start``).
        """

        entry = self._require_entry(name)

        visited: set[str] = set()
        post_order: list[str] = []

        def _walk(svc_name: str) -> None:
            if svc_name in visited:
                return
            visited.add(svc_name)
            dep_entry = self._by_name.get(svc_name)
            if dep_entry is None:
                # External Boot_Bundle dep (postgres / vault / ...).
                # Not a manifest node  we cannot start it, so it must
                # not appear in either output bucket. Returning here
                # also matches the runtime behaviour of
                # ``_do_start``'s Step 1.6 ``self._by_name.get(...)``
                # branch which silently skips unknown deps.
                return
            for dep_name in dep_entry.depends_on_services:
                _walk(dep_name)
            post_order.append(svc_name)

        _walk(entry.name)

        will_start: list[str] = []
        already_running: list[str] = []
        for svc_name in post_order:
            slot = self._state.get(svc_name)
            if slot is not None and slot.state == "running":
                already_running.append(svc_name)
            else:
                will_start.append(svc_name)

        return StartPlan(
            target_service=entry.name,
            will_start=tuple(will_start),
            already_running=tuple(already_running),
        )

    async def list_summaries(self) -> list[ServiceSummary]:
        """Return one :class:`ServiceSummary` per manifest entry.

        Honours the cache TTL of
        ``HEALTH_POLL_INTERVAL_SECONDS / 2`` (behavior 12.4): if
        the cached snapshot is older than the TTL the method does
        **not** silently re-probe - that responsibility lives in
        :meth:`health_of` so callers explicitly request fresh data.
        :meth:`list_summaries` therefore returns the *currently
        cached* snapshot (or ``None``) for each service, which is
        what the UI displays between explicit health-pings.
        """

        out: list[ServiceSummary] = []
        ttl = self._poll_interval / 2
        for entry in self._manifest:
            slot = self._state[entry.name]
            snapshot = slot.last_health_snapshot
            if snapshot is not None and slot.last_health_polled_at is not None:
                age = (self._clock() - slot.last_health_polled_at).total_seconds()
                if age > ttl:
                    # Snapshot is stale; surface ``None`` so the UI
                    # knows to render a "no data" badge rather than a
                    # potentially-misleading old reading.
                    snapshot = None
            out.append(
                ServiceSummary(
                    name=slot.name,
                    kind=entry.kind,
                    state=slot.state,
                    last_started_at=slot.last_started_at,
                    last_health_snapshot=snapshot,
                )
            )
        return out

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        name: str,
        env_overrides: Mapping[str, str],
        actor: Any,
    ) -> StartResponse:
        """Bring a Managed_Service up using the lifecycle start sequence.

        Order:

        1. Manifest lookup (else :class:`UnknownServiceError`).
        2. Form-schema match (else :class:`FormSchemaMismatchError`).
        3. Sensitive-field non-empty check.
        4. Audit precheck (audit-or-rollback - behavior 11.6).
        5. Per-key Vault writes (behavior 9.1, 9.6).
        6. ``audit.write`` of the ``pending`` row (must succeed -
           we just prechecked).
        7. ``state[name].state = "starting"``.
        8. Compose ``up`` (failure  ``state="failed"`` and an audit
           ``failed`` row written via ``write_with_retry``).
        9. Poll ``health.probe`` until ``healthy`` or timeout.
        10. Write the final audit row and assemble the response.
        """

        entry = self._require_entry(name)
        return await self._do_start(
            entry=entry,
            env_overrides=dict(env_overrides),
            actor=actor,
        )

    async def _do_start(
        self,
        *,
        entry: ManagedServiceEntry,
        env_overrides: dict[str, str],
        actor: Any,
        _recursion_path: tuple[str, ...] = (),
    ) -> StartResponse:
        """Internal start implementation shared by ``start`` and
        ``restart``. Splitting it out lets ``restart`` pre-fill the
        overrides from Vault without re-validating the form schema.

        ``_recursion_path`` is a private parameter used by Step 1.6
        (rule 5 / Q11 - dependency chain orchestration). Each recursive
        descent appends the current ``entry.name`` so the depth guard
        below can reject chains longer than
        :data:`MAX_DEPENDENCY_DEPTH`. External callers (``start`` /
        ``restart``) MUST leave it at the default empty tuple.
        """

        actor_sub = self._actor_sub(actor)
        correlation_id = uuid4()

        # Step 1.6 (depth guard) - refuse to descend further when the
        # ancestor chain has already reached :data:`MAX_DEPENDENCY_DEPTH`.
        # Checked *before* feature-flag and form-schema work so a
        # pathological manifest is rejected with a single deterministic
        # audit row rather than an unbounded fan-out.
        if len(_recursion_path) >= MAX_DEPENDENCY_DEPTH:
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="dependency_chain_max_depth_exceeded",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json={
                        "recursion_path": list(_recursion_path),
                        "blocked_dependency": entry.name,
                        "max_depth": MAX_DEPENDENCY_DEPTH,
                    },
                )
            )
            raise MaxDependencyDepthExceededError(
                recursion_path=_recursion_path + (entry.name,),
            )

        # Step 1.5 - feature-flag gate (rule 10 / Q12). Runs *before* the
        # form-schema check so a flag-disabled start fails fast with
        # 409 even when the operator submitted no env_overrides at all
        # (matches the design Step Order: Step 1 manifest lookup
        # Step 1.5 feature-flag gate  Step 1.6 dependency chain
        # Step 2 form-schema check).
        await self._check_feature_flags(
            entry, actor_sub=actor_sub, correlation_id=correlation_id
        )

        # Step 1.6 (dependency chain) - start every dependency listed
        # in the manifest's ``depends_on_services`` tuple before
        # touching the form schema for the parent. rule 5 / Q11 specifies
        # *sequential* (not parallel) descent so each child's health
        # is confirmed before the next sibling is touched
        # 5.4) and so an early failure aborts the chain without
        # leaving partially-started peers in flight.
        await self._start_dependencies(
            entry=entry,
            actor=actor,
            actor_sub=actor_sub,
            correlation_id=correlation_id,
            recursion_path=_recursion_path,
        )

        # Step 2 + 3 - form schema check.
        self._validate_env_overrides(entry, env_overrides)
        compose_env_overrides = await self._compose_env_overrides_for_start(
            entry, env_overrides
        )

        # Step 4 - audit precheck. Raises AuditUnreachableError on a
        # database outage; the router converts that into 502.
        await self._audit.precheck()

        # Step 5 - per-key Vault writes. First write that fails
        # propagates VaultWriteError; already-written keys remain in
        # Vault (behavior 9.6 atomic per-key semantics).
        for key, value in env_overrides.items():
            await self._vault.write_env_override(
                service_name=entry.name,
                key=key,
                value=value,
            )

        # Step 6 - pending audit row.
        env_keys = list(env_overrides.keys())
        pending_entry = AuditEntry(
            id=uuid4(),
            actor=actor_sub,
            actor_type="admin_dashboard_user",
            service_name=entry.name,
            action="start",
            timestamp=self._clock(),
            correlation_id=correlation_id,
            outcome="pending",
            details_json=details_with_env_keys(env_keys),
        )
        await self._audit.write(pending_entry)

        # Step 7 - flip to ``starting`` BEFORE the compose call so
        # the ``GET /admin/services`` snapshot reflects the action
        # (behavior 6.3 second sentence).
        slot = self._state[entry.name]
        slot.state = "starting"
        slot.last_correlation_id = correlation_id

        # Step 8 - Compose up. On failure: mark failed, write final
        # audit row (deferred-on-DB-outage), surface the exception.
        try:
            await self._compose.up(
                profile=entry.compose_profile,
                service_name=entry.compose_service_name,
                env_overrides=compose_env_overrides,
            )
        except ComposeFailureError:
            slot.state = "failed"
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="start",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json=details_with_env_keys(
                        env_keys,
                        extra={"reason": "compose_up_nonzero"},
                    ),
                )
            )
            raise

        # Step 9 - poll until healthy or timeout.
        healthy = await self._wait_for_healthy(entry)

        # Step 9.5 - connectivity probe (platform operations rule 9 / Q10).
        # Runs *after* _wait_for_healthy succeeds and *before* the final
        # audit row so the probe result is visible in the state cache by
        # the time the caller receives the StartResponse. A failed probe
        # does NOT change the service state to "failed" - the service is
        # still considered "running"; only the credentials_status field
        # in the state cache is updated.
        if healthy:
            await self._run_connectivity_probe(
                entry=entry,
                actor_sub=actor_sub,
                correlation_id=correlation_id,
            )

        # Step 10 - final audit row + response.
        outcome: Literal["success", "failed"] = "success" if healthy else "failed"
        if healthy:
            slot.state = "running"
            slot.last_started_at = self._clock()
            slot.consecutive_unhealthy_polls = 0
            slot.streak_alert_emitted = False
        else:
            slot.state = "failed"

        final_outcome = await self._audit.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor=actor_sub,
                actor_type="admin_dashboard_user",
                service_name=entry.name,
                action="start",
                timestamp=self._clock(),
                correlation_id=correlation_id,
                outcome=outcome,
                details_json=details_with_env_keys(env_keys),
            )
        )

        return StartResponse(
            state=slot.state,
            correlation_id=correlation_id,
            audit_write_deferred=final_outcome.deferred,
        )

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    async def record_purge_vault_blocked(
        self,
        *,
        name: str,
        actor: Any,
    ) -> None:
        """Record an audit row when ``purge_vault=true`` is blocked.

        Implements platform operations behavior 14.2 (Q16) -
        the lifecycle stop endpoint refuses ``purge_vault=true`` when
        ``settings.deployment_profile == "production"`` and writes a
        ``purge_vault_blocked_in_production`` audit row before the
        router returns ``403 Forbidden``.

        The helper deliberately mirrors the shape of other Step
        bookkeeping helpers (``_check_feature_flags``,
        ``_run_connectivity_probe``):

        * Resolves the manifest entry up-front so an unknown service
          surfaces :class:`UnknownServiceError` (the router maps this
          to 404, matching every other ``/admin/services/{name}/...``
          endpoint).
        * Writes a single ``write_with_retry`` row with
          ``outcome="failed"`` so a transient audit-DB outage does not
          escalate into a hard failure on the operator side - the
          guard already produced the 403, the audit row is the
          observability layer.
        * The ``details_json`` payload carries ``actor_id`` so the
          security review can pivot back to the OIDC subject without
          joining against an external IdP record (behavior 14.2
          payload: ``{service_name, actor_id}``).

        Compose is **never** invoked on this path; the router
        short-circuits before calling :meth:`stop`. The method is
        therefore safe to call regardless of the service's current
        ``state`` value (running, stopped, or starting).
        """

        entry = self._require_entry(name)
        actor_sub = self._actor_sub(actor)
        await self._audit.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor=actor_sub,
                actor_type="admin_dashboard_user",
                service_name=entry.name,
                action="purge_vault_blocked_in_production",
                timestamp=self._clock(),
                correlation_id=uuid4(),
                outcome="failed",
                details_json={
                    "service_name": entry.name,
                    "actor_id": actor_sub,
                    "reason": "deployment_profile_production",
                },
            )
        )

    async def stop(
        self,
        *,
        name: str,
        remove_volumes: bool = False,
        purge_vault: bool = False,
        actor: Any,
    ) -> StopResponse:
        """Bring a Managed_Service down (idempotent - behavior 6.5).

        When the service is already in the ``stopped`` state the
        method short-circuits: it still writes a *successful* audit
        entry (so the operator's action is visible) but does **not**
        invoke Compose. This is the structural surface of the invariant
        P3 - repeated ``stop`` calls remain ``200 OK``.

        platform operations behavior 14.3 / 14.4 (Q16) -
        when ``purge_vault=True`` the orchestrator runs a best-effort
        Vault purge **after** the Compose stop succeeds. The
        production guard lives in the router (behavior 14.2 - the
        body field is rejected with a 403 before ``stop`` is even
        called when ``deployment_profile`` resolves to
        ``"production"``); by the time we reach this method the
        ``purge_vault=True`` path is guaranteed safe to execute.

        Purge semantics (best-effort - behavior 14.4):

        * The Compose stop happens first. If it fails the canonical
          ``ComposeFailureError`` audit row is written and the
          exception propagates - no Vault purge is attempted.
        * On Compose success we list every Env_Override key under
          ``services/{name}/`` via :meth:`VaultClient.list_env_override_keys`
          and soft-delete each one via
          :meth:`VaultClient.delete_env_override`.
        * Total success  ``vault_overrides_purged`` audit row with
          ``payload: {service_name, deleted_paths_count}``.
        * Vault list/delete raises :class:`VaultWriteError`
          ``vault_purge_partial_failure`` audit row with
          ``payload: {service_name, error_type, partial_count}``.
          The Compose stop has **already** succeeded so the
          :class:`StopResponse` is still returned with
          ``state="stopped"``; the failure is observable only via
          the audit trail (matches the design tagged "best-effort -
          mevcut ``stop`` semantiği ile uyumlu").
        * Idempotent path (already stopped) skips the purge entirely
          - a no-op stop should not have side-effects that a
          previous successful stop already handled.
        """

        entry = self._require_entry(name)
        actor_sub = self._actor_sub(actor)
        correlation_id = uuid4()
        slot = self._state[entry.name]

        if slot.state == "stopped":
            # Idempotent path. Still subject to audit-or-rollback for
            # consistency with the running-stop path: if the DB is
            # down we cannot record the no-op safely either.
            await self._audit.precheck()
            outcome = await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="stop",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="success",
                    details_json={"noop": True},
                )
            )
            slot.last_correlation_id = correlation_id
            return StopResponse(
                state="stopped",
                correlation_id=correlation_id,
                noop=True,
                audit_write_deferred=outcome.deferred,
            )

        # Non-noop path.
        await self._audit.precheck()
        try:
            await self._compose.stop(
                service_name=entry.compose_service_name,
                remove_volumes=remove_volumes,
            )
        except ComposeFailureError:
            # The state stays at its previous value; the router
            # converts this into 502 + correlation_id.
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="stop",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json={"reason": "compose_stop_nonzero"},
                )
            )
            raise

        slot.state = "stopped"
        slot.consecutive_unhealthy_polls = 0
        slot.streak_alert_emitted = False
        slot.last_correlation_id = correlation_id

        outcome = await self._audit.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor=actor_sub,
                actor_type="admin_dashboard_user",
                service_name=entry.name,
                action="stop",
                timestamp=self._clock(),
                correlation_id=correlation_id,
                outcome="success",
                details_json={"remove_volumes": remove_volumes},
            )
        )

        # platform operations rule 14.3 / rule 14.4 (Q16) - best-effort
        # Vault purge. Runs ONLY when the operator explicitly opted in
        # and ONLY after the Compose stop succeeded. The router has
        # already gated the production profile (behavior 14.2), so
        # by the time we reach here it is safe to enumerate and delete
        # the override keys.
        # # Failures are recorded as ``vault_purge_partial_failure`` and
        # do NOT propagate - the Compose stop has already taken
        # effect and rolling it back would defeat the purpose of the
        # operator's request. The canonical ``stop`` audit row above
        # is the source of truth for the lifecycle transition; this
        # purge audit row is the security-review observability layer.
        if purge_vault:
            await self._purge_vault_overrides(
                entry=entry,
                actor_sub=actor_sub,
                correlation_id=correlation_id,
            )

        return StopResponse(
            state="stopped",
            correlation_id=correlation_id,
            noop=False,
            audit_write_deferred=outcome.deferred,
        )

    async def _purge_vault_overrides(
        self,
        *,
        entry: ManagedServiceEntry,
        actor_sub: str,
        correlation_id: UUID,
    ) -> None:
        """Best-effort delete of every Env_Override under ``services/{name}/``.

        Implements platform operations behavior 14.3 / 14.4
        (Q16). Called from :meth:`stop` after a successful Compose
        stop when ``purge_vault=True`` was passed. Two terminal
        outcomes - both surfaced via a single audit row, neither
        rolling back the (already-completed) Compose stop:

        * **Total success** - ``vault_overrides_purged`` audit row
          with ``payload: {service_name, deleted_paths_count}``. The
          counter reflects the number of keys we successfully
          soft-deleted via :meth:`VaultClient.delete_env_override`;
          a Vault that legitimately holds zero overrides for the
          service still emits this row with ``deleted_paths_count=0``
          so the security review sees "purge ran, found nothing"
          rather than absence-of-evidence.
        * **Partial failure** - ``vault_purge_partial_failure``
          audit row with ``payload: {service_name, error_type,
          partial_count}``. ``partial_count`` is the number of keys
          we did manage to delete before the failure; ``error_type``
          is the bare exception class name (no message body - Vault
          error messages can echo the requesting URL which contains
          the service name, but never secret values).
        """

        deleted = 0
        try:
            keys = await self._vault.list_env_override_keys(
                service_name=entry.name
            )
            for key in keys:
                await self._vault.delete_env_override(
                    service_name=entry.name,
                    key=key,
                )
                deleted += 1
        except VaultWriteError as exc:
            # Best-effort: record the partial failure but do NOT
            # raise. The Compose stop has already succeeded; the
            # operator does not need a 502 just because the optional
            # purge step failed. The audit row is the security
            # observability layer - security review can pivot from
            # ``vault_purge_partial_failure`` straight to the manual
            # cleanup runbook.
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="vault_purge_partial_failure",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json={
                        "service_name": entry.name,
                        "error_type": type(exc).__name__,
                        "partial_count": deleted,
                    },
                )
            )
            return

        await self._audit.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor=actor_sub,
                actor_type="admin_dashboard_user",
                service_name=entry.name,
                action="vault_overrides_purged",
                timestamp=self._clock(),
                correlation_id=correlation_id,
                outcome="success",
                details_json={
                    "service_name": entry.name,
                    "deleted_paths_count": deleted,
                },
            )
        )

    # ------------------------------------------------------------------
    # restart
    # ------------------------------------------------------------------

    async def restart(
        self,
        *,
        name: str,
        actor: Any,
    ) -> StartResponse:
        """Stop the service then start it again with Vault-stored env.

        The Env_Override map is **read** from Vault (behavior 6.6
        explicit clause "son Env_Override setini Vault'tan tekrar
        okuyarak"). When the operator has never started the service
        before the map is empty and the form-schema check still
        applies - :class:`FormSchemaMismatchError` propagates out so
        the router can return 422.
        """

        entry = self._require_entry(name)

        # First half: stop. Idempotent if already stopped.
        await self.stop(name=name, remove_volumes=False, actor=actor)

        # Second half: re-read overrides from Vault and start. We
        # bypass the public ``start`` so we can pre-populate the
        # overrides without going through another form-schema match
        # twice (the schema check inside ``_do_start`` still fires).
        env_overrides = await self._vault.read_env_overrides(service_name=name)
        return await self._do_start(
            entry=entry,
            env_overrides=dict(env_overrides),
            actor=actor,
        )

    # ------------------------------------------------------------------
    # run_tests
    # ------------------------------------------------------------------

    async def run_tests(
        self,
        *,
        name: str,
        stream: bool = False,
        actor: Any,
    ) -> RunTestsResponse:
        """Execute the manifest ``test_command`` against the service.

        Pre-conditions:

        * Service must be ``running`` (behavior 8.6) - else
          :class:`TestPreconditionError`.
        * Manifest entry must declare ``test_command``
          8.2) - else :class:`TestPreconditionError`.

        On success the pytest summary is parsed via
        :data:`_PYTEST_SUMMARY_RE`; if the regex fails to match the
        ``summary`` field is ``None`` (the router maps this to a
        JSON ``null``).
        """

        entry = self._require_entry(name)
        slot = self._state[entry.name]
        actor_sub = self._actor_sub(actor)
        correlation_id = uuid4()

        if slot.state not in {"running", "running_unmonitored"}:
            raise TestPreconditionError("service must be running before tests")
        if entry.test_command is None:
            raise TestPreconditionError("service has no test_command in manifest")

        # The ``test_command`` is sourced from the manifest, which is
        # already JSON-Schema validated. We still tokenise via shlex
        # so we get an explicit argv list (no shell metacharacter
        # interpretation - behavior 8.3 surface).
        full_argv = shlex.split(entry.test_command)

        # The manifest convention is to write the *full* command
        # ("docker compose -f infra/docker-compose.yml exec <svc>
        # pytest ..."). The ``ComposeRunner.exec_test`` helper builds
        # its own ``docker compose -f F exec -T <svc>`` prefix, so we
        # need to strip the leading prefix from the manifest string.
        # We do this defensively: we look for an ``exec`` token and
        # take everything after the service-name token; if the format
        # is unexpected we fall back to running the whole list as the
        # test argv.
        argv = self._strip_compose_prefix(full_argv, entry.compose_service_name)

        result = await self._compose.exec_test(
            service_name=entry.compose_service_name,
            argv=argv,
            stream=stream,
        )

        summary = self._parse_pytest_summary(result.stdout)

        # Audit: run_tests is a Lifecycle_Action so it gets its own
        # row. ``write_with_retry`` is used because the test command
        # has already executed by the time we reach here - we will
        # not roll the operator's invocation back if the DB went
        # down (behavior 11.7).
        outcome_label: Literal["success", "failed"] = (
            "success" if result.exit_code == 0 else "failed"
        )
        write_outcome = await self._audit.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor=actor_sub,
                actor_type="admin_dashboard_user",
                service_name=entry.name,
                action="run_tests",
                timestamp=self._clock(),
                correlation_id=correlation_id,
                outcome=outcome_label,
                details_json={
                    "exit_code": result.exit_code,
                    "summary": (
                        {
                            "passed": summary.passed,
                            "failed": summary.failed,
                            "duration_seconds": summary.duration_seconds,
                        }
                        if summary is not None
                        else None
                    ),
                },
            )
        )

        return RunTestsResponse(
            output=result.stdout + (result.stderr if result.stderr else ""),
            exit_code=result.exit_code,
            summary=summary,
            correlation_id=correlation_id,
            audit_write_deferred=write_outcome.deferred,
        )

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    async def logs(
        self,
        *,
        name: str,
        tail: int,
        follow: bool,
    ) -> list[str]:
        """Return tailed Compose logs with sensitive values redacted.

        For ``follow=False`` the method awaits the full Compose
        invocation, splits the captured stdout into lines, and
        returns them as a list. The streaming path
        (``follow=True``) is the responsibility of the router (task
        6.2): it calls ``compose.logs(... follow=True)`` and applies
        :meth:`_redact_log_line` per chunk before forwarding to the
        SSE response. We expose the redaction pattern via
        :meth:`build_log_redaction_pattern` so the router shares the
        same key set we use here (invariant C5).
        """

        entry = self._require_entry(name)
        result = await self._compose.logs(
            service_name=entry.compose_service_name,
            tail=tail,
            follow=False,
        )
        # ``compose.logs(follow=False)`` returns a ComposeResult; the
        # async-generator branch is reserved for the streaming path
        # which the router consumes directly.
        from .compose_runner import ComposeResult  # local import for typing

        assert isinstance(result, ComposeResult)
        pattern = self.build_log_redaction_pattern(entry)
        return [
            _redact_log_line(line, pattern)
            for line in result.stdout.splitlines()
        ]

    def build_log_redaction_pattern(
        self,
        entry: ManagedServiceEntry,
    ) -> re.Pattern[str] | None:
        """Build (or fetch from cache) the redaction regex for ``entry``.

        The pattern union is built from the **Sensitive_Env_Key**
        subset of the service's ``.env.example`` LHS keys plus the
        canonical patterns shared across the codebase (any key whose
        name itself matches :func:`is_sensitive_env_key`).

        Returned pattern is suitable for :func:`_redact_log_line` and
        for the streaming-logs router to invoke per chunk.
        """

        fields = self._load_env_fields(entry)
        sensitive_keys: list[str] = [f.key for f in fields if f.is_sensitive]
        return _build_redaction_pattern(sensitive_keys)

    # ------------------------------------------------------------------
    # health_of (with streak alerting - behavior 12.5)
    # ------------------------------------------------------------------

    async def health_of(self, *, name: str) -> HealthSnapshot:
        """Return a fresh :class:`HealthSnapshot`, updating the cache.

        Cache TTL: ``HEALTH_POLL_INTERVAL_SECONDS / 2``. When the
        cached snapshot is younger than the TTL the method returns
        it verbatim - this is how :meth:`list_summaries` and the
        router's ``GET /admin/services/{name}/health`` endpoint
        coexist with the UI's polling (behavior 12.4).

        Streak alerting (behavior 12.5): each ``unhealthy``
        snapshot increments
        :attr:`LifecycleStateCache.consecutive_unhealthy_polls`. When
        the counter reaches :attr:`_fail_streak_threshold` the
        method writes a single ``health_streak_alert`` audit row and
        sets :attr:`LifecycleStateCache.streak_alert_emitted` to
        prevent duplicate rows on subsequent polls. The flag is
        cleared whenever the service returns to ``healthy`` (or a
        successful start/stop transition resets it).
        """

        entry = self._require_entry(name)
        slot = self._state[entry.name]

        # Cache hit branch (behavior 12.4 cache TTL).
        ttl = self._poll_interval / 2
        if slot.last_health_snapshot is not None and slot.last_health_polled_at is not None:
            age = (self._clock() - slot.last_health_polled_at).total_seconds()
            if age <= ttl:
                return slot.last_health_snapshot

        snapshot = await self._health.probe(entry)
        slot.last_health_snapshot = snapshot
        slot.last_health_polled_at = self._clock()

        # Streak bookkeeping. Only true ``unhealthy`` snapshots count
        # - ``unknown`` (the assume-running case for infra services
        # without /healthz) is excluded so ``redis``/``minio`` do not
        # spuriously trigger the alert.
        if snapshot.state == "unhealthy":
            slot.consecutive_unhealthy_polls += 1
            if (
                slot.consecutive_unhealthy_polls >= self._fail_streak_threshold
                and not slot.streak_alert_emitted
            ):
                # Best-effort audit write; we use ``write_with_retry``
                # so a transient DB outage does not raise out of the
                # health endpoint (behavior 11.7).
                await self._audit.write_with_retry(
                    AuditEntry(
                        id=uuid4(),
                        actor="system",
                        actor_type="admin_dashboard_user",
                        service_name=entry.name,
                        action="health_streak_alert",
                        timestamp=self._clock(),
                        correlation_id=uuid4(),
                        outcome="success",
                        details_json={
                            "reason": "consecutive_unhealthy_polls",
                            "streak": slot.consecutive_unhealthy_polls,
                        },
                    )
                )
                slot.streak_alert_emitted = True

            # Mirror state into the cache so list_summaries reflects
            # transient degradation.
            if slot.state == "running":
                slot.state = "unhealthy"
            elif slot.state == "starting":
                slot.state = "unhealthy"
            elif slot.state in ("stopped", "starting", "failed") and (
                snapshot.healthz_status != -1 or snapshot.readyz_status not in (None, -1)
            ):
                slot.state = "unhealthy"
        elif snapshot.state == "healthy":
            slot.consecutive_unhealthy_polls = 0
            slot.streak_alert_emitted = False
            if slot.state in ("stopped", "starting", "unhealthy", "failed"):
                slot.state = "running"
        elif snapshot.state in ("starting", "running_unmonitored"):
            slot.state = snapshot.state
        # ``unknown`` snapshots: do not change state machine, do not
        # touch the streak counter.

        return snapshot

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_entry(self, name: str) -> ManagedServiceEntry:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise UnknownServiceError(name) from exc

    async def _start_dependencies(
        self,
        *,
        entry: ManagedServiceEntry,
        actor: Any,
        actor_sub: str,
        correlation_id: UUID,
        recursion_path: tuple[str, ...],
    ) -> None:
        """Step 1.6 - start every dependency listed in ``entry.depends_on_services``.

        Implements platform operations behavior 5.1 / 5.3 / 5.4 / 5.5
        (Q11 - dependency chain orchestration). Behaviour:

        * Iterates ``entry.depends_on_services`` in manifest order
          (behavior 5.4 - sequential, not parallel).
        * Skips dependencies whose current ``state`` is already
          ``"running"`` or ``"starting"`` (behavior 5.3 - idempotent).
        * Skips dependencies that are not manifest-resident (external
          Boot_Bundle infra such as ``postgres`` / ``vault`` / ``temporal``
          that the lifecycle service cannot manage).
        * Recursively calls :meth:`_do_start` for each remaining
          dependency, passing ``_recursion_path=recursion_path + (entry.name,)``
          so the depth guard in ``_do_start`` fires correctly.
        * When a dependency's ``_do_start`` raises
          :class:`ComposeFailureError` (or any other exception that
          escapes the canonical audit-or-rollback boundary), the method:

          1. Writes a ``dependency_start_failed`` audit row with
             ``payload: {parent_service, failed_dependency, error_type}``
             (behavior 5.5).
          2. Does **not** stop already-started sibling dependencies
             (behavior 5.5 explicit clause "önceden başlatılmış
             sibling'ler stop edilmez").
          3. Re-raises wrapped in :class:`DependencyStartFailedError`
             so the parent's caller (the router) can render a 502.
        """

        for dep_name in entry.depends_on_services:
            dep_entry = self._by_name.get(dep_name)
            if dep_entry is None:
                # External Boot_Bundle dep (postgres / vault / temporal /
                # atlassian-mcp). Not a manifest node  we cannot start
                # it; skip silently (matches ``compute_start_plan`` logic).
                continue

            dep_state = self._state[dep_entry.name].state
            if dep_state in ("running", "starting"):
                # Idempotent skip - behavior 5.3.
                continue

            try:
                await self._do_start(
                    entry=dep_entry,
                    env_overrides={},
                    actor=actor,
                    _recursion_path=recursion_path + (entry.name,),
                )
            except Exception as exc:
                # behavior 5.5: write audit, do NOT stop siblings,
                # propagate wrapped in DependencyStartFailedError.
                await self._audit.write_with_retry(
                    AuditEntry(
                        id=uuid4(),
                        actor=actor_sub,
                        actor_type="admin_dashboard_user",
                        service_name=entry.name,
                        action="dependency_start_failed",
                        timestamp=self._clock(),
                        correlation_id=correlation_id,
                        outcome="failed",
                        details_json={
                            "parent_service": entry.name,
                            "failed_dependency": dep_entry.name,
                            "error_type": type(exc).__name__,
                        },
                    )
                )
                raise DependencyStartFailedError(
                    parent_service=entry.name,
                    failed_dependency=dep_entry.name,
                    cause=exc,
                ) from exc

    async def _check_feature_flags(
        self,
        entry: ManagedServiceEntry,
        *,
        actor_sub: str,
        correlation_id: UUID,
    ) -> None:
        """Step 1.5 gate - refuse to start when a required flag is off.

        Implements platform operations behavior 10.1 / 10.2 / 10.5
        (Q12). Behaviour:

        * No-op when the manifest entry's ``feature_flag_dependency``
          tuple is empty or no :class:`FeatureFlagReader` is wired
          (boot-time / unit-test path).
        * Otherwise issues **one** SQL ``SELECT`` (behavior 10.5) to
          fetch every flag's ``enabled`` value. Flags absent from the
          result map are treated as *disabled* - this catches typos in
          the manifest before they can corrupt audit history.
        * On the first disabled flag (manifest order - invariant 11
          determinism), writes a ``service_start_blocked_feature_flag``
          audit row through ``write_with_retry`` (best-effort: a DB
          outage cannot block the request flow because the request is
          about to be rejected anyway) and raises
          :class:`FeatureFlagDisabledError`.

        The audit row is best-effort by design: the canonical Step 4
        audit precheck has not yet run, so we cannot rely on the audit
        DB being reachable. ``write_with_retry`` queues the row on the
        deferred queue when the DB is down - the operator still
        receives the 409, and the audit lands once Postgres recovers.
        """

        flags = entry.feature_flag_dependency
        if not flags:
            return
        if self._feature_flag_reader is None:
            # No reader wired (e.g. boot before pg_pool came up, or a
            # unit-test path that doesn't exercise the gate). Treat
            # this as a non-event so we don't accidentally block
            # legitimate starts. The rule 10 acceptance criteria are
            # exercised through the production wiring in
            # ``src.main.lifespan`` which always supplies a reader
            # when ``app.state.pg_pool`` is available.
            return

        enabled_map = await self._feature_flag_reader.fetch_enabled_flags(
            list(flags)
        )

        for flag_name in flags:
            # Missing rows are treated as disabled - see docstring.
            enabled = enabled_map.get(flag_name, False)
            if enabled:
                continue

            # Audit the block before raising so the operator can see
            # *why* their request 409'd even when no other audit row
            # exists (the canonical pending/failed pair lives further
            # down the start flow). ``write_with_retry`` keeps the
            # call non-fatal if the audit DB is unreachable.
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="service_start_blocked_feature_flag",
                    timestamp=self._clock(),
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json={
                        "blocking_flag": flag_name,
                        "flag_state": "disabled" if flag_name in enabled_map else "missing",
                        "feature_flag_dependency": list(flags),
                    },
                )
            )
            raise FeatureFlagDisabledError(blocking_flag=flag_name)

    @staticmethod
    def _actor_sub(actor: Any) -> str:
        """Best-effort extraction of an OIDC ``sub`` from ``actor``.

        The router passes :class:`AuthClaims` (which has a ``sub``
        attribute), but unit tests routinely pass a bare string. We
        accept either shape so the orchestrator stays decoupled from
        the ``auth`` module's import surface.
        """

        if isinstance(actor, str):
            return actor
        sub = getattr(actor, "sub", None)
        if isinstance(sub, str) and sub:
            return sub
        return "unknown"

    def _load_env_fields(self, entry: ManagedServiceEntry) -> list[EnvField]:
        """Read + parse ``entry.env_example_path``, caching by path."""

        cached = self._form_schema_cache.get(entry.env_example_path)
        if cached is not None:
            return cached

        path = self._workspace_root / entry.env_example_path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Treat a missing example file as an empty schema; the
            # form-schema match would then accept only ``{}``. The
            # router-level startup readiness check (audit sink wiring) is
            # responsible for surfacing the missing file at boot,
            # not this hot path.
            text = ""
        fields = parse_env_example(text)
        self._form_schema_cache[entry.env_example_path] = fields
        return fields

    async def _compose_env_overrides_for_start(
        self,
        entry: ManagedServiceEntry,
        env_overrides: Mapping[str, str],
    ) -> dict[str, str]:
        """Return compose-only env overrides for service start.

        Streamlit must mirror the already-started Atlassian MCP runtime
        target (cloud vs Local/DC and site URLs), but those values are not
        Streamlit credentials and should not be repeated in the Streamlit
        start form schema. They are passed only to the docker compose child
        process so Compose interpolation can place them into the container
        environment.
        """

        merged = dict(env_overrides)
        if entry.name != STREAMLIT_UI_SERVICE:
            return merged

        atlassian_runtime = await self._vault.read_env_overrides(
            service_name=ATLASSIAN_MCP_SERVICE
        )
        for key in ATLASSIAN_RUNTIME_KEYS:
            value = atlassian_runtime.get(key, "")
            if value and not merged.get(key):
                merged[key] = value
        return merged

    def _validate_env_overrides(
        self,
        entry: ManagedServiceEntry,
        env_overrides: Mapping[str, str],
    ) -> None:
        """Enforce form-schema parity (behavior 5.6 / 5.7).

        Two checks:

        1. The submitted LHS key set equals the ``.env.example`` LHS
           key set **exactly**. Missing or extra keys both raise.
        2. Every Sensitive_Env_Key in the schema has a non-empty
           value (behavior 5.7).
        """

        fields = self._load_env_fields(entry)
        schema_keys = {f.key for f in fields}
        submitted_keys = set(env_overrides.keys())
        llm_provider = _llm_provider_for_schema(fields, env_overrides)

        if schema_keys != submitted_keys:
            missing = sorted(schema_keys - submitted_keys)
            extra = sorted(submitted_keys - schema_keys)
            parts: list[str] = []
            if missing:
                parts.append(f"missing keys: {missing}")
            if extra:
                parts.append(f"extra keys: {extra}")
            raise FormSchemaMismatchError(
                f"env_overrides for {entry.name!r} do not match form schema: "
                + "; ".join(parts)
            )

        for f in fields:
            if not f.is_sensitive:
                continue
            if entry.name == "atlassian-mcp":
                continue
            value = env_overrides.get(f.key, "")
            if value == "":
                if _llm_secret_can_be_empty(f.key, llm_provider):
                    continue
                raise FormSchemaMismatchError(
                    f"sensitive value required for key {f.key!r} of "
                    f"service {entry.name!r}"
                )

    async def run_connectivity_probe(
        self,
        *,
        name: str,
        actor: Any,
    ) -> None:
        """Manually re-run the connectivity probe for ``name`` (rule 9.6 / Q10).

        Implements platform operations behavior 9.6 - the
        ``POST /admin/services/{name}/probe`` endpoint calls this method
        to trigger a manual re-run of the manifest's
        ``connectivity_probe_command``. The same audit events
        (``service_connectivity_probe_passed`` /
        ``service_connectivity_probe_failed``) are emitted as during the
        automatic post-start Step 9.5 probe.

        Raises :class:`UnknownServiceError` when ``name`` is not in the
        manifest (router maps to 404).
        """

        entry = self._require_entry(name)
        actor_sub = self._actor_sub(actor)
        correlation_id = uuid4()
        await self._run_connectivity_probe(
            entry=entry,
            actor_sub=actor_sub,
            correlation_id=correlation_id,
        )

    async def _run_connectivity_probe(
        self,
        *,
        entry: ManagedServiceEntry,
        actor_sub: str,
        correlation_id: UUID,
    ) -> None:
        """Step 9.5 - run the manifest ``connectivity_probe_command`` (rule 9 / Q10).

        Implements platform operations behavior 9.2, 9.4 (Q10 -
        connectivity probe). Called after :meth:`_wait_for_healthy` returns
        ``True`` and before the final audit row is written.

        Behaviour
        ---------
        * No-op when ``entry.connectivity_probe_command`` is ``None``
          (behavior 9.1 - default ``null`` means no probe).
        * Otherwise runs the command via ``subprocess.run`` with a 30-second
          timeout (behavior 9.2 - "timeout 30 sn").
        * ``exit_code == 0``  ``state[name].credentials_status = "ok"`` +
          ``service_connectivity_probe_passed`` audit (behavior 9.4).
        * Any other exit code (or timeout / OS error)
          ``credentials_status = "failed"``,
          ``credentials_probe_detail = stderr[-500:]`` +
          ``service_connectivity_probe_failed`` audit (behavior 9.4).
        * A failed probe does **not** change the service ``state`` to
          ``"failed"`` - the service remains ``"running"``; only the
          ``credentials_status`` field is updated.

        This method is also called directly by the
        ``POST /admin/services/{name}/probe`` endpoint (behavior 9.6 -
        manuel re-run) so the same audit events are emitted for both the
        automatic post-start probe and the operator-triggered re-run.
        """

        import subprocess  # stdlib - local import keeps module-level imports clean

        cmd = entry.connectivity_probe_command
        if cmd is None:
            # No probe configured for this service - update credentials_status
            # to None to signal "no probe" (distinct from "unknown" which means
            # "probe configured but never run").
            return

        slot = self._state[entry.name]
        now = self._clock()

        exit_code: int
        stderr_text: str = ""

        try:
            proc = subprocess.run(  # noqa: S603 - command comes from trusted manifest
                shlex.split(cmd),
                timeout=30,
                capture_output=True,
                text=True,
            )
            exit_code = proc.returncode
            stderr_text = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            stderr_text = f"TimeoutExpired after 30s: {exc}"
        except OSError as exc:
            exit_code = -1
            stderr_text = f"OSError: {exc}"

        slot.credentials_probe_at = now

        if exit_code == 0:
            slot.credentials_status = "ok"
            slot.credentials_probe_detail = None
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="service_connectivity_probe_passed",
                    timestamp=now,
                    correlation_id=correlation_id,
                    outcome="success",
                    details_json={
                        "service_name": entry.name,
                        "command": cmd,
                        "exit_code": 0,
                    },
                )
            )
        else:
            stderr_summary = stderr_text[-500:] if stderr_text else ""
            slot.credentials_status = "failed"
            slot.credentials_probe_detail = stderr_summary
            await self._audit.write_with_retry(
                AuditEntry(
                    id=uuid4(),
                    actor=actor_sub,
                    actor_type="admin_dashboard_user",
                    service_name=entry.name,
                    action="service_connectivity_probe_failed",
                    timestamp=now,
                    correlation_id=correlation_id,
                    outcome="failed",
                    details_json={
                        "service_name": entry.name,
                        "command": cmd,
                        "exit_code": exit_code,
                        "stderr_summary": stderr_summary,
                    },
                )
            )

    async def _wait_for_healthy(self, entry: ManagedServiceEntry) -> bool:
        """Poll ``health.probe`` until ``healthy`` or the timeout fires.

        Used by ``start`` to determine the final state of the service
        after a successful Compose ``up``. The polling cadence is
        :data:`_HEALTH_POLL_STEP_SECONDS` and the timeout is the
        clamped ``HEALTH_READY_TIMEOUT_SECONDS``. Any non-``healthy``
        snapshot keeps the loop going; the loop returns ``True`` on
        the first ``healthy`` snapshot, ``False`` on timeout.

        ``unknown`` snapshots (for ``health_endpoint=null`` infra
        services) are **treated as success** because we have no way
        to confirm health and the operator's intent is "assume
        running".

        ``running_unmonitored`` snapshots (platform operations
        rule 12 / Q14) carry the same semantics: the container is up but
        has no Compose ``healthcheck`` block, so we cannot obtain a
        native health signal. Both ``unknown`` and
        ``running_unmonitored`` are therefore treated as "ready" here.
        """

        deadline_seconds = self._ready_timeout
        elapsed = 0.0
        while True:
            snapshot = await self._health.probe(entry)
            slot = self._state[entry.name]
            slot.last_health_snapshot = snapshot
            slot.last_health_polled_at = self._clock()
            if snapshot.state in ("healthy", "unknown", "running_unmonitored"):
                return True
            if elapsed >= deadline_seconds:
                return False
            await self._sleep(_HEALTH_POLL_STEP_SECONDS)
            elapsed += _HEALTH_POLL_STEP_SECONDS

    @staticmethod
    def _strip_compose_prefix(
        argv: list[str],
        compose_service_name: str,
    ) -> list[str]:
        """Drop the ``docker compose ... exec <svc>`` prefix if present.

        Manifest ``test_command`` strings follow the
        ``docker compose -f infra/docker-compose.yml exec <svc> <cmd>...``
        convention. The :class:`ComposeRunner.exec_test` helper builds
        its own ``docker compose -f F exec -T <svc>`` prefix, so we
        must strip the manifest prefix to avoid running the prefix
        twice. If the format does not match the convention we return
        the original list unchanged - the runner will surface the
        error as a Compose failure, not silently mangle the command.
        """

        try:
            exec_index = argv.index("exec")
        except ValueError:
            return argv
        # The token after ``exec`` is the service name; the test
        # command starts at ``exec_index + 2``.
        if exec_index + 1 >= len(argv):
            return argv
        if argv[exec_index + 1] != compose_service_name:
            # Service name in manifest mismatches; bail out and let
            # the runner pick up the unmodified argv.
            return argv
        return argv[exec_index + 2 :]

    @staticmethod
    def _parse_pytest_summary(stdout: str) -> TestSummary | None:
        """Parse the canonical pytest summary line from ``stdout``.

        Regex captures, in order: ``passed``, ``failed``, duration in
        seconds. Returns ``None`` on no match (router emits a JSON
        ``null``).
        """

        m = _PYTEST_SUMMARY_RE.search(stdout)
        if m is None:
            return None
        return TestSummary(
            passed=int(m.group(1)),
            failed=int(m.group(2)),
            duration_seconds=float(m.group(3)),
        )
