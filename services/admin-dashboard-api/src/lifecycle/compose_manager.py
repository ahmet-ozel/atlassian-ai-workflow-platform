"""Compose Bootstrap Manager — profile-level Docker Compose orchestration.

The admin-dashboard-api drives the Setup Wizard by activating Compose
*profiles* for managed service groups on demand. This module is
the **profile-level** counterpart to :class:`ComposeRunner` (which is
service-level): callers ask for a profile, ComposeManager runs::

    docker compose -f infra/docker-compose.yml --profile {profile} up -d
    docker compose -f infra/docker-compose.yml --profile {profile} down

and then polls the per-service ``health_endpoint`` (when supplied) to
confirm the start actually came up. State is persisted to the existing
``automation.setup_wizard_state`` row family (migration 006) so a
platform restart can replay the operator's last activation set after a restart.

Why a separate module?
----------------------
* :class:`~src.lifecycle.compose_runner.ComposeRunner` issues
  per-service ``up`` / ``stop`` calls and is wired into the heavy
  :class:`LifecycleService` state machine. The Setup Wizard needs a
  *lighter* entry point that operates on Compose profiles directly
  and persists to a different table — overloading ``ComposeRunner``
  would blur the separation of concerns between profile-level and
  service-level orchestration.
* The subprocess-invocation patterns are deliberately mirrored
  (allow-listed env, ``shell=False``, argv lists) so audit/security
  reviews can confirm both modules use the same scrubbing rules. We do NOT
  depend on ``ComposeRunner``'s
  helpers here because the argv shape differs (``--profile`` is at
  the project level, not the service level).

Persistence model
-----------------
We reuse the existing ``automation.setup_wizard_state`` table created
in migration ``006_platform_completion_tables.sql``. Rows owned by
this module use the key prefix ``started_profile:`` so they coexist
with the Setup Wizard's step rows (``vault``, ``postgresql``, ...).

* ``step_name`` — ``f"started_profile:{profile}"``
* ``status``    — ``"running"`` after a successful ``start_service``;
                  the row is deleted on ``stop_service`` so the
                  ``list_started_profiles`` set is the canonical
                  source of truth for "what should auto-start on boot".
* ``config_data`` — JSON blob ``{"profile": ..., "started_at": ...,
                    "services": [...]}`` so the operator can audit
                    *what* came up under a given profile without
                    re-querying Compose.

Public surface
--------------
``ServiceStartResult``  Result of ``start_service``.
``ServiceStopResult``   Result of ``stop_service``.
``RunningService``      One row of ``docker compose ps --format json``.
``StartedProfileStore`` Persistence protocol.
``AsyncpgStartedProfileStore``  Production asyncpg-backed store.
``ComposeManagerError`` Raised on non-zero ``docker compose`` exit.
``InvalidProfileError`` Raised on malformed profile names.
``ComposeManager``      The orchestrator itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping, Protocol, Sequence

import httpx

# ---------------------------------------------------------------------------
# Subprocess hardening — mirrors compose_runner._ALLOWED_HOST_ENV_KEYS
# ---------------------------------------------------------------------------

#: Host env keys the spawned ``docker compose`` process is allowed to
#: inherit. Anything else (``VAULT_TOKEN``, ``OPENAI_API_KEY``, the
#: operator's shell history, …) stays on the API host. Mirrors the
#: scrubbing contract enforced by
#: :class:`~src.lifecycle.compose_runner.ComposeRunner._scrubbed_environ`
#: so both modules apply identical subprocess environment semantics.
_ALLOWED_HOST_ENV_KEYS: Final[frozenset[str]] = frozenset(
    {"PATH", "HOME", "DOCKER_HOST"}
)

#: Profile names must be operator-supplied tokens that are safe to
#: forward as a single ``--profile`` argv value. We restrict to the
#: shape Docker Compose itself enforces internally
#: (alphanumeric + ``-``, ``_``, ``.``) plus a length cap so the
#: subprocess argv stays bounded. The regex is anchored on both ends
#: to prevent any embedded whitespace, ``--``, or shell metacharacters
#: from sneaking in. ``shell=False`` already neutralises shell-side
#: expansion, but the validation also defends against a manifest
#: typo silently activating an unintended profile (e.g. an empty
#: string would expand to "all profiles").
_PROFILE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: How frequently :meth:`ComposeManager.check_health` re-issues the
#: health probe while waiting for a service to become healthy. Kept
#: short so unit tests with patched timeouts terminate quickly.
_HEALTH_POLL_INTERVAL_SECONDS: Final[float] = 1.0

#: Per-request timeout for the ``GET {health_endpoint}`` probe. The
#: outer ``timeout`` parameter on :meth:`check_health` controls the
#: *total* wait budget; this constant caps each individual request so
#: a hung upstream cannot starve the polling loop.
_HEALTH_REQUEST_TIMEOUT_SECONDS: Final[float] = 5.0

#: Maximum stdout size we read back from ``docker compose ps`` before
#: refusing to parse. ``ps --format json`` emits one JSON object per
#: container per line; even a 100-service deployment fits well inside
#: this cap. Acts as a defence-in-depth bound against a runaway
#: Compose plugin filling memory.
_PS_OUTPUT_BYTE_LIMIT: Final[int] = 1024 * 1024  # 1 MiB

#: Persistence row prefix — see module docstring "Persistence model".
_STARTED_PROFILE_PREFIX: Final[str] = "started_profile:"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceStartResult:
    """Outcome of a single :meth:`ComposeManager.start_service` call.

    Attributes
    ----------
    profile:
        The Compose profile that was activated.
    exit_code:
        The ``docker compose ... up`` return code. ``0`` on success.
        Non-zero values surface as :class:`ComposeManagerError`
        before this dataclass is built; the field is retained on the
        exception's :attr:`ComposeManagerError.exit_code` for the
        router error envelope.
    stdout:
        Captured stdout, decoded as UTF-8 with replacement.
    stderr:
        Captured stderr (same decoding).
    argv:
        The exact argv tuple that was executed. Stored verbatim so
        unit tests can assert the argv shape and so audit logging can
        include the resolved command line.
    """

    profile: str
    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ServiceStopResult:
    """Outcome of a single :meth:`ComposeManager.stop_service` call."""

    profile: str
    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RunningService:
    """One row of ``docker compose ps --format json`` output.

    Compose's JSON schema for ``ps`` exposes more fields than we
    surface here; we keep only the columns the Admin_Dashboard UI
    actually consumes: service name,
    lifecycle state, optional health rollup, image identifier). The
    ``raw`` field preserves the unparsed dict so callers that need a
    less-stable column (``Publishers``, ``Mounts``, …) can still
    pivot through it without re-parsing the JSON.
    """

    name: str
    state: str
    health: str | None
    image: str | None
    raw: Mapping[str, Any]


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ComposeManagerError(RuntimeError):
    """Raised when a ``docker compose`` invocation exits non-zero.

    The exception carries the captured exit code, stdout, stderr, and
    argv so the router can render the canonical 502 error envelope
    (mirrors :class:`~src.lifecycle.compose_runner.ComposeFailureError`).
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
        argv: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.argv = argv


