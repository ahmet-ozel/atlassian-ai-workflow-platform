"""``CapabilitiesRouter`` (`platform-gap-fill` task 9.1).

**Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5**

Capability probe matrix surface for the admin dashboard. The router
exposes two endpoints that let an admin inspect the live connectivity
between every department and every external service the platform
talks to (Jira, Bitbucket, Confluence, LLM, SSH, Docker):

* ``GET /api/v1/departments/capabilities`` — full ``dept × service``
  matrix served from the cache (Requirement 10.1).
* ``POST /api/v1/departments/{dept_id}/probe/{service}`` — re-run a
  single probe synchronously and return the fresh result
  (Requirement 10.3).

Design notes
------------

The router is intentionally agnostic of *how* probes are executed and
*where* their results live. Two Protocols sit between the FastAPI
endpoints and the production wiring:

* :class:`SupportsCapabilityProbeStore` — the cache backed by
  ``shared.capability_probes`` (Requirement 10.1). Task 9.3 ships
  the asyncpg-backed implementation; this module also provides
  :class:`InMemoryCapabilityProbeStore` so the router can be wired
  end-to-end while task 9.3 is still in flight (and so unit tests
  do not need a Postgres).
* :class:`SupportsCapabilityProber` — the actual probe runner that
  knows how to call ``/myself``, ``docker info``, etc. Production
  wires this against the foundation MCP / SSH / Vault clients;
  tests inject a stub that scripts each service's outcome.

Probe contract (Requirement 10.4)
---------------------------------

Service value → probe action:

* ``jira``        → ``GET {site}/rest/api/3/myself``
* ``bitbucket``   → ``GET {site}/2.0/user``
* ``confluence``  → ``GET {site}/wiki/rest/api/space``
* ``llm``         → minimal completion call against the dept's primary
  LLM provider
* ``ssh``         → SSH authentication attempt against the runner host
* ``docker``      → ``docker info`` over SSH on the runner host

Each probe returns a :class:`ProbeResult` with one of three statuses:

* ``"healthy"``        — probe succeeded.
* ``"unhealthy"``      — probe ran but failed (HTTP non-200, auth
  rejected, connection refused, timeout, etc.).
* ``"not_configured"`` — the department config does not declare the
  service (eg. dept has no ``bot.bitbucket`` section, or no
  ``llm_overrides`` block when ``service == "llm"``). This branch
  short-circuits the prober so we never call out to a non-existent
  endpoint (Requirement 10.5).

Status persistence (Requirement 10.1)
-------------------------------------

After every probe the router upserts the result into
``shared.capability_probes`` via :class:`SupportsCapabilityProbeStore`.
The matrix endpoint reads from the cache so the UI loads in <100ms
even when a slow upstream is misbehaving — a stale row is preferable
to a 30-second hang.

The ``GET`` endpoint does **not** trigger fresh probes; the UI's
auto-refresh + the explicit ``POST .../probe/{service}`` button are
the two write paths into the cache (Requirement 10.6).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Request, status
from pydantic import BaseModel, ConfigDict

from ..auth.dependencies import AuthClaims, require_admin

__all__ = [
    "router",
    "ProbeStatus",
    "ProbeResult",
    "ProbeRequest",
    "SupportsCapabilityProber",
    "SupportsCapabilityProbeStore",
    "InMemoryCapabilityProbeStore",
    "SUPPORTED_SERVICES",
    "ProbeResultModel",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Status values stored in ``shared.capability_probes.status`` and
#: returned to the UI. Mirrors the Postgres ``CHECK`` constraint
#: declared by migration ``008_capability_probes.sql``: ``ok`` /
#: ``error`` / ``not_configured``. The router exposes these to the FE
#: under the historical names ``healthy`` / ``unhealthy`` /
#: ``not_configured`` (Requirement 10.6) so the UI's CSS classes
#: continue to match — translation happens at the persistence boundary.
ProbeStatus = Literal["healthy", "unhealthy", "not_configured"]

#: The six services the matrix reports on (Requirement 10.4). Order
#: matches the UI column order so the matrix endpoint produces stable
#: JSON for snapshot tests and screenshot diffs.
SUPPORTED_SERVICES: tuple[str, ...] = (
    "jira",
    "bitbucket",
    "confluence",
    "llm",
    "ssh",
    "docker",
)

#: Translation from the in-memory ``ProbeStatus`` (FE-friendly) to the
#: ``shared.capability_probes.status`` enum (DB column). The mapping is
#: intentional — we surface ``healthy`` / ``unhealthy`` to the FE so
#: existing UI components keep working, but persist the canonical
#: ``ok`` / ``error`` values so the schema matches the migration.
_STATUS_TO_DB: Mapping[ProbeStatus, str] = {
    "healthy": "ok",
    "unhealthy": "error",
    "not_configured": "not_configured",
}
_STATUS_FROM_DB: Mapping[str, ProbeStatus] = {
    "ok": "healthy",
    "error": "unhealthy",
    "not_configured": "not_configured",
}


def _platform_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    for parent in Path(__file__).resolve().parents:
        if (parent / "config").is_dir():
            return parent
    return Path("/app")


#: Path to ``departments.json``. Mirrors the resolution used by
#: :mod:`src.routers.departments` so both routers agree on which file
#: to inspect when deriving "configured" state for a dept.
_DEPARTMENTS_CONFIG_PATH = _platform_root() / "config" / "departments.json"


# ---------------------------------------------------------------------------
# Public dataclasses (returned by the prober and the store)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Single ``(dept_id, service)`` probe outcome.

    Attributes mirror the columns of ``shared.capability_probes`` plus
    the ``service`` / ``dept_id`` keys the router needs to form the
    matrix.
    """

    dept_id: str
    service: str
    status: ProbeStatus
    error: str | None = None
    latency_ms: int | None = None
    probed_at: datetime | None = None

    def to_response(self) -> dict[str, Any]:
        """Serialise to the JSON shape returned by both endpoints."""

        return {
            "dept_id": self.dept_id,
            "service": self.service,
            "status": self.status,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "probed_at": (
                self.probed_at.isoformat() if self.probed_at else None
            ),
        }


