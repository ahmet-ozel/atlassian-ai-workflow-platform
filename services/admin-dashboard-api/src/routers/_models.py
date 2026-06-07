"""Pydantic v2 request/response models for the lifecycle REST router.

Co-located with ``services_lifecycle.py`` so the HTTP boundary stays
in one folder. The orchestrator (``src/lifecycle/service.py``) is
deliberately Pydantic-free - it ships frozen dataclasses
(:class:`~src.lifecycle.service.ServiceSummary`,
:class:`~src.lifecycle.service.StartResponse`, ...) - and this module
adapts those into Pydantic models for FastAPI's serialiser.

Model coverage
--------------
* endpoint matrix and JSON shapes.
* start request body shape ``{env_overrides: {...}}``.
* list summary row shape.
* service detail + ``form_schema`` rows.
* stop request/response (with ``noop``).
* logs + health response shapes.
* test response shape.
* ``correlation_id`` echoed in 502 envelopes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Health snapshot
# ---------------------------------------------------------------------------


class HealthSnapshotModel(BaseModel):
    """Pydantic mirror of :class:`src.lifecycle.health_probe.HealthSnapshot`.

    Field set is identical so the router can construct the model from
    a snapshot via ``HealthSnapshotModel.model_validate(snapshot,
    from_attributes=True)`` without copying field-by-field.
    """

    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    healthz_status: int
    healthz_body: str
    readyz_status: int | None = None
    readyz_body: str | None = None
    state: Literal[
        "healthy",
        "unhealthy",
        "starting",
        "unknown",
        "running_unmonitored",
    ]


# ---------------------------------------------------------------------------
# Service summary / detail
# ---------------------------------------------------------------------------


ServiceState = Literal[
    "stopped", "starting", "running", "unhealthy", "failed", "running_unmonitored"
]

#: Tooltip text for the ``running_unmonitored`` badge (Feature 14).
#: Services without a ``health_endpoint`` but with Docker State.Running
#: receive this badge so operators know the service is alive but not
#: actively health-checked.
RUNNING_UNMONITORED_TOOLTIP: str = (
    "Healthcheck tanımlı değil; native Docker State.Running ile takip ediliyor."
)
ServiceKind = Literal["http_service", "worker", "ui", "infra", "sidecar"]


class ServiceSummary(BaseModel):
    """Row shape returned by ``GET /admin/services`` (behavior 6.1)."""

    model_config = ConfigDict(from_attributes=True)

    name: str
    kind: ServiceKind
    state: ServiceState
    last_started_at: datetime | None = None
    last_health_snapshot: HealthSnapshotModel | None = None


class FormSchemaField(BaseModel):
    """One row of the ``form_schema`` array (behavior 5.1, 6.2)."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    default_value: str
    comment: str | None = None
    is_sensitive: bool


class FormSchema(BaseModel):
    """``form_schema`` envelope used inside :class:`ServiceDetail`."""

    fields: list[FormSchemaField] = Field(default_factory=list)


class ServiceDetail(BaseModel):
    """Body shape of ``GET /admin/services/{name}`` (behavior 6.2).

    The model embeds the manifest entry verbatim plus the *current*
    cached :class:`HealthSnapshotModel` and the form schema rendered
    from the service's ``.env.example`` file.

    Connectivity probe fields (``credentials_status``,
    ``credentials_probe_at``, ``credentials_probe_detail``) reflect
    the most recent Step 9.5 / manual probe outcome (behavior 9.5);
    they remain ``None`` when the manifest entry has no
    ``connectivity_probe_command`` (no probe configured) so the UI can
    skip rendering the credentials banner for such services.
    """

    name: str
    kind: ServiceKind
    compose_service_name: str
    compose_profile: str
    env_example_path: str
    health_endpoint: str | None = None
    test_command: str | None = None
    state: ServiceState
    last_started_at: datetime | None = None
    last_health_snapshot: HealthSnapshotModel | None = None
    form_schema: FormSchema
    credentials_status: Literal["ok", "failed", "unknown"] | None = None
    credentials_probe_at: datetime | None = None
    credentials_probe_detail: str | None = None


# ---------------------------------------------------------------------------
# Lifecycle request / response models
# ---------------------------------------------------------------------------


class StartRequest(BaseModel):
    """``POST /admin/services/{name}/start`` body (behavior 5.5).

    The map is intentionally typed as ``dict[str, str]`` even though
    Pydantic would happily accept arbitrary JSON: every Env_Override
    key/value is destined for either Vault (which only stores
    strings) or a child process's environment dict (which only
    accepts strings). Coercing here keeps the form-schema check in
    :meth:`LifecycleService.start` deterministic.
    """

    env_overrides: dict[str, str] = Field(default_factory=dict)


class StartResponse(BaseModel):
    """``POST /admin/services/{name}/start`` 202 body."""

    model_config = ConfigDict(from_attributes=True)

    state: ServiceState
    correlation_id: UUID
    audit_write_deferred: bool = False


class StopRequest(BaseModel):
    """``POST /admin/services/{name}/stop`` body (behavior 6.4).

    The optional ``purge_vault`` flag (platform operations rule 14 /
    Q16) instructs the orchestrator to delete every Vault override under
    ``secret/services/{name}/`` after the Compose ``stop`` step
    completes. The router's ``stop_service`` endpoint refuses the flag
    when ``settings.deployment_profile == "production"`` (returns 403
    + ``purge_vault_forbidden_in_production``); the actual purge
    behaviour for non-production profiles is wired in the lifecycle layer.
    Defaults to ``False`` for backward compatibility with callers that
    only know about ``remove_volumes``.
    """

    remove_volumes: bool = False
    purge_vault: bool = False


