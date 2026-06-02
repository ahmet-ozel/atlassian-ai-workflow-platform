"""REST router backing ``/admin/services`` (task 6.2, design §3.3).

This module is the HTTP boundary for every Lifecycle_Action exposed by
the admin-dashboard-api. The router is intentionally *thin*: it adapts
:class:`~src.lifecycle.service.LifecycleService` results into Pydantic
v2 models, maps the orchestrator's exception hierarchy onto the
canonical HTTP status codes, and wires the streaming-logs / SSE-test
paths through :class:`fastapi.responses.StreamingResponse`.

Endpoints (8 total, every one is gated on
``Depends(require_admin)`` per Requirement 10.1):

* ``GET    /admin/services``                — Requirement 6.1.
* ``GET    /admin/services/{name}``         — Requirement 6.2.
* ``POST   /admin/services/{name}/start``   — Requirement 5.5, 6.3.
* ``POST   /admin/services/{name}/stop``    — Requirement 6.4, 6.5.
* ``POST   /admin/services/{name}/restart`` — Requirement 6.6.
* ``POST   /admin/services/{name}/test``    — Requirement 8.1/8.2/8.4/8.5/8.6.
* ``GET    /admin/services/{name}/logs``    — Requirement 7.1/7.2/7.3/7.7.
* ``GET    /admin/services/{name}/health``  — Requirement 7.4/7.5/7.6.

Error mapping (design §3.3):

* :class:`UnknownServiceError`        → ``404 Not Found``.
* :class:`FormSchemaMismatchError`    → ``422 Unprocessable Entity``.
* :class:`TestPreconditionError`      → ``409 Conflict``.
* :class:`FeatureFlagDisabledError`   → ``409 Conflict`` (R10 / Q12).
* :class:`VaultWriteError`            → ``502 Bad Gateway`` + ``correlation_id``.
* :class:`AuditUnreachableError`      → ``502 Bad Gateway`` + ``correlation_id``.
* :class:`ComposeFailureError`        → ``502 Bad Gateway`` + ``correlation_id``.

The 502 envelopes carry a ``correlation_id`` UUID so the operator can
pivot between the HTTP response, the audit log row, and the
structured server logs (Requirement 6.7, 11.8).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.dependencies import AuthClaims, require_admin
from ..config import Settings
from ..lifecycle.audit_writer import AuditUnreachableError
from ..lifecycle.compose_runner import ComposeFailureError
from ..lifecycle.service import (
    DependencyStartFailedError,
    FeatureFlagDisabledError,
    FormSchemaMismatchError,
    LifecycleService,
    MaxDependencyDepthExceededError,
    TestPreconditionError,
    UnknownServiceError,
)
from ..lifecycle.vault_client import VaultWriteError
from ._models import (
    ErrorEnvelope,
    FormSchema,
    FormSchemaField,
    HealthSnapshotModel,
    LogsResponse,
    ProbeResponse,
    ServiceDetail,
    ServiceSummary,
    StartPlanResponse,
    StartRequest,
    StartResponse,
    StopRequest,
    StopResponse,
    TestResponse,
    TestSummaryModel,
)


# ---------------------------------------------------------------------------
# Router + DI
# ---------------------------------------------------------------------------


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/services",
    tags=["services-lifecycle"],
    dependencies=[Depends(require_admin)],
)


def get_lifecycle_service(request: Request) -> LifecycleService:
    """Resolve the per-process :class:`LifecycleService` singleton.

    The application startup hook (task 6.3, ``src/main.py``) is
    responsible for constructing the service and binding it to
    ``app.state.lifecycle``. We pull it from there at request time so
    unit tests can override the dependency via
    ``app.dependency_overrides[get_lifecycle_service] = ...`` without
    monkey-patching module globals.

    Raises ``503`` (the same shape ``/readyz`` returns when the manifest
    is invalid) when the singleton is missing — this happens during the
    short window between process start and lifespan completion, and on
    every request after a manifest-load failure.
    """

    svc: LifecycleService | None = getattr(request.app.state, "lifecycle", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "manifest_invalid"},
        )
    return svc


def get_settings_dependency() -> Settings:
    """Resolve the per-process :class:`Settings` singleton.

    Reads :class:`Settings` on every call so ``pydantic-settings`` can
    pick up environment changes during long-running test sessions.
    Production deployments mount the env file at boot so the value is
    effectively constant. Unit tests override the dependency via
    ``app.dependency_overrides[get_settings_dependency] = ...`` so they
    can drive the ``deployment_profile`` field without touching
    process environment variables.

    The dedicated dependency function (rather than constructing
    :class:`Settings` inline inside each endpoint) keeps the router's
    test surface symmetric with :func:`get_lifecycle_service` and
    avoids the cross-test contamination that a module-level singleton
    would produce.
    """

    return Settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gateway_failure_response(
    *,
    detail: str,
    correlation_id: UUID,
) -> JSONResponse:
    """Build the ``502 Bad Gateway`` envelope (Requirement 6.7, 11.8).

    Implemented as a plain :class:`JSONResponse` rather than an
    :class:`HTTPException` so we can guarantee the ``correlation_id``
    field always lands at the top of the body alongside ``detail``.
    """

    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorEnvelope(
            detail=detail,
            correlation_id=correlation_id,
        ).model_dump(mode="json"),
    )


def _detail_from_entry(
    svc: LifecycleService,
    name: str,
) -> ServiceDetail:
    """Assemble the ``GET /admin/services/{name}`` body.

    The router fans out across:

    * :meth:`LifecycleService.get_manifest_entry` → manifest fields.
    * :meth:`LifecycleService.state_cache` → ``state``,
      ``last_started_at``, ``last_health_snapshot`` (cached snapshot).
    * :meth:`LifecycleService.get_form_schema` → ``form_schema.fields``.

    We deliberately surface the *cached* snapshot rather than firing a
    fresh probe — the dedicated ``GET /admin/services/{name}/health``
    endpoint exists for that purpose (Requirement 7.4).
    """

    entry = svc.get_manifest_entry(name)
    slot = svc.state_cache[name]
    schema_rows = [
        FormSchemaField.model_validate(field, from_attributes=True)
        for field in svc.get_form_schema(name)
    ]

    snapshot_model: HealthSnapshotModel | None = None
    if slot.last_health_snapshot is not None:
        snapshot_model = HealthSnapshotModel.model_validate(
            slot.last_health_snapshot, from_attributes=True
        )

    return ServiceDetail(
        name=entry.name,
        kind=entry.kind,
        compose_service_name=entry.compose_service_name,
        compose_profile=entry.compose_profile,
        env_example_path=entry.env_example_path,
        health_endpoint=entry.health_endpoint,
        test_command=entry.test_command,
        state=slot.state,
        last_started_at=slot.last_started_at,
        last_health_snapshot=snapshot_model,
        form_schema=FormSchema(fields=schema_rows),
        # Connectivity probe fields (R9.5 / Q10) — surfaced so the UI's
        # service detail page can render the credentials banner without
        # an extra round-trip. ``None`` when no probe is configured.
        credentials_status=slot.credentials_status,
        credentials_probe_at=slot.credentials_probe_at,
        credentials_probe_detail=slot.credentials_probe_detail,
    )


async def _refresh_manifest_health_cache(svc: LifecycleService) -> None:
    manifest = getattr(svc, "manifest", ())
    if not manifest:
        return
    results = await asyncio.gather(
        *(svc.health_of(name=entry.name) for entry in manifest),
        return_exceptions=True,
    )
    for entry, result in zip(manifest, results):
        if isinstance(result, Exception):
            logger.debug(
                "background health refresh failed for %s: %s",
                entry.name,
                result,
            )


def _schedule_health_refresh(svc: LifecycleService) -> None:
    task = asyncio.create_task(_refresh_manifest_health_cache(svc))

    def _consume_result(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except Exception as exc:  # noqa: BLE001
            logger.debug("background health refresh crashed: %s", exc)

    task.add_done_callback(_consume_result)


# ---------------------------------------------------------------------------
# GET /admin/services
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ServiceSummary],
    summary="List Managed_Service summaries",
)
async def list_services(
    refresh: bool = Query(
        default=False,
        description="When true, wait for a fresh health refresh before listing.",
    ),
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> list[ServiceSummary]:
    """Return one row per Managed_Service (Requirement 6.1)."""

    manifest = getattr(svc, "manifest", ())
    if manifest and refresh:
        await _refresh_manifest_health_cache(svc)
    elif manifest:
        _schedule_health_refresh(svc)
    summaries = await svc.list_summaries()
    return [
        ServiceSummary.model_validate(s, from_attributes=True)
        for s in summaries
    ]


# ---------------------------------------------------------------------------
# GET /admin/services/{name}
# ---------------------------------------------------------------------------


@router.get(
    "/{name}",
    response_model=ServiceDetail,
    summary="Manifest entry + cached health + form_schema",
)
async def get_service_detail(
    name: str,
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> ServiceDetail:
    """Return the full detail body for one Managed_Service (Requirement 6.2)."""

    try:
        return _detail_from_entry(svc, name)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/start-plan  (R5.6 / Q11 — preview)
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/start-plan",
    response_model=StartPlanResponse,
    summary="Preview the dependency-chain plan for start(name) (R5.6 / Q11)",
)
async def get_service_start_plan(
    name: str,
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> StartPlanResponse:
    """Return the topologically-sorted dependency-chain plan.

    Implements platform-mimari-uyumluluk Requirement 5.6 (Q11). The
    admin-dashboard-ui calls this endpoint when the operator clicks
    *Start* on a service so it can render a confirmation modal of the
    form "Aşağıdaki servisler de başlatılacak: {will_start}".

    Behaviour:

    * Reads the manifest entry for ``name`` (404 on miss).
    * Walks ``depends_on_services`` depth-first in post-order so
      dependencies precede dependents in ``will_start`` (mirrors the
      actual Step 1.6 descent — Requirement 5.4).
    * Filters out external Boot_Bundle deps (e.g. ``postgres`` /
      ``vault``) that are not manifest-resident — the lifecycle
      service cannot start them so they have no place in the plan.
    * Partitions visited services into ``already_running`` (current
      ``state="running"`` — idempotent skip per Requirement 5.3) and
      ``will_start``.
    * Read-only: writes no audit rows, performs no I/O outside the
      in-process state cache. Safe to poll from the UI.
    """

    try:
        plan = svc.compute_start_plan(name)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return StartPlanResponse(
        target_service=plan.target_service,
        will_start=list(plan.will_start),
        already_running=list(plan.already_running),
    )


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/start
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartResponse,
    summary="Start a Managed_Service",
)
async def start_service(
    name: str,
    body: StartRequest,
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Bring a service up — Requirement 5.5, 6.3.

    Error mapping mirrors design §3.3:

    * ``UnknownServiceError``              → 404
    * ``FormSchemaMismatchError``          → 422
    * ``VaultWriteError``                  → 502 + ``correlation_id``
    * ``AuditUnreachableError``            → 502 + ``correlation_id``
    * ``ComposeFailureError``              → 502 + ``correlation_id``
    * ``MaxDependencyDepthExceededError``  → 502 + ``correlation_id`` (R5.2 / Q11)
    * ``DependencyStartFailedError``       → 502 + ``correlation_id`` (R5.5 / Q11)
    """

    try:
        result = await svc.start(
            name=name,
            env_overrides=body.env_overrides,
            actor=actor,
        )
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FormSchemaMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except FeatureFlagDisabledError as exc:
        # platform-mimari-uyumluluk R10 / Q12 — feature-flag start gate.
        # The orchestrator's Step 1.5 raised because at least one flag
        # in the manifest's ``feature_flag_dependency`` is disabled.
        # We surface 409 with a structured envelope so the UI can
        # render a targeted "open Feature Flags page → toggle X" modal
        # (Requirement 10.3).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "feature_flag_disabled",
                "blocking_flag": exc.blocking_flag,
                "detail": str(exc),
            },
        ) from exc
    except (
        VaultWriteError,
        AuditUnreachableError,
        ComposeFailureError,
        MaxDependencyDepthExceededError,
        DependencyStartFailedError,
    ) as exc:
        # platform-mimari-uyumluluk R5.2 / R5.5 (Q11): dependency-chain
        # failures (depth exceeded or a dep failed to start) surface as
        # 502 alongside the canonical Vault / Audit / Compose failures.
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    return StartResponse.model_validate(result, from_attributes=True)


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/stop
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/stop",
    response_model=StopResponse,
    summary="Stop a Managed_Service (idempotent — Requirement 6.5)",
)
async def stop_service(
    name: str,
    body: StopRequest | None = None,
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
    settings: Settings = Depends(get_settings_dependency),
):
    """Bring a service down (Requirement 6.4, 6.5).

    The body is optional — a missing body is equivalent to
    ``{"remove_volumes": false, "purge_vault": false}``. The
    orchestrator handles the idempotent path internally and returns
    ``noop=True`` when the service was already stopped.

    platform-mimari-uyumluluk Requirement 14.2 (Q16) — the optional
    ``purge_vault`` flag instructs the orchestrator to delete every
    Vault override under ``secret/services/{name}/`` after the Compose
    ``stop`` step completes (the actual purge wiring lands in task
    15.2). When the flag is ``true`` and ``settings.deployment_profile``
    resolves (case-insensitively) to ``"production"`` the router
    short-circuits with ``403 Forbidden`` and writes a
    ``purge_vault_blocked_in_production`` audit row before any Compose
    side-effect is triggered. Mirrors the existing
    ``test_stop_lifecycle_purge_guard.py`` semantics.
    """

    remove_volumes = bool(body.remove_volumes) if body is not None else False
    purge_vault = bool(body.purge_vault) if body is not None else False

    # platform-mimari-uyumluluk R14.2 (Q16) — production guard.
    # We refuse the destructive flag *before* invoking Compose so a
    # single rogue request cannot tear down Vault overrides on a live
    # cluster. The check is case-insensitive because operators
    # commonly normalise the env var via shell exports
    # (``DEPLOYMENT_PROFILE=Production``); matching the production
    # profile in any case-folding form keeps the guard tight.
    if purge_vault and settings.deployment_profile.lower() == "production":
        # Resolve the manifest entry first so unknown service names
        # surface a 404 (consistent with every other endpoint that
        # operates on ``/admin/services/{name}``). The audit row is
        # only worth recording when the request was otherwise
        # well-formed; an unknown service is a routing miss, not a
        # security event.
        try:
            svc.get_manifest_entry(name)
        except UnknownServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

        # Best-effort audit. The audit DB outage path is intentionally
        # non-fatal — the canonical 403 still fires. The
        # ``write_with_retry`` queue persists the row when Postgres is
        # back online (Requirement 11.7 deferred-queue semantics).
        try:
            await svc.record_purge_vault_blocked(name=name, actor=actor)
        except AuditUnreachableError:
            # Logged downstream by the AuditWriter; the guard's job is
            # done. Falling through to the 403 is the safer default.
            pass

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "purge_vault_forbidden_in_production",
                "detail": (
                    "purge_vault=true is forbidden when "
                    "DEPLOYMENT_PROFILE resolves to 'production'. "
                    "Switch to a dev or staging profile, or stop the "
                    "service without the purge_vault flag."
                ),
            },
        )

    try:
        result = await svc.stop(
            name=name,
            remove_volumes=remove_volumes,
            purge_vault=purge_vault,
            actor=actor,
        )
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except (AuditUnreachableError, ComposeFailureError) as exc:
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    return StopResponse.model_validate(result, from_attributes=True)


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/restart
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/restart",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartResponse,
    summary="Restart a Managed_Service (Requirement 6.6)",
)
async def restart_service(
    name: str,
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Stop then start with overrides re-read from Vault (Requirement 6.6).

    Re-application of the form-schema check inside ``LifecycleService``
    means a stale Vault state can still surface a 422 here; we treat
    it the same as the start path so the operator gets a consistent
    error envelope.
    """

    try:
        result = await svc.restart(name=name, actor=actor)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FormSchemaMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except FeatureFlagDisabledError as exc:
        # ``restart`` re-enters ``_do_start`` for the second half of
        # the operation, so the same Step 1.5 gate applies. Mirror
        # the ``start`` mapping for envelope consistency.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "feature_flag_disabled",
                "blocking_flag": exc.blocking_flag,
                "detail": str(exc),
            },
        ) from exc
    except (
        VaultWriteError,
        AuditUnreachableError,
        ComposeFailureError,
        MaxDependencyDepthExceededError,
        DependencyStartFailedError,
    ) as exc:
        # platform-mimari-uyumluluk R5.2 / R5.5 (Q11): dependency-chain
        # failures surface as 502 alongside the canonical failures.
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    return StartResponse.model_validate(result, from_attributes=True)


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/test
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/test",
    response_model=None,
    summary="Run the manifest test_command (Requirement 8.1/8.2/8.4/8.5/8.6)",
)
async def run_service_tests(
    name: str,
    request: Request,
    stream: bool = Query(default=False, description="Stream output as SSE"),
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Run the manifest ``test_command`` against ``{name}``.

    JSON path (``stream=false``): returns :class:`TestResponse`.
    SSE path (``stream=true``): returns ``text/event-stream`` with one
    ``data:`` event per stdout line, terminated by a ``done`` event
    carrying ``exit_code``.

    Requirement 8.6: a 409 is raised when the service is not running
    (``TestPreconditionError("service must be running before tests")``).
    Requirement 8.2: a 409 is raised when the manifest entry has no
    ``test_command`` (``TestPreconditionError("service has no
    test_command in manifest")``).
    """

    try:
        # Note: the underlying ComposeRunner currently captures the
        # full output regardless of ``stream`` — the SSE path slices
        # the captured stdout into ``data:`` frames after-the-fact so
        # we still benefit from the audit + summary parsing in
        # ``LifecycleService.run_tests``. A future enhancement can
        # rewire this to a true line-by-line generator without
        # changing the on-the-wire SSE shape.
        result = await svc.run_tests(name=name, stream=stream, actor=actor)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except TestPreconditionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.reason,
        ) from exc
    except (AuditUnreachableError, ComposeFailureError) as exc:
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    if stream:
        return StreamingResponse(
            _sse_test_output(result.output, result.exit_code),
            media_type="text/event-stream",
        )

    summary_model: TestSummaryModel | None = None
    if result.summary is not None:
        summary_model = TestSummaryModel.model_validate(
            result.summary, from_attributes=True
        )

    # E4 — persist the run to automation.test_runs so the dashboard
    # keeps a durable pass/fail trend (gereksinim.txt G9). Best-effort:
    # a persistence failure must not fail the test-run response.
    try:
        from .test_results import record_test_run

        await record_test_run(
            request,
            service_name=name,
            exit_code=result.exit_code,
            output=result.output or "",
            duration_ms=None,
            triggered_by=getattr(actor, "sub", "system") or "system",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "run_service_tests: failed to record run history for %s: %s",
            name,
            exc,
        )

    return TestResponse(
        output=result.output,
        exit_code=result.exit_code,
        summary=summary_model,
        correlation_id=result.correlation_id,
        audit_write_deferred=result.audit_write_deferred,
    )


