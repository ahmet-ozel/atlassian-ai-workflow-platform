"""``post_cost_prediction_comment`` - best-effort Jira yorum yazma.

Posts the cost prediction comment after :class:`BudgetCapPolicy.enforce`
returns ``allow``.

Why this lives in ``automation-service`` (and not in the worker)
----------------------------------------------------------------

The cost prediction is computed at **workflow start** time by the
``automation-service`` HTTP handler. The prediction is the side product
of the ``CostPredictor`` call sitting in front of
:class:`BudgetCapPolicy`. The user-visible
artefact that explains the prediction lives on the originating Jira
issue, so the most economical place to post the comment is
**before** the workflow signal-with-start: a workflow that fails to
start would otherwise never write the comment, and a workflow that
starts successfully does not need to drag the prediction value
through Temporal payloads just so an activity inside the workflow
can post it.

Best-effort semantics
---------------------

The user-facing contract treats this comment as the
``best_effort`` partition of output handling: if the MCP call, the credential resolution, or
the JSON-RPC parse fails, the workflow start MUST still succeed.
The failure is recorded as a single ``cost_prediction_comment_failed``
audit event (severity-ish via the ``result="error"`` field) so the
``/costs`` panel and Loki search can surface "comment was skipped"
to operators. This module implements the best-effort wrapping inline
rather than partitioning a one-element list of actions through a
shared output layer. The shape it returns (:class:`CostCommentOutcome`)
is deliberately compatible with the eventual partition / apply API
so a later refactor can splice it in without changing call sites.

MCP wiring
----------

Every outbound Atlassian HTTP call goes through the
``atlassian_unified`` MCP service. This module routes through the same
``http_shared.make_mcp_client`` + ``http_shared.with_atlassian_creds``
plumbing that the ``agent-runner-worker`` activities use for
``jira_add_comment``. The outbound call invokes the ``jira_add_comment``
MCP tool over JSON-RPC ``tools/call`` - identical surface to
``platform/workers/agent-runner-worker/src/activities/jira.py`` so the
two callers stay observationally indistinguishable from the MCP's
perspective (cred header layout, body shape, error mapping).

The ``mcp_client.atlassian_client.AtlassianClient`` skeleton is
intentionally not used here: that class is a banned-tool / PR-draft
enforcement chokepoint and its
``open_pull_request`` method still raises :class:`NotImplementedError`
in the current implementation. The ``available_tools`` filter is
irrelevant for a single hard-coded ``jira_add_comment`` call. When
the shared client gains a real HTTP transport, this module can swap
its inline JSON-RPC for a call into that
client without changing its public surface.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Final, Literal, Protocol, runtime_checkable

import httpx

from audit_logger import AuditEvent, AuditLogger
from http_shared import (
    CredentialResolutionError,
    make_mcp_client,
    with_atlassian_creds,
)

__all__ = [
    "CostCommentOutcome",
    "CostPredictionLike",
    "DeptCostPanelLinker",
    "JiraCommentPoster",
    "McpClientFactory",
    "post_cost_prediction_comment",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants - body templates and MCP wiring
# ---------------------------------------------------------------------------

#: MCP streamable-http endpoint path; identical to
#: ``agent-runner-worker``'s ``_MCP_PATH`` so both callers reach the
#: same JSON-RPC surface.
_MCP_PATH: Final[str] = "/mcp"

#: Identifier carried on the ``X-Client-Source`` header by
#: :func:`make_mcp_client`. Used by the MCP for per-caller observability.
_CLIENT_SOURCE: Final[str] = "automation-service"

#: Default per-call timeout (seconds). The body is small and the call
#: is best-effort - keeping the timeout tight prevents a slow MCP
#: from delaying workflow start.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 15.0

#: Body line listing the prediction with its 80% confidence interval.
#: The prefix is " Tahmini maliyet" and the source label
#: is rendered verbatim - ``dept`` or ``global_fallback`` - so admins
#: can grep for it in Jira.
_BODY_HEADER_TEMPLATE: Final[str] = (
    " Tahmini maliyet: ${predicted} (CI %80: ${low}-${high}). "
    "Kaynak: {source}."
)

#: Extra disclosure note appended for the ``global_fallback`` source
#: for cold-start transparency.
_GLOBAL_FALLBACK_NOTE: Final[str] = (
    "Bu departmanın geçmiş verisi henüz az; tahmin global ortalamadan üretildi."
)

#: Two-character monetary scale: USD with cents. The CostPredictor
#: produces ``Decimal`` values that may carry more precision; we
#: quantise here to match the user-visible "$X.YY" rendering used
#: by the ``/costs`` panel.
_USD_QUANT: Final[Decimal] = Decimal("0.01")


# ---------------------------------------------------------------------------
# Protocols - keep the module decoupled from concrete dependency types
# ---------------------------------------------------------------------------


@runtime_checkable
class CostPredictionLike(Protocol):
    """Structural type matching :class:`cost_tracking.CostPrediction`.

    The cost-tracking lib ships a frozen dataclass with
    these four attributes. Declaring the dependency as a Protocol
    keeps this module compilable and testable without an import-time
    coupling on a sibling library that may still be evolving. The
    real dataclass - once available - satisfies the Protocol via
    structural subtyping; tests inject a tiny ``@dataclass(frozen=True)``
    that does the same.

    Attributes mirror the ``CostPrediction`` data model exactly
    (Decimals so monetary precision is preserved end-to-end).
    """

    @property
    def predicted_usd(self) -> Decimal: ...  # pragma: no cover - protocol

    @property
    def confidence_low(self) -> Decimal: ...  # pragma: no cover - protocol

    @property
    def confidence_high(self) -> Decimal: ...  # pragma: no cover - protocol

    @property
    def source(self) -> str: ...  # pragma: no cover - protocol


#: Factory protocol for ``httpx.AsyncClient`` creation. The default
#: production wiring binds this to :func:`http_shared.make_mcp_client`;
#: tests pass a function returning an ``httpx.AsyncClient`` built on
#: top of an ``httpx.MockTransport``.
McpClientFactory = Callable[..., httpx.AsyncClient]


@runtime_checkable
class JiraCommentPoster(Protocol):
    """Optional injection point for callers that want to swap out HTTP.

    Defaults to the inline MCP path; tests rarely need to swap this
    out because the :class:`McpClientFactory` already covers the
    happy and sad paths. Exposed so a future ``output_actions.apply``
    integration can reuse the body composer without re-running MCP
    transport.
    """

    async def post(
        self,
        *,
        issue_key: str,
        body: str,
        dept_id: str,
    ) -> None:  # pragma: no cover - protocol
        ...


#: Optional resolver that turns a ``dept_id`` into a deep-link URL for
#: the admin dashboard ``/costs?dept_id=...`` panel. When set, the
#: comment includes a "Departman maliyet panelini aç" link. Returning
#: ``None`` skips the link silently - production wiring may not have
#: a stable public URL in dev environments.
DeptCostPanelLinker = Callable[[str], str | None]


# ---------------------------------------------------------------------------
# Outcome dataclass
# ---------------------------------------------------------------------------


#: Discriminator labels for :class:`CostCommentOutcome`. Mirrors
#: the eventual ``ApplyResult`` outcomes (``ok``, ``failed``,
#: ``skipped``) so a future refactor can lift this module's
#: outcome shape into the partition framework with no rename.
CostCommentStatus = Literal["posted", "skipped", "failed"]


@dataclass(frozen=True, slots=True)
class CostCommentOutcome:
    """Result of a :func:`post_cost_prediction_comment` call.

    The function is best-effort and never raises on transport /
    credential / MCP errors; callers inspect this dataclass when they
    care to surface the result (eg. structured logs or a debug
    response field). The dataclass is intentionally minimal - it
    carries enough to describe **what happened** without leaking the
    Jira body or the MCP response, both of which would expand the
    audit trail beyond the required operational fields.

    Attributes:
        status: ``"posted"`` on a clean ``2xx`` from the MCP,
            ``"failed"`` when any transport / credential / parse
            error was swallowed, ``"skipped"`` when the caller asked
            for an explicit no-op (eg. cost prediction was missing).
        issue_key: The Jira issue the call targeted. Echoed back so
            structured logs can correlate without a separate field.
        body_chars: Length of the rendered body, measured **after**
            template substitution. Useful as a smoke-check for
            unbounded growth - the comment is bounded above by the
            template + a deep-link URL, so anything > a few hundred
            chars points at a regression.
        error: Short error description on ``"failed"``; ``None``
            otherwise. Intentionally plain ``str`` (not an exception
            instance) so the outcome is JSON-serialisable.
    """

    status: CostCommentStatus
    issue_key: str
    body_chars: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Body composer (pure function - easy to unit test)
# ---------------------------------------------------------------------------


def _format_usd(value: Decimal) -> str:
    """Render a :class:`Decimal` USD value as ``"X.YY"``.

    Quantises to two decimal places using ``ROUND_HALF_EVEN``
    (Python's :class:`Decimal` default). The output is plain digits
    with a single ``.`` separator and no thousands grouping - easy to
    parse downstream and consistent with how ``departments.json``
    serialises cap values.
    """

    return format(value.quantize(_USD_QUANT), "f")


def _compose_body(
    *,
    prediction: CostPredictionLike,
    cost_panel_url: str | None,
) -> str:
    """Render the Jira comment body for a cost prediction.

    Pulled out of the public function so the formatting logic can be
    unit-tested without standing up an MCP transport. The body has
    three layered sections:

    1. Header line - always present; contains the predicted value,
       the 80% CI bounds, and the source label.
    2. Global-fallback disclosure - appended only when
       ``prediction.source == "global_fallback"``.
    3. Cost panel deep-link - appended only when ``cost_panel_url``
       is a non-empty string. Allows the comment author to point at
       the live dashboard without forcing a stable URL on every dept
       at this stage of the rollout.
    """

    body_parts: list[str] = [
        _BODY_HEADER_TEMPLATE.format(
            predicted=_format_usd(prediction.predicted_usd),
            low=_format_usd(prediction.confidence_low),
            high=_format_usd(prediction.confidence_high),
            source=prediction.source,
        )
    ]

    if prediction.source == "global_fallback":
        body_parts.append(_GLOBAL_FALLBACK_NOTE)

    if cost_panel_url:
        # A bare URL on its own line so Jira's auto-linker picks it
        # up; the leading label is a Turkish phrase the rest of the
        # UI already uses (matches admin-dashboard /costs sayfası
        # button text).
        body_parts.append(f"Departman maliyet paneli: {cost_panel_url}")

    return "\n\n".join(body_parts)


# ---------------------------------------------------------------------------
# JSON-RPC helpers - kept tiny so the module stays self-contained
# ---------------------------------------------------------------------------


def _build_jsonrpc_request(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    request_id: int = 1,
) -> dict[str, Any]:
    """Construct a JSON-RPC ``tools/call`` envelope.

    The shape matches ``agent-runner-worker``'s ``_build_jsonrpc_request``
    exactly so the MCP cannot tell the two callers apart (other than
    via ``X-Client-Source``). Kept inline here rather than imported
    from the worker because the worker's helpers live behind a
    Temporal activity decorator import path that pulls in the
    ``temporalio`` runtime - a heavyweight dependency the
    HTTP-only ``automation-service`` does not need on a best-effort
    Jira write.
    """

    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
        "id": request_id,
    }


def _interpret_mcp_response(payload: dict[str, Any]) -> None:
    """Raise :class:`RuntimeError` if the MCP response carries an error.

    The MCP wraps tool-call results in a JSON-RPC envelope; an
    application-level failure is reported either via a top-level
    ``error`` member (transport / unknown tool) or via
    ``result.isError`` (tool reported a problem). We treat both as
    failure and let the caller swallow the exception via the
    best-effort wrapper.
    """

    if "error" in payload:
        err = payload["error"]
        # JSON-RPC error members have ``code`` + ``message``; fall back
        # to ``str(err)`` for non-conformant servers so we never
        # KeyError our way out of a best-effort path.
        if isinstance(err, dict):
            raise RuntimeError(
                f"MCP jira_add_comment error: code={err.get('code')!r} "
                f"message={err.get('message')!r}"
            )
        raise RuntimeError(f"MCP jira_add_comment error: {err!r}")

    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(
            f"MCP jira_add_comment tool reported error: {result.get('content')!r}"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def post_cost_prediction_comment(
    *,
    issue_key: str,
    prediction: CostPredictionLike | None,
    dept_id: str,
    credential_resolver: Any,
    mcp_base_url: str,
    audit_logger: AuditLogger | None = None,
    actor_id: str = "system",
    dept_cost_panel_linker: DeptCostPanelLinker | None = None,
    mcp_client_factory: McpClientFactory | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    clock: Callable[[], datetime] | None = None,
) -> CostCommentOutcome:
    """Post the cost prediction as a best-effort Jira comment.

    Called by the workflow start handler **after**
    :meth:`BudgetCapPolicy.enforce` has returned an ``allow`` decision
    for the cost prediction and budget cap enforcement sequence. The
    function never raises on MCP / credential /
    transport failure; on any error it records a single
    ``cost_prediction_comment_failed`` audit event (when an
    :class:`AuditLogger` was provided) and returns a
    :class:`CostCommentOutcome` describing the outcome. On success
    a ``cost_prediction_comment_posted`` audit event is written.

    Args:
        issue_key: The originating Jira issue key (eg. ``"PAY-4211"``).
            Empty values raise :class:`ValueError` because the call
            site is the workflow start handler, which always knows
            the issue at this point - passing an empty value indicates
            a programming error rather than a recoverable runtime
            condition.
        prediction: The cost prediction value object produced by the
            ``CostPredictor`` call. Any object satisfying
            :class:`CostPredictionLike` is accepted. ``None`` is
            tolerated and returns an outcome with status
            ``"skipped"`` so callers that have a feature-flagged
            predictor can no-op without an ``if`` at the call site.
        dept_id: Department identifier used for credential resolution
            and (optionally) the deep-link URL.
        credential_resolver: A :class:`CredentialResolver`-like object
            (anything compatible with :func:`http_shared.with_atlassian_creds`'s
            duck-typed contract - must expose async ``get(dept_id,
            service, scope=...)``).
        mcp_base_url: The ``atlassian_unified`` MCP base URL. Threaded
            through from ``automation_service.config`` rather than
            re-read here so the function stays unit-testable without
            environment fixtures.
        audit_logger: Optional :class:`AuditLogger`. When supplied,
            the function writes a ``cost_prediction_comment_*`` event
            on every code path (posted / failed / skipped).
        actor_id: ``actor_id`` recorded on audit events. Defaults to
            ``"system"`` because the call is part of the workflow
            start gate, not a user-driven action; callers may pass
            the originating user id for richer attribution.
        dept_cost_panel_linker: Optional callable resolving a
            ``dept_id`` to a public ``/costs`` panel URL. Returning
            ``None`` (or omitting the argument) suppresses the
            deep-link line silently.
        mcp_client_factory: Optional override for the
            ``httpx.AsyncClient`` factory; defaults to
            :func:`http_shared.make_mcp_client`. Tests pass a factory
            that returns a client backed by an
            ``httpx.MockTransport`` so the function can be exercised
            without a live MCP.
        timeout_seconds: Per-call HTTP timeout. Defaults to
            :data:`_DEFAULT_TIMEOUT_SECONDS`; the function does not
            retry - best-effort means a single attempt.
        clock: Callable returning a timezone-aware UTC ``datetime``;
            defaults to ``datetime.now(timezone.utc)``. Tests inject
            a fake so audit timestamps are deterministic.

    Returns:
        :class:`CostCommentOutcome` describing the outcome. The
        ``status`` field is the discriminator the caller should switch
        on; the surrounding fields are useful for structured logging
        and metrics.

    Raises:
        ValueError: If ``issue_key`` or ``dept_id`` is the empty
            string; both are required for a meaningful audit row and
            the workflow start handler always knows them.

    Side effects:
        Writes at most one audit event per call (the matching
        ``cost_prediction_comment_*`` action). Issues a single MCP
        call when ``prediction`` is non-``None``. Never logs the
        Jira body (which is short and bounded but treated as user-
        visible content best surfaced via the ``/costs`` panel).
    """

    if not isinstance(issue_key, str) or not issue_key:
        raise ValueError("issue_key must be a non-empty string")
    if not isinstance(dept_id, str) or not dept_id:
        raise ValueError("dept_id must be a non-empty string")

    now = (clock or _default_clock)()

    if prediction is None:
        await _emit_audit(
            audit_logger,
            action="cost_prediction_comment_skipped",
            actor_id=actor_id,
            dept_id=dept_id,
            issue_key=issue_key,
            result="ok",
            timestamp=now,
            payload={"reason": "no_prediction"},
        )
        return CostCommentOutcome(
            status="skipped",
            issue_key=issue_key,
            body_chars=0,
            error=None,
        )

    cost_panel_url: str | None = None
    if dept_cost_panel_linker is not None:
        try:
            cost_panel_url = dept_cost_panel_linker(dept_id)
        except Exception as exc:  # noqa: BLE001 - best-effort
            # A misbehaving linker must not break the comment path;
            # log at warning and proceed without the deep-link.
            _LOG.warning(
                "dept_cost_panel_linker raised %s for dept_id=%s; "
                "continuing without deep-link",
                type(exc).__name__,
                dept_id,
            )
            cost_panel_url = None

    body = _compose_body(prediction=prediction, cost_panel_url=cost_panel_url)

    factory = mcp_client_factory or _default_mcp_client_factory

    try:
        await _send_jira_comment(
            issue_key=issue_key,
            body=body,
            dept_id=dept_id,
            credential_resolver=credential_resolver,
            mcp_base_url=mcp_base_url,
            mcp_client_factory=factory,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort wrapper
        # Map the failure to a short error string for both the audit
        # payload and the outcome, then return without re-raising. The
        # exception is logged at WARNING (not ERROR) because the
        # caller is the workflow start handler and a missed comment
        # is a soft failure mode.
        error_msg = f"{type(exc).__name__}: {exc}"
        _LOG.warning(
            "cost_prediction_comment failed for issue_key=%s dept_id=%s: %s",
            issue_key,
            dept_id,
            error_msg,
        )
        await _emit_audit(
            audit_logger,
            action="cost_prediction_comment_failed",
            actor_id=actor_id,
            dept_id=dept_id,
            issue_key=issue_key,
            result="error",
            timestamp=now,
            payload={
                "source": prediction.source,
                "predicted_usd": _format_usd(prediction.predicted_usd),
                "error": error_msg,
            },
        )
        return CostCommentOutcome(
            status="failed",
            issue_key=issue_key,
            body_chars=len(body),
            error=error_msg,
        )

    await _emit_audit(
        audit_logger,
        action="cost_prediction_comment_posted",
        actor_id=actor_id,
        dept_id=dept_id,
        issue_key=issue_key,
        result="ok",
        timestamp=now,
        payload={
            "source": prediction.source,
            "predicted_usd": _format_usd(prediction.predicted_usd),
            "confidence_low": _format_usd(prediction.confidence_low),
            "confidence_high": _format_usd(prediction.confidence_high),
            "body_chars": len(body),
        },
    )
    return CostCommentOutcome(
        status="posted",
        issue_key=issue_key,
        body_chars=len(body),
        error=None,
    )


# ---------------------------------------------------------------------------
# Helpers - MCP transport + audit emission
# ---------------------------------------------------------------------------


async def _send_jira_comment(
    *,
    issue_key: str,
    body: str,
    dept_id: str,
    credential_resolver: Any,
    mcp_base_url: str,
    mcp_client_factory: McpClientFactory,
    timeout_seconds: float,
) -> None:
    """Issue the MCP ``jira_add_comment`` JSON-RPC call.

    Mirrors the structure of
    ``platform/workers/agent-runner-worker/src/activities/jira.py``'s
    ``jira_add_comment`` activity: build a client via
    :func:`http_shared.make_mcp_client`, inject Atlassian credentials
    via :func:`http_shared.with_atlassian_creds`, POST a JSON-RPC
    ``tools/call`` envelope to ``/mcp``, and raise on a non-2xx
    response or an ``error`` member in the JSON-RPC response.

    Raises:
        :class:`httpx.HTTPError` on transport problems.
        :class:`http_shared.CredentialResolutionError` if the dept
            has no Jira credential or the secret is incomplete.
        :class:`RuntimeError` on JSON-RPC application errors.
    """

    client = mcp_client_factory(
        client_source=_CLIENT_SOURCE,
        timeout=timeout_seconds,
        base_url=mcp_base_url,
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service="jira",
            credential_resolver=credential_resolver,
        ) as authed_client:
            request_body = _build_jsonrpc_request(
                tool_name="jira_add_comment",
                arguments={"issue_key": issue_key, "comment": body},
            )
            response = await authed_client.post(_MCP_PATH, json=request_body)
            response.raise_for_status()
            _interpret_mcp_response(response.json())


async def _emit_audit(
    audit_logger: AuditLogger | None,
    *,
    action: str,
    actor_id: str,
    dept_id: str,
    issue_key: str,
    result: Literal["ok", "denied", "error"],
    timestamp: datetime,
    payload: dict[str, Any],
) -> None:
    """Write a ``cost_prediction_comment_*`` audit event when configured.

    Centralised so every code path in
    :func:`post_cost_prediction_comment` audits with the same shape:

    * ``actor_role="system"`` - the workflow start handler invokes
      this on the user's behalf, but the comment itself is a system
      action (no human pressed "post comment"). The ``actor_id``
      field carries the human attribution when the caller passes it.
    * ``resource=f"jira:{issue_key}"`` so the ``/audit`` panel can
      filter all events targeting the same issue.
    * ``payload`` always carries the prediction ``source`` and the
      USD-quantised ``predicted_usd`` (when available) so a downstream
      query can reconstruct the user-visible body without storing it
      verbatim.

    A ``None`` ``audit_logger`` short-circuits to a no-op so unit
    tests that do not exercise audit can pass ``None`` and inspect
    the function's return value alone.
    """

    if audit_logger is None:
        return
    await audit_logger.write(
        AuditEvent(
            actor_id=actor_id,
            actor_role="system",
            dept_id=dept_id,
            action=action,
            resource=f"jira:{issue_key}",
            result=result,
            timestamp=timestamp,
            payload=payload,
        )
    )


def _default_clock() -> datetime:
    """Return ``datetime.now(timezone.utc)`` - overridable for tests."""

    return datetime.now(timezone.utc)


def _default_mcp_client_factory(
    *,
    client_source: str,
    timeout: float,
    base_url: str,
) -> httpx.AsyncClient:
    """Production wiring - defer to :func:`http_shared.make_mcp_client`.

    Wrapped in a thin function (rather than referenced directly) so
    the module's public surface lists :data:`McpClientFactory` as the
    extension point. Production callers omit the override and get
    the same client construction the worker uses.
    """

    return make_mcp_client(
        client_source=client_source,
        timeout=timeout,
        base_url=base_url,
    )