# ---------------------------------------------------------------------------
# Pydantic response model (FastAPI uses this for ``response_model`` /
# OpenAPI; the dataclass above is the internal contract).
# ---------------------------------------------------------------------------


class ProbeResultModel(BaseModel):
    """Pydantic mirror of :class:`ProbeResult` for OpenAPI documentation."""

    model_config = ConfigDict(from_attributes=True)

    dept_id: str
    service: str
    status: ProbeStatus
    error: str | None = None
    latency_ms: int | None = None
    probed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Protocols (the router's only contracts with the production wiring)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsCapabilityProber(Protocol):
    """Run a single probe against one ``(dept_id, service)`` pair.

    Production wires this against:

    * a Jira / Bitbucket / Confluence MCP client (or raw httpx call to
      ``/myself`` / ``/user`` / ``/wiki/rest/api/space``),
    * an LLM completion adapter that issues a minimal ``ping`` prompt,
    * the foundation SSH runner (``paramiko`` / shared SSH client) for
      ``ssh`` and ``docker`` probes.

    The router never inspects the implementation — it just calls
    :meth:`probe` and persists the returned :class:`ProbeResult`.

    Implementations MUST honour the ``not_configured`` short-circuit:
    when the dept config does not declare the service, return
    ``ProbeResult(status="not_configured")`` without performing any
    network call (Requirement 10.5).
    """

    async def probe(self, *, dept_id: str, service: str) -> ProbeResult: ...


@runtime_checkable
class SupportsCapabilityProbeStore(Protocol):
    """Cache layer for :class:`ProbeResult` rows.

    Production wires this against the ``shared.capability_probes``
    table (task 9.3). Until that wiring lands the router ships an
    in-memory implementation so the FE can be developed end-to-end.

    The contract is intentionally minimal:

    * :meth:`upsert` — persist the latest result for a
      ``(dept_id, service)`` pair (overwriting any previous row).
    * :meth:`get_all` — return every row currently in the cache. The
      matrix endpoint uses this to render the full grid in one call.
    * :meth:`get_one` — return a single row, or ``None`` when the
      pair has never been probed. The single-probe endpoint uses
      this to surface the previous error in the UI when a probe
      stays unhealthy.
    """

    async def upsert(self, result: ProbeResult) -> None: ...

    async def get_all(self) -> list[ProbeResult]: ...

    async def get_one(
        self, *, dept_id: str, service: str
    ) -> ProbeResult | None: ...


# ---------------------------------------------------------------------------
# In-memory probe store (default until task 9.3's asyncpg adapter lands)
# ---------------------------------------------------------------------------


class InMemoryCapabilityProbeStore:
    """In-memory :class:`SupportsCapabilityProbeStore` implementation.

    The store keeps the most recent row per ``(dept_id, service)``
    pair in a plain dict. It is **not** a long-term substitute for the
    asyncpg-backed adapter (probe history is lost on process restart)
    — but it is sufficient for the matrix endpoint to round-trip data
    and for the unit tests in this package to verify routing /
    serialisation without standing up Postgres.

    Thread-safety: the dict is mutated only from inside the FastAPI
    request loop (cooperative async, single thread per worker), so we
    do not take a lock here. If a future caller drives the store from
    a background task they should wrap mutations in :class:`asyncio.Lock`
    themselves.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ProbeResult] = {}

    async def upsert(self, result: ProbeResult) -> None:
        self._rows[(result.dept_id, result.service)] = result

    async def get_all(self) -> list[ProbeResult]:
        # Return a stable order so the matrix endpoint produces
        # snapshot-friendly JSON. Sorting by (dept_id, service)
        # matches the UI's natural column order for a fixed dept
        # ordering.
        return [
            self._rows[k]
            for k in sorted(self._rows.keys())
        ]

    async def get_one(
        self, *, dept_id: str, service: str
    ) -> ProbeResult | None:
        return self._rows.get((dept_id, service))


# ---------------------------------------------------------------------------
# Department configuration helpers
# ---------------------------------------------------------------------------


def _load_departments() -> list[dict[str, Any]]:
    """Read ``config/departments.json``; return ``[]`` on any failure.

    Mirrors the helper in :mod:`src.routers.departments` so both
    routers agree on the source of truth. Soft-failure: a malformed
    or missing file logs a warning and returns an empty list, which
    the matrix endpoint serialises as ``{"departments": []}``.
    """

    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        depts = data.get("departments", [])
        return depts if isinstance(depts, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "capabilities router: failed to load departments.json: %s",
            exc,
        )
        return []


async def _assigned_runner_ids(request: Request, dept_id: str) -> list[str]:
    """Return runtime SSH runner assignments from Postgres when available."""

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            """
            SELECT runner_id
            FROM infrastructure.dept_ssh_assignments
            WHERE dept_id = $1
            ORDER BY priority ASC, assigned_at ASC
            """,
            dept_id,
        )
    except Exception as exc:  # noqa: BLE001 - probe UI must degrade
        logger.warning(
            "capabilities router: failed to load SSH runner assignments "
            "for %s: %s",
            dept_id,
            exc,
        )
        return []
    return [str(row["runner_id"]) for row in rows]


def _is_service_configured(dept: Mapping[str, Any], service: str) -> bool:
    """Return ``True`` when ``dept`` declares ``service`` in its config.

    The mapping mirrors Requirement 10.5: a probe row is
    ``not_configured`` when the dept config does not declare the
    service we are probing. Concretely:

    * ``jira`` / ``bitbucket`` / ``confluence`` — require a non-empty
      ``credential_ref`` under ``bot.{service}``.
    * ``llm`` — require an ``llm_overrides.primary`` block (a dept
      that inherits the global LLM has the ``llm_overrides`` key set
      to ``null`` / missing — there is no per-dept override to probe).
    * ``ssh`` / ``docker`` - require an explicit runner assignment or
      a legacy/env runner flag. Bitbucket credentials alone never
      imply execution capability.
    """

    bot = dept.get("bot") or {}

    if service in ("jira", "bitbucket", "confluence"):
        section = bot.get(service) or {}
        cred_ref = section.get("credential_ref")
        return bool(cred_ref)

    if service == "llm":
        overrides = dept.get("llm_overrides")
        if not isinstance(overrides, Mapping):
            return False
        primary = overrides.get("primary")
        return bool(primary)

    if service in ("ssh", "docker"):
        # Explicit runner assignment or legacy/env runner signal.
        ssh_section = dept.get("ssh") or {}
        runner_assigned = bool(
            dept.get("ssh_runner_id")
            or dept.get("ssh_runner_ids")
            or dept.get("ssh_runners")
            or dept.get("execution_runner_id")
            or ssh_section.get("runner_id")
            or ssh_section.get("host")
            or ssh_section.get("ssh_host_ref")
        )
        runner_env_available = any(
            os.environ.get(key, "").strip().lower()
            in {"1", "true", "yes", "on"}
            for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
        ) or bool(os.environ.get("SSH_HOST", "").strip())
        if runner_assigned or runner_env_available:
            return True
        # Bitbucket credentials alone do not make SSH/Docker configured.
        return False

    # Unknown service — treat as not configured rather than letting
    # the endpoint crash with an obscure error.
    return False


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class ProbeRequest(BaseModel):
    """Optional body for ``POST .../probe/{service}``.

    The endpoint takes the service name from the URL path so the body
    is empty by default. We declare it here so future fields (eg. a
    ``timeout_seconds`` override) can be added without breaking the
    surface; today the router accepts both an empty body and a body
    that omits every optional field.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Router setup
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/departments",
    tags=["capabilities"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_prober(request: Request) -> SupportsCapabilityProber:
    """Return the wired :class:`SupportsCapabilityProber`.

    Returns 503 with ``reason="prober_unavailable"`` when the slot is
    ``None`` — the same pattern :mod:`src.routers.workflow_control`
    uses for the Temporal client. The FE renders a clear "service
    not ready" badge instead of a stack trace.
    """

    prober = getattr(request.app.state, "capability_prober", None)
    if prober is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prober_unavailable",
            },
        )
    return prober