class StopResponse(BaseModel):
    """``POST /admin/services/{name}/stop`` 200 body."""

    model_config = ConfigDict(from_attributes=True)

    state: ServiceState
    correlation_id: UUID
    noop: bool = False
    audit_write_deferred: bool = False


# ---------------------------------------------------------------------------
# Test execution response (behavior 8.4)
# ---------------------------------------------------------------------------


class TestSummaryModel(BaseModel):
    """Parsed pytest summary line (subset of behavior 8.4).

    The orchestrator's ``TestSummary`` carries ``passed``, ``failed``,
    ``duration_seconds``. The ``errors`` field mentioned in
    behavior 8.4 is not yet parsed by ``LifecycleService`` (the
    canonical pytest summary regex captures only passed/failed) so
    it is omitted here rather than fabricated.
    """

    model_config = ConfigDict(from_attributes=True)

    passed: int
    failed: int
    duration_seconds: float


class TestResponse(BaseModel):
    """``POST /admin/services/{name}/test`` 200 body."""

    model_config = ConfigDict(from_attributes=True)

    output: str
    exit_code: int
    summary: TestSummaryModel | None = None
    correlation_id: UUID
    audit_write_deferred: bool = False


# ---------------------------------------------------------------------------
# Logs (behavior 7.1, 7.2)
# ---------------------------------------------------------------------------


class LogsResponse(BaseModel):
    """``GET /admin/services/{name}/logs`` 200 body (non-streaming).

    Streaming responses (``follow=true``) bypass this model and are
    served through :class:`fastapi.responses.StreamingResponse`.
    """

    lines: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Start plan (platform operations behavior 5.6 / Q11)
# ---------------------------------------------------------------------------


class StartPlanResponse(BaseModel):
    """``GET /admin/services/{name}/start-plan`` 200 body.

    Implements platform operations behavior 5.6 (Q11 -
    dependency chain orchestration preview). The UI fetches this
    payload before the operator presses *Start* so it can render a
    confirmation modal listing every transitive dependency that will
    be brought up alongside the target service.

    Field semantics
    ---------------
    * ``target_service`` - the service the operator clicked. Echoed
      back so the UI can correlate the response with the originating
      request without parsing the URL.
    * ``will_start`` - manifest-resident services that the
      :meth:`LifecycleService.start` call will visit and start (or
      attempt to start) under Step 1.6 (dependency chain) and Step 8
      (the parent's own ``compose.up``). Sorted in topological order
      with **dependencies before dependents** so the UI can render the
      list in the same order the chain will execute.
    * ``already_running`` - manifest-resident services in the
      transitive closure that are currently in ``state="running"`` and
      will therefore be skipped (behavior 5.3 idempotent skip).

    External dependencies (Boot_Bundle infra such as ``postgres``,
    ``vault``, ``temporal``) that appear in
    :attr:`ManagedServiceEntry.depends_on_services` but are not
    themselves manifest entries are intentionally **omitted** from
    both lists. The cascade aggregator (``healthcheck`` router) treats
    them as raw status lookups; the lifecycle service cannot start
    them, so listing them in the plan would mislead the operator.
    """

    target_service: str
    will_start: list[str] = Field(default_factory=list)
    already_running: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Connectivity probe (platform operations behavior 9.6 / Q10)
# ---------------------------------------------------------------------------


class ProbeResponse(BaseModel):
    """``POST /admin/services/{name}/probe`` 200 body.

    Implements platform operations behavior 9.6 (Q10 - manual
    connectivity probe re-run). The response reflects the *current*
    state of the ``credentials_status`` field in the in-memory state
    cache after the probe has completed.

    Field semantics
    ---------------
    * ``service_name`` - echoed back from the URL path parameter.
    * ``credentials_status`` - ``"ok"`` when the probe command exited
      with code 0; ``"failed"`` on non-zero exit, timeout, or OS error;
      ``None`` when the manifest entry has no ``connectivity_probe_command``
      (no probe configured).
    * ``credentials_probe_at`` - UTC timestamp of the probe execution,
      or ``None`` when no probe was run (no command configured).
    * ``credentials_probe_detail`` - last 500 characters of stderr from
      a failed probe, or ``None`` when the probe passed or was not run.
    """

    service_name: str
    credentials_status: Literal["ok", "failed", "unknown"] | None = None
    credentials_probe_at: datetime | None = None
    credentials_probe_detail: str | None = None


# ---------------------------------------------------------------------------
# Error envelope (behavior 6.7, 11.8)
# ---------------------------------------------------------------------------


class ErrorEnvelope(BaseModel):
    """502 error envelope used for upstream gateway failures.

    Returned as the body of ``HTTP 502`` responses raised by
    Vault / Audit / Compose failures. The ``correlation_id`` lets
    the operator pivot between the response, the audit log row, and
    the structured server logs (behavior 6.7, 11.8).
    """

    detail: str
    correlation_id: UUID


__all__ = (
    "ErrorEnvelope",
    "FormSchema",
    "FormSchemaField",
    "HealthSnapshotModel",
    "LogsResponse",
    "ProbeResponse",
    "RUNNING_UNMONITORED_TOOLTIP",
    "ServiceDetail",
    "ServiceKind",
    "ServiceState",
    "ServiceSummary",
    "StartPlanResponse",
    "StartRequest",
    "StartResponse",
    "StopRequest",
    "StopResponse",
    "TestResponse",
    "TestSummaryModel",
)