async def _sse_test_output(
    output: str,
    exit_code: int,
) -> AsyncIterator[bytes]:
    """Yield one SSE ``data:`` frame per output line + a final ``done`` event.

    The terminal ``event: done`` frame carries the exit code so the
    UI knows whether to highlight the run as red or green without
    parsing the (potentially huge) output buffer.
    """

    for line in output.splitlines():
        # SSE frames are delimited by a blank line. We escape any
        # embedded ``\r`` because ``\r\n`` would prematurely close
        # the frame in some clients.
        safe = line.replace("\r", "")
        yield f"data: {safe}\n\n".encode("utf-8")
    yield (
        f"event: done\n"
        f"data: {{\"exit_code\": {exit_code}}}\n\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/smoke  (Smoke Test Runner)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/smoke",
    response_model=None,
    summary="Run the manifest smoke_test_command (≤30s quick validation)",
)
async def run_service_smoke_test(
    name: str,
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Run the manifest ``smoke_test_command`` against ``{name}``.

    Smoke tests are lightweight (≤30s) health validations that verify
    a service's core functionality without running the full integration
    suite. Returns a JSON response with output, exit_code, and a
    pass/fail badge.

    Error mapping:
    * 404 — unknown service.
    * 409 — service has no ``smoke_test_command`` in manifest.
    """
    try:
        entry = svc.get_manifest_entry(name)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    smoke_cmd = getattr(entry, "smoke_test_command", None)
    if not smoke_cmd:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"service '{name}' has no smoke_test_command in manifest",
        )

    try:
        result = await svc.run_tests(
            name=name, stream=False, actor=actor, command_override=smoke_cmd
        )
    except TestPreconditionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.reason,
        ) from exc
    except (AuditUnreachableError, ComposeFailureError) as exc:
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    badge = "pass" if result.exit_code == 0 else "fail"
    return {
        "service": name,
        "badge": badge,
        "exit_code": result.exit_code,
        "output": result.output,
    }


# ---------------------------------------------------------------------------
# POST /admin/services/smoke/all  (Run All Smoke Tests)
# ---------------------------------------------------------------------------


@router.post(
    "/smoke/all",
    summary="Run smoke tests for all services that have smoke_test_command",
)
async def run_all_smoke_tests(
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Run smoke tests for every service that has a ``smoke_test_command``.

    Returns a summary with per-service badge (pass/fail/skipped/error).
    Services without a ``smoke_test_command`` are reported as "skipped".
    """
    results: list[dict[str, Any]] = []

    for entry_name in svc.state_cache:
        try:
            entry = svc.get_manifest_entry(entry_name)
        except UnknownServiceError:
            continue

        smoke_cmd = getattr(entry, "smoke_test_command", None)
        if not smoke_cmd:
            results.append({
                "service": entry_name,
                "badge": "skipped",
                "exit_code": None,
                "reason": "no smoke_test_command",
            })
            continue

        try:
            result = await svc.run_tests(
                name=entry_name, stream=False, actor=actor,
                command_override=smoke_cmd,
            )
            badge = "pass" if result.exit_code == 0 else "fail"
            results.append({
                "service": entry_name,
                "badge": badge,
                "exit_code": result.exit_code,
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "service": entry_name,
                "badge": "error",
                "exit_code": None,
                "reason": str(exc)[:200],
            })

    passed = sum(1 for r in results if r["badge"] == "pass")
    failed = sum(1 for r in results if r["badge"] == "fail")
    errored = sum(1 for r in results if r["badge"] == "error")
    skipped = sum(1 for r in results if r["badge"] == "skipped")

    return {
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "skipped": skipped,
        },
        "results": results,
    }


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/logs
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/logs",
    response_model=None,
    summary="Tail Compose logs with redaction (Requirement 7.1/7.2/7.3/7.7)",
)
async def get_service_logs(
    name: str,
    tail: int = Query(default=200, ge=1, le=1000),
    follow: bool = Query(default=False),
    svc: LifecycleService = Depends(get_lifecycle_service),
):
    """Return tailed Compose logs (JSON) or a live SSE stream.

    Requirement 7.7 — Sensitive_Env_Key tokens are replaced with
    ``<redacted>`` before any line leaves the response. The
    redaction pattern is built from the service's ``.env.example``
    LHS keys via :meth:`LifecycleService.build_log_redaction_pattern`
    so streaming and non-streaming paths share the exact same key
    set (Property C5).
    """

    if not follow:
        # Non-streaming path delegates redaction to the orchestrator
        # so the same regex is applied as in the streaming path.
        try:
            lines = await svc.logs(name=name, tail=tail, follow=False)
        except UnknownServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ComposeFailureError as exc:
            return _gateway_failure_response(
                detail=str(exc),
                correlation_id=uuid4(),
            )
        return LogsResponse(lines=lines)

    # Streaming path — call ``compose.logs(follow=True)`` directly so
    # we get the async iterator and can redact line-by-line. Resolve
    # the manifest entry up-front so the SSE response is short-circuited
    # with a 404 before any subprocess is spawned.
    try:
        entry = svc.get_manifest_entry(name)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    pattern = svc.build_log_redaction_pattern(entry)
    log_iter = await svc.compose.logs(
        service_name=entry.compose_service_name,
        tail=tail,
        follow=True,
    )
    # ``compose.logs(follow=True)`` returns an ``AsyncIterator[str]``;
    # narrow the type before handing it off to the SSE generator.
    assert hasattr(log_iter, "__aiter__"), "expected async iterator for follow=True"

    return StreamingResponse(
        _sse_log_stream(log_iter, pattern),  # type: ignore[arg-type]
        media_type="text/event-stream",
    )