class InvalidProfileError(ValueError):
    """Raised when a profile name fails the
    :data:`_PROFILE_PATTERN` validation.

    Routed to ``422 Unprocessable Entity`` upstream — operator
    supplied a malformed name, the request never reaches Compose.
    """


# ---------------------------------------------------------------------------
# Persistence — StartedProfileStore
# ---------------------------------------------------------------------------


class StartedProfileStore(Protocol):
    """Persistence interface for the auto-start manifest.

    Implementations MUST be safe to call concurrently from multiple
    coroutines (the Setup Wizard issues ``start_service`` /
    ``stop_service`` from request handlers that share the same
    process). The production
    :class:`AsyncpgStartedProfileStore` delegates concurrency to the
    underlying asyncpg pool; in-memory fakes used by unit tests are
    typically driven from a single coroutine and do not need
    locking.
    """

    async def record_started(  # pragma: no cover - structural protocol
        self,
        *,
        profile: str,
        services: Sequence[str],
        started_at: datetime,
    ) -> None: ...

    async def record_stopped(  # pragma: no cover - structural protocol
        self, *, profile: str
    ) -> None: ...

    async def list_started_profiles(  # pragma: no cover - structural protocol
        self,
    ) -> list[str]: ...


class AsyncpgStartedProfileStore:
    """Production :class:`StartedProfileStore` backed by asyncpg.

    Stores rows in ``automation.setup_wizard_state`` with
    ``step_name = f"started_profile:{profile}"``. The columns map
    cleanly:

    * ``status``       — ``"running"`` (the row's lifecycle marker;
                         deletion handles the inverse transition).
    * ``config_data``  — JSON ``{"profile", "services", "started_at"}``.
    * ``completed_at`` — wall-clock timestamp of the start, so audit
                         queries can answer "when did this profile
                         come up" without parsing JSON.
    * ``updated_at``   — refreshed by ``DEFAULT NOW()`` on every
                         upsert.

    The ``ON CONFLICT`` clause makes ``record_started`` idempotent
    (restarting an already-running profile
    should not orphan a stale row).
    """

    _UPSERT_SQL: Final[str] = (
        "INSERT INTO automation.setup_wizard_state "
        "(step_name, status, config_data, completed_at, updated_at) "
        "VALUES ($1, 'running', $2::jsonb, $3, NOW()) "
        "ON CONFLICT (step_name) DO UPDATE SET "
        "status = EXCLUDED.status, "
        "config_data = EXCLUDED.config_data, "
        "completed_at = EXCLUDED.completed_at, "
        "updated_at = NOW()"
    )

    _DELETE_SQL: Final[str] = (
        "DELETE FROM automation.setup_wizard_state WHERE step_name = $1"
    )

    _LIST_SQL: Final[str] = (
        "SELECT step_name FROM automation.setup_wizard_state "
        "WHERE step_name LIKE 'started_profile:%' "
        "AND status = 'running' "
        "ORDER BY step_name"
    )

    def __init__(self, *, pool: Any) -> None:
        self._pool = pool

    async def record_started(
        self,
        *,
        profile: str,
        services: Sequence[str],
        started_at: datetime,
    ) -> None:
        payload = json.dumps(
            {
                "profile": profile,
                "services": list(services),
                "started_at": started_at.isoformat(),
            }
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._UPSERT_SQL,
                f"{_STARTED_PROFILE_PREFIX}{profile}",
                payload,
                started_at,
            )

    async def record_stopped(self, *, profile: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._DELETE_SQL, f"{_STARTED_PROFILE_PREFIX}{profile}"
            )

    async def list_started_profiles(self) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_SQL)
        prefix_len = len(_STARTED_PROFILE_PREFIX)
        return [row["step_name"][prefix_len:] for row in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC time as a timezone-aware :class:`datetime`."""

    return datetime.now(timezone.utc)


def _decode(stream: bytes | None) -> str:
    """Decode subprocess output bytes with replacement for invalid UTF-8."""

    if stream is None:
        return ""
    return stream.decode("utf-8", errors="replace")


def _validate_profile(profile: str) -> str:
    """Return ``profile`` unchanged if valid, else raise.

    Empty strings, leading hyphens (which Compose would interpret as a
    flag), and any character outside :data:`_PROFILE_PATTERN`'s allow
    list are rejected up-front so the bad input never reaches the
    subprocess argv. ``shell=False`` already prevents shell-level
    injection, but rejecting here gives the operator a 422 with a
    readable error rather than an obscure ``docker compose`` failure.
    """

    if not isinstance(profile, str) or not _PROFILE_PATTERN.match(profile):
        raise InvalidProfileError(
            f"profile name must match {_PROFILE_PATTERN.pattern!r}; got {profile!r}"
        )
    return profile


def _scrubbed_env() -> dict[str, str]:
    """Return a minimal env dict for a child ``docker compose`` process.

    Only the keys in :data:`_ALLOWED_HOST_ENV_KEYS` are forwarded from
    :data:`os.environ`. This mirrors
    :func:`ComposeRunner._scrubbed_environ` so both modules give the
    same security surface during subprocess review.
    """

    env: dict[str, str] = {}
    for key in _ALLOWED_HOST_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


# ---------------------------------------------------------------------------
# ComposeManager
# ---------------------------------------------------------------------------


class ComposeManager:
    """Manages Docker Compose profiles for the Setup Wizard lifecycle.

    Parameters
    ----------
    compose_file:
        Absolute (or workspace-relative) path to the Compose file.
        The same value resolved by :class:`~src.config.Settings`
        (``infra/docker-compose.yml``).
    http_client:
        :class:`httpx.AsyncClient` used by :meth:`check_health` for
        the per-service health-endpoint poll. The manager does **not**
        own the client — callers manage its lifecycle (open at
        startup in ``main.lifespan``, close on shutdown).
    store:
        Optional :class:`StartedProfileStore` for persistence
        for auto-start after restart. When ``None``, ``start_service`` /
        ``stop_service`` skip the persistence write — useful in unit
        tests that don't exercise the auto-restart path.
    health_base_url_template:
        Format string used by :meth:`check_health` to build the URL
        it polls. Defaults to ``"http://{service}{endpoint}"`` so the
        manager assumes Compose's internal DNS resolves the service
        name on the project network. Tests can override to point at
        a captured ``httpx.MockTransport``.
    """

    def __init__(
        self,
        *,
        compose_file: Path,
        http_client: httpx.AsyncClient,
        store: StartedProfileStore | None = None,
        health_base_url_template: str = "http://{service}{endpoint}",
        clock: Any = None,
        sleep: Any = None,
    ) -> None:
        self._compose_file = compose_file
        self._http_client = http_client
        self._store = store
        self._health_base_url_template = health_base_url_template
        # Injectable clock + sleep for deterministic unit tests
        # (mirrors the convention in :class:`LifecycleService`).
        self._clock = clock or _utcnow
        self._sleep = sleep or asyncio.sleep

    # ------------------------------------------------------------------
    # Internal subprocess helpers
    # ------------------------------------------------------------------

    async def _run_compose(
        self, argv: Sequence[str]
    ) -> tuple[int, str, str, tuple[str, ...]]:
        """Spawn ``argv`` with ``shell=False`` and capture stdout/stderr.

        Returns a 4-tuple ``(exit_code, stdout, stderr, argv_tuple)``.
        Exceptions from :func:`asyncio.create_subprocess_exec` are
        translated into :class:`ComposeManagerError` so the caller
        gets a uniform failure shape regardless of whether Compose
        itself rejected the command or the spawn step never landed.
        """

        env = _scrubbed_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ComposeManagerError(
                "docker binary not found on PATH; install Docker or fix PATH",
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                argv=tuple(argv),
            ) from exc

        stdout_bytes, stderr_bytes = await proc.communicate()
        return (
            proc.returncode if proc.returncode is not None else -1,
            _decode(stdout_bytes),
            _decode(stderr_bytes),
            tuple(argv),
        )

    # ------------------------------------------------------------------
    # start_service
    # ------------------------------------------------------------------

    async def start_service(self, profile: str) -> ServiceStartResult:
        """Activate a Compose profile (``up -d``).

        Runs::

            docker compose -f {compose_file} --profile {profile} up -d

        On success, persists the activation in
        :class:`StartedProfileStore`. On non-zero
        exit, raises :class:`ComposeManagerError` and does **not**
        write the persistence row — the operator can retry without
        having to reconcile a stale "running" record.

        Parameters
        ----------
        profile:
            The Compose profile to activate. Must match
            :data:`_PROFILE_PATTERN`; invalid names raise
            :class:`InvalidProfileError`.

        Returns
        -------
        ServiceStartResult
            The result of the Compose invocation.

        Raises
        ------
        InvalidProfileError
            ``profile`` is empty / malformed.
        ComposeManagerError
            ``docker compose`` exited non-zero (or the binary is
            missing). The exception carries argv + stderr for the
            router 502 response.
        """

        profile = _validate_profile(profile)
        argv: list[str] = [
            "docker",
            "compose",
            "-f",
            str(self._compose_file),
            "--profile",
            profile,
            "up",
            "-d",
        ]
        exit_code, stdout, stderr, argv_tuple = await self._run_compose(argv)
        if exit_code != 0:
            raise ComposeManagerError(
                f"docker compose --profile {profile} up failed "
                f"(exit_code={exit_code})",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                argv=argv_tuple,
            )

        # Persist *after* a successful start so a failure leaves no
        # ghost row. We list the running services right now and
        # snapshot the names — operators reading the audit row can
        # see what came up under this profile without re-querying
        # Compose.
        services_under_profile: list[str] = []
        if self._store is not None:
            try:
                running = await self.get_running_services()
                services_under_profile = [svc.name for svc in running]
            except ComposeManagerError:
                # Persistence still proceeds with an empty service list
                # — the row's primary purpose is auto-restart, and
                # losing the service-name audit detail is preferable
                # to dropping the auto-restart entry entirely.
                services_under_profile = []
            await self._store.record_started(
                profile=profile,
                services=services_under_profile,
                started_at=self._clock(),
            )

        return ServiceStartResult(
            profile=profile,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            argv=argv_tuple,
        )

    # ------------------------------------------------------------------
    # stop_service
    # ------------------------------------------------------------------

    async def stop_service(self, profile: str) -> ServiceStopResult:
        """Deactivate a Compose profile (``down``).

        Runs::

            docker compose -f {compose_file} --profile {profile} down

        On success, removes the persistence row so the profile is no
        longer auto-restarted on boot. On non-zero exit, raises
        :class:`ComposeManagerError` and leaves the persistence row
        intact — a partial stop should not silently un-register the
        profile.
        """

        profile = _validate_profile(profile)
        argv: list[str] = [
            "docker",
            "compose",
            "-f",
            str(self._compose_file),
            "--profile",
            profile,
            "down",
        ]
        exit_code, stdout, stderr, argv_tuple = await self._run_compose(argv)
        if exit_code != 0:
            raise ComposeManagerError(
                f"docker compose --profile {profile} down failed "
                f"(exit_code={exit_code})",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                argv=argv_tuple,
            )

        if self._store is not None:
            await self._store.record_stopped(profile=profile)

        return ServiceStopResult(
            profile=profile,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            argv=argv_tuple,
        )

    # ------------------------------------------------------------------
    # check_health
    # ------------------------------------------------------------------

    async def check_health(
        self,
        service: str,
        endpoint: str,
        timeout: int = 30,
    ) -> bool:
        """Poll ``GET http://{service}{endpoint}`` until 200 or timeout.

        After activating a profile, the Setup Wizard verifies each service
        comes up by polling its
        ``health_endpoint`` (typically ``/healthz``) on the Compose
        internal network. Returns ``True`` when the endpoint returns
        ``200`` within ``timeout`` seconds, ``False`` otherwise.

        Parameters
        ----------
        service:
            Compose service name. Used as the hostname in the URL —
            Docker's internal DNS resolves it within the project
            network.
        endpoint:
            URL path beginning with ``/`` (e.g. ``"/healthz"``).
        timeout:
            Total polling budget in seconds. Each individual request
            is capped at :data:`_HEALTH_REQUEST_TIMEOUT_SECONDS`; the
            outer loop exits as soon as the first 200 lands or the
            budget elapses.

        Returns
        -------
        bool
            ``True`` if a 200 response landed within the budget,
            ``False`` if the budget elapsed without a 200.
        """

        if not isinstance(service, str) or not service:
            raise ValueError(f"service must be a non-empty str, got {service!r}")
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            raise ValueError(
                f"endpoint must start with '/', got {endpoint!r}"
            )
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout!r}")

        url = self._health_base_url_template.format(
            service=service, endpoint=endpoint
        )
        request_timeout = httpx.Timeout(_HEALTH_REQUEST_TIMEOUT_SECONDS)

        deadline = self._clock().timestamp() + float(timeout)
        while True:
            try:
                response = await self._http_client.get(
                    url, timeout=request_timeout
                )
            except httpx.HTTPError:
                # Connection refused / DNS / timeout — fall through to
                # the next polling iteration. The service may still be
                # warming up.
                response = None

            if response is not None and response.status_code == 200:
                return True

            now = self._clock().timestamp()
            if now >= deadline:
                return False

            # Sleep between polls — clamped so we never sleep past
            # the deadline (the next ``while True`` head check
            # handles edge cases).
            sleep_for = min(_HEALTH_POLL_INTERVAL_SECONDS, deadline - now)
            if sleep_for > 0:
                await self._sleep(sleep_for)

    # ------------------------------------------------------------------
    # get_running_services
    # ------------------------------------------------------------------

    async def get_running_services(self) -> list[RunningService]:
        """Return the list of currently running Compose services.

        Runs::

            docker compose -f {compose_file} ps --format json

        and parses the output. Compose's ``--format json`` output is
        version-dependent: newer versions emit a single JSON array,
        older versions emit one JSON object per line (NDJSON). This
        method tolerates both shapes.

        Returns
        -------
        list[RunningService]
            One row per container reported by Compose. The list is
            empty when no services are running (the command exits
            ``0`` with empty stdout).

        Raises
        ------
        ComposeManagerError
            ``docker compose`` exited non-zero, the binary is
            missing, or the output exceeded
            :data:`_PS_OUTPUT_BYTE_LIMIT`.
        """

        argv: list[str] = [
            "docker",
            "compose",
            "-f",
            str(self._compose_file),
            "ps",
            "--format",
            "json",
        ]
        exit_code, stdout, stderr, argv_tuple = await self._run_compose(argv)
        if exit_code != 0:
            raise ComposeManagerError(
                f"docker compose ps failed (exit_code={exit_code})",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                argv=argv_tuple,
            )

        if len(stdout.encode("utf-8", errors="replace")) > _PS_OUTPUT_BYTE_LIMIT:
            raise ComposeManagerError(
                f"docker compose ps output exceeded "
                f"{_PS_OUTPUT_BYTE_LIMIT} bytes; refusing to parse",
                exit_code=exit_code,
                stdout="<truncated>",
                stderr=stderr,
                argv=argv_tuple,
            )

        return _parse_compose_ps_output(stdout)

    # ------------------------------------------------------------------
    # auto_start_persisted
    # ------------------------------------------------------------------

    async def auto_start_persisted(self) -> list[str]:
        """Re-activate every profile recorded as running.

        Called from ``main.lifespan`` on startup so a platform restart
        re-establishes the operator's previously activated profile
        set without manual intervention. When the
        manager has no :class:`StartedProfileStore` attached the
        method returns an empty list; otherwise it walks the persisted
        profiles in deterministic order and calls
        :meth:`start_service` on each one.

        Failures
        --------
        Each :meth:`start_service` call may raise
        :class:`ComposeManagerError`. We catch the failure, log it
        via the standard logging hook (the caller wires
        ``logging.getLogger`` upstream), and continue with the next
        profile so a single broken profile does not prevent the rest
        of the platform from coming back up. The return value is the
        list of profiles that **successfully** restarted.

        Returns
        -------
        list[str]
            The profile names whose :meth:`start_service` call
            succeeded. May be a strict subset of the persisted set
            when one or more profiles failed to restart.
        """

        if self._store is None:
            return []

        profiles = await self._store.list_started_profiles()
        restarted: list[str] = []
        for profile in profiles:
            try:
                await self.start_service(profile)
            except (InvalidProfileError, ComposeManagerError):
                # Best-effort restart: log via the module logger so
                # operators see the failure on stdout/Loki, then
                # continue. The persistence row is intentionally
                # left in place so a follow-up retry (operator
                # clicks "Start" in the wizard) will pick it up.
                _logger().warning(
                    "auto_start_persisted: failed to restart profile %r; "
                    "leaving persistence row in place for operator retry",
                    profile,
                    exc_info=True,
                )
                continue
            restarted.append(profile)
        return restarted


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def _logger() -> Any:
    """Return the module-level logger.

    Wrapped in a function so unit tests can monkey-patch the logger
    without rebinding a module global, mirroring the convention used
    by :mod:`src.lifecycle.service`.
    """

    import logging

    return logging.getLogger(__name__)


def _parse_compose_ps_output(stdout: str) -> list[RunningService]:
    """Parse the output of ``docker compose ps --format json``.

    Compose has shipped two JSON layouts over time:

    * **Array layout** (Compose v2.21+): a single JSON array of
      container objects, optionally pretty-printed.
    * **NDJSON layout** (Compose v2.0 – v2.20): one JSON object per
      line, no surrounding array.

    We try the array layout first (``json.loads`` on the whole
    string); on failure we fall back to NDJSON parsing. Either way
    the result is a list of :class:`RunningService` rows.

    Compose's per-row schema uses different key spellings across
    versions:

    * ``Name`` (modern) / ``Service`` (older) for the service name.
    * ``State`` for the lifecycle state (``running`` / ``exited`` /
      ``created`` / …).
    * ``Health`` (top-level on modern Compose) for the rollup health
      column. Older versions nest it under
      ``Publishers`` → ``Health``; we accept both.
    * ``Image`` for the image identifier.

    Missing fields are reported as ``None`` rather than raising;
    Compose's schema is documented as best-effort and we don't want
    a single-row anomaly to fail the whole listing.
    """

    stdout = stdout.strip()
    if not stdout:
        return []

    rows: list[Any]
    try:
        # Array layout — a single JSON value spanning the whole stdout.
        decoded = json.loads(stdout)
        if isinstance(decoded, list):
            rows = decoded
        elif isinstance(decoded, dict):
            # Single-row deployment can come back as a bare object.
            rows = [decoded]
        else:
            rows = []
    except json.JSONDecodeError:
        # NDJSON fallback — one JSON object per non-empty line.
        rows = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip malformed lines rather than fail the whole call.
                continue

    parsed: list[RunningService] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("Name") or row.get("Service") or ""
        state = row.get("State") or ""
        # Modern Compose puts the rollup health at the top level.
        health = row.get("Health")
        if not health:
            # Older Compose versions emit ``Publishers`` as a list
            # and never expose a top-level Health column. Surface
            # ``None`` rather than guessing.
            health = None
        parsed.append(
            RunningService(
                name=str(name),
                state=str(state),
                health=str(health) if health else None,
                image=str(row.get("Image")) if row.get("Image") else None,
                raw=row,
            )
        )
    return parsed


__all__ = (
    "AsyncpgStartedProfileStore",
    "ComposeManager",
    "ComposeManagerError",
    "InvalidProfileError",
    "RunningService",
    "ServiceStartResult",
    "ServiceStopResult",
    "StartedProfileStore",
)