def _get_store(request: Request) -> SupportsCapabilityProbeStore:
    """Return the wired :class:`SupportsCapabilityProbeStore`.

    When the slot is ``None`` (lifespan still wiring up the asyncpg
    adapter) the helper auto-installs an :class:`InMemoryCapabilityProbeStore`
    so the matrix endpoint always has *some* cache to read from.
    The in-memory variant is process-local so a restart loses the
    history; this matches the documented behaviour of the in-memory
    fallback (Requirement 10.1 — "results are cached" — does not
    mandate durability across restarts when no DB is wired).
    """

    store = getattr(request.app.state, "capability_probe_store", None)
    if store is None:
        store = InMemoryCapabilityProbeStore()
        request.app.state.capability_probe_store = store
    return store


def _validate_service(service: str) -> str:
    """Reject service names outside :data:`SUPPORTED_SERVICES`.

    A 400 is friendlier than a 404 here — the path matched the route
    template, the value is just outside the documented enum.
    """

    if service not in SUPPORTED_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_service",
                "service": service,
                "supported": list(SUPPORTED_SERVICES),
            },
        )
    return service


def _find_department(dept_id: str) -> dict[str, Any]:
    """Return the dept config dict, or raise 404."""

    for dept in _load_departments():
        if dept.get("id") == dept_id:
            return dept
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"department '{dept_id}' not found",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/capabilities",
    summary="Full department × service capability matrix",
    dependencies=[Depends(require_admin)],
)
async def get_capability_matrix(request: Request) -> dict[str, Any]:
    """Return the cached capability matrix for every department.

    **Validates: Requirements 10.1, 10.5**

    Response shape::

        {
          "departments": [
            {
              "dept_id": "payment",
              "display_name": "Payment",
              "services": {
                "jira":       {"status": "healthy",        ...},
                "bitbucket":  {"status": "unhealthy",      ...},
                "confluence": {"status": "healthy",        ...},
                "llm":        {"status": "healthy",        ...},
                "ssh":        {"status": "not_configured", ...},
                "docker":     {"status": "not_configured", ...}
              }
            }
          ],
          "supported_services": ["jira", "bitbucket", "confluence",
                                  "llm", "ssh", "docker"]
        }

    The endpoint reads from the cache only — it does **not** trigger
    fresh probes. The UI's auto-refresh + the explicit "Yeniden Test
    Et" button (``POST .../probe/{service}``) are the two write paths
    into the cache.

    Cells without a cached row are reported as ``status="unknown"``
    so the FE can render a grey placeholder. Cells whose dept config
    does not declare the service are reported as
    ``status="not_configured"`` even when no row has been written yet
    — this matches Requirement 10.5 ("the cell SHALL be marked
    ``not_configured`` when the service is not declared in dept
    config") so an operator who has never run a probe still sees the
    right colour for a cell that *will* always be ``not_configured``.
    """

    store = _get_store(request)
    cached_rows = await store.get_all()
    cached_by_pair: dict[tuple[str, str], ProbeResult] = {
        (row.dept_id, row.service): row for row in cached_rows
    }

    departments = _load_departments()
    response_depts: list[dict[str, Any]] = []
    for dept in departments:
        dept_id = dept.get("id")
        if not isinstance(dept_id, str):
            # Defensive — schema validation rejects this in production
            # but we don't want a malformed config to 500 the matrix.
            continue
        runner_ids = await _assigned_runner_ids(request, dept_id)
        dept_with_runtime = {**dept, "ssh_runner_ids": runner_ids}

        services_block: dict[str, dict[str, Any]] = {}
        for service in SUPPORTED_SERVICES:
            cached = cached_by_pair.get((dept_id, service))
            if cached is not None:
                services_block[service] = cached.to_response()
                continue

            # No cached row → derive a placeholder. ``not_configured``
            # for unconfigured services, ``unknown`` otherwise so the
            # UI can render a grey placeholder until the first probe
            # writes a row.
            if not _is_service_configured(dept_with_runtime, service):
                placeholder = ProbeResult(
                    dept_id=dept_id,
                    service=service,
                    status="not_configured",
                    error=None,
                    latency_ms=None,
                    probed_at=None,
                )
                services_block[service] = placeholder.to_response()
            else:
                services_block[service] = {
                    "dept_id": dept_id,
                    "service": service,
                    "status": "unknown",
                    "error": None,
                    "latency_ms": None,
                    "probed_at": None,
                }

        response_depts.append(
            {
                "dept_id": dept_id,
                "display_name": dept.get("display_name"),
                "services": services_block,
            }
        )

    return {
        "departments": response_depts,
        "supported_services": list(SUPPORTED_SERVICES),
    }