async def _sse_log_stream(
    log_iter: AsyncIterator[str],
    pattern,  # re.Pattern[str] | None — typed loosely to avoid module import
) -> AsyncIterator[bytes]:
    """Forward each log line as an SSE ``data:`` frame, redacted.

    The redaction pattern is supplied by
    :meth:`LifecycleService.build_log_redaction_pattern` and comes
    from the service's ``.env.example`` LHS Sensitive_Env_Keys
    (Requirement 7.7). When ``pattern is None`` (no sensitive keys)
    the line is forwarded unchanged.
    """

    # Local import keeps the module's import surface narrow; the
    # helper is only ever needed on the SSE path.
    from ..lifecycle.service import _redact_log_line  # type: ignore[attr-defined]

    async for raw_line in log_iter:
        safe = _redact_log_line(raw_line, pattern).replace("\r", "")
        yield f"data: {safe}\n\n".encode("utf-8")


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/probe  (R9.6 / Q10 — manual re-run)
# ---------------------------------------------------------------------------


@router.post(
    "/{name}/probe",
    response_model=ProbeResponse,
    summary="Manually re-run the connectivity probe (R9.6 / Q10)",
)
async def probe_service_connectivity(
    name: str,
    actor: AuthClaims = Depends(require_admin),
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> ProbeResponse:
    """Trigger a manual re-run of the manifest ``connectivity_probe_command``.

    Implements platform-mimari-uyumluluk Requirement 9.6 (Q10 — manual
    connectivity probe re-run). The endpoint calls the same
    :meth:`LifecycleService._run_connectivity_probe` helper that the
    automatic Step 9.5 post-start probe uses, so the same audit events
    (``service_connectivity_probe_passed`` /
    ``service_connectivity_probe_failed``) are emitted.

    The UI's service detail page calls this endpoint when the operator
    clicks the ``[Re-probe]`` button next to a ``credentials_status =
    "failed"`` banner (Requirement 9.5).

    Error mapping:

    * ``UnknownServiceError`` → 404 Not Found.
    * ``AuditUnreachableError`` → 502 Bad Gateway + ``correlation_id``.

    When the manifest entry has no ``connectivity_probe_command`` the
    call is a no-op and the response reflects ``credentials_status=None``
    (no probe configured). This is not an error — the UI should simply
    not render the ``[Re-probe]`` button for such services.
    """

    try:
        await svc.run_connectivity_probe(name=name, actor=actor)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except AuditUnreachableError as exc:
        return _gateway_failure_response(
            detail=str(exc),
            correlation_id=uuid4(),
        )

    # Reflect the updated state cache fields in the response.
    slot = svc.state_cache[name]
    return ProbeResponse(
        service_name=name,
        credentials_status=slot.credentials_status,
        credentials_probe_at=slot.credentials_probe_at,
        credentials_probe_detail=slot.credentials_probe_detail,
    )


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/health
# ---------------------------------------------------------------------------


@router.get(
    "/{name}/health",
    response_model=HealthSnapshotModel,
    summary="Fresh Health_Snapshot (Requirement 7.4/7.5/7.6)",
)
async def get_service_health(
    name: str,
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> HealthSnapshotModel:
    """Return a fresh :class:`HealthSnapshot` for ``{name}``.

    The orchestrator's :meth:`health_of` honours the cache TTL and
    increments the consecutive-unhealthy streak counter (Requirement
    12.5) on every probe. The router only adapts the result; it does
    not poll or alert.
    """

    try:
        snapshot = await svc.health_of(name=name)
    except UnknownServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return HealthSnapshotModel.model_validate(snapshot, from_attributes=True)


# ---------------------------------------------------------------------------
# GET /admin/services/ssh-runner/status  (SSH Runner Status — healthcheck)
# ---------------------------------------------------------------------------


@router.get(
    "/ssh-runner/status",
    summary="SSH Runner connectivity status (cascade healthcheck)",
)
async def get_ssh_runner_status(
    svc: LifecycleService = Depends(get_lifecycle_service),
) -> dict[str, Any]:
    """Return the current SSH runner connectivity status.

    Performs a lightweight TCP connect probe against the configured SSH
    runner host (same probe as the ``ssh_healthcheck`` activity in the
    execution-runner-worker). The result is returned immediately — no
    caching — so the admin-dashboard UI can show real-time runner
    availability.

    Response shape::

        {
            "status": "healthy" | "unhealthy" | "unconfigured",
            "host": "runner-host",
            "port": 22,
            "error": null | "TCP connect failed: ...",
            "checked_at": "2024-01-15T10:30:00Z"
        }

    When no SSH host is configured (neither ``SSH_HOST`` nor the
    deprecated ``SSH_HOST_1`` alias is set) the response returns
    ``status="unconfigured"`` so the UI can render a "no runner
    configured" banner instead of a red error.

    Single-runner canonical contract: the platform runs **exactly one**
    SSH runner. ``SSH_HOST`` is canonical; ``SSH_HOST_1`` is preserved
    as a deprecated alias for backwards compatibility.
    """
    import os
    import socket
    from datetime import datetime, timezone

    host = os.environ.get("SSH_HOST", "").strip()
    if not host:
        host = os.environ.get("SSH_HOST_1", "").strip()
    if not host:
        return {
            "status": "unconfigured",
            "host": None,
            "port": None,
            "error": "SSH_HOST not configured (canonical) — no SSH_HOST_1 alias either",
            "checked_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    port_raw = os.environ.get("SSH_PORT_DEFAULT", "22")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22

    timeout_raw = os.environ.get("SSH_CONNECT_TIMEOUT_S", "15")
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = 15.0

    # Perform TCP connect probe.
    error: str | None = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        status_val = "healthy"
    except OSError as exc:
        status_val = "unhealthy"
        error = f"TCP connect failed: {exc}"
    finally:
        sock.close()

    return {
        "status": status_val,
        "host": host,
        "port": port,
        "error": error,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }


__all__ = (
    "get_lifecycle_service",
    "get_settings_dependency",
    "router",
)