@router.post(
    "/{dept_id}/probe/{service}",
    summary="Run a single capability probe (admin only)",
    dependencies=[Depends(require_admin)],
)
async def run_single_probe(
    request: Request,
    dept_id: str = PathParam(..., min_length=1, max_length=64),
    service: str = PathParam(..., min_length=1, max_length=32),
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Run one probe synchronously and return the fresh result.

    **Validates: Requirements 10.2, 10.3, 10.4, 10.5**

    Behaviour:

    1. Validate the service is one of :data:`SUPPORTED_SERVICES`
       (returns 400 otherwise).
    2. Verify the department exists (returns 404 otherwise).
    3. When the service is not declared in the dept config, write a
       ``not_configured`` row to the cache and return it without
       touching the prober (Requirement 10.5).
    4. Otherwise call the prober's :meth:`probe` method, persist the
       result via :meth:`SupportsCapabilityProbeStore.upsert`, and
       return the row.

    The endpoint is a write operation so it's gated by the admin
    role (mirrors :mod:`src.routers.workflow_control`).
    """

    _validate_service(service)
    dept = _find_department(dept_id)
    runner_ids = await _assigned_runner_ids(request, dept_id)
    dept_with_runtime = {**dept, "ssh_runner_ids": runner_ids}
    store = _get_store(request)

    # Requirement 10.5 short-circuit: never call out to a service the
    # dept does not declare. We persist the synthetic row so the
    # matrix endpoint and the single-probe endpoint stay consistent
    # — both will see the same ``not_configured`` value.
    if not _is_service_configured(dept_with_runtime, service):
        result = ProbeResult(
            dept_id=dept_id,
            service=service,
            status="not_configured",
            error=None,
            latency_ms=None,
            probed_at=datetime.now(tz=timezone.utc),
        )
        try:
            await store.upsert(result)
        except Exception as exc:  # noqa: BLE001 — cache write must not block
            logger.warning(
                "capability probe cache upsert failed for "
                "dept=%s service=%s: %s",
                dept_id,
                service,
                exc,
            )
        return result.to_response()

    prober = _get_prober(request)
    try:
        result = await prober.probe(dept_id=dept_id, service=service)
    except Exception as exc:  # noqa: BLE001 — translate into 502
        # The prober contract is to never raise — every probe outcome
        # should land on the ``ProbeResult`` channel. If a buggy
        # implementation raises, surface a 502 with a stable error
        # code so the FE renders a clear "upstream failure" badge
        # instead of a 500.
        logger.exception(
            "capability prober raised for dept=%s service=%s",
            dept_id,
            service,
        )
        # Persist the failure so the matrix endpoint surfaces it on
        # the next read. We deliberately downgrade ``"unhealthy"``
        # rather than ``"unknown"`` here — the prober *attempted* the
        # call, it just blew up before returning a structured result.
        result = ProbeResult(
            dept_id=dept_id,
            service=service,
            status="unhealthy",
            error=f"prober_exception: {exc.__class__.__name__}",
            latency_ms=None,
            probed_at=datetime.now(tz=timezone.utc),
        )
        try:
            await store.upsert(result)
        except Exception as inner:  # noqa: BLE001
            logger.warning(
                "capability probe cache upsert failed (after prober "
                "exception) for dept=%s service=%s: %s",
                dept_id,
                service,
                inner,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "prober_exception",
                "dept_id": dept_id,
                "service": service,
                "message": str(exc),
            },
        ) from exc

    # Stamp ``probed_at`` server-side if the prober didn't — gives the
    # FE a stable timestamp even when the underlying implementation
    # forgets to fill the field.
    if result.probed_at is None:
        result = ProbeResult(
            dept_id=result.dept_id,
            service=result.service,
            status=result.status,
            error=result.error,
            latency_ms=result.latency_ms,
            probed_at=datetime.now(tz=timezone.utc),
        )

    try:
        await store.upsert(result)
    except Exception as exc:  # noqa: BLE001 — cache write must not block
        logger.warning(
            "capability probe cache upsert failed for "
            "dept=%s service=%s: %s",
            dept_id,
            service,
            exc,
        )

    return result.to_response()
