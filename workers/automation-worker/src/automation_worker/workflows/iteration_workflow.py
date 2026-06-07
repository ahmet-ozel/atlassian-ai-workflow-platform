"""``IterationWorkflow`` - entry point for ``[iterate]``-driven re-runs.

This workflow is the Temporal-side counterpart of the dispatcher's
``[iterate]`` detection (see:mod:`platform.services.automation-service.src.webhooks.dispatcher`).
The webhook dispatcher already:

* matches the ``[iterate]`` keyword in the comment body
 (case-insensitive -:data:`webhooks.dispatcher._ITERATE_PATTERN`),
* enforces authorization (``approvers`` ∪ ``reporter``) via:meth:`WebhookDispatcher._is_iterate_authorized`,
* extracts free-form ``extra_instructions`` from the comment body, and
* emits the ``dispatch_iteration_started`` /
 ``dispatch_iteration_unauthorized`` audit rows.

What is left for the workflow side (this module / spec):

1. Run the:func:`prepare_iteration` activity *first* - it loads the
 most recent stored iteration row, increments to ``N+1``, builds the
 ``{base}/{issue_key}/iter-{N+1}`` workspace path (/), and persists a ``shared.workflow_iterations`` row with
 status ``"pending"``. The activity also runs its own authorization
 check as a defence-in-depth gate so a misbehaving caller cannot
 bypass by going around the dispatcher.
2. On a *not-authorized* / *insert_failed* /
 *max_iteration_exceeded* / *invalid_workspace_path* result the
 workflow logs and exits cleanly - never raises so a stray
 ``[iterate]`` cannot crash the worker.
3. On success, dispatch:class:`AutomationWorkflow` as a child with
 the iteration metadata (``iteration=N+1``,
 ``trigger_event="jira:iterate"``) so the rest of the gateway
 pipeline (LLM analysis, capability gate, branch-pattern rules,
 workflow_type routing) runs identically to a fresh start. /
 (carry-over PR / branch) are honoured by the LLM context
 reading the ``previous_*`` fields the activity returned..
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Activity / shared-helper imports inside the Temporal sandbox escape hatch.
#
# Mirrors ``automation_workflow.py`` -:func:`prepare_iteration` lives
# in an activities module that imports asyncpg, regex helpers, etc.
# ``imports_passed_through`` is the Temporal-blessed way to make these
# safe for sandbox replay.
# ---------------------------------------------------------------------------

with workflow.unsafe.imports_passed_through():
    from automation_worker.activities.iteration_manager import (
        IterationContext,
        MAX_ITERATIONS_PER_ISSUE,
        PrepareIterationInput,
    )
    from temporal_shared.messages import (
        AutomationWorkflowInput,
        AutomationWorkflowOutput,
    )
    from temporal_shared.workflow_registry import task_queue_for


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Activity name strings - referenced via ``workflow.execute_activity``
#: so the workflow module stays decoupled from the concrete activity
#: implementation.
_ACT_PREPARE_ITERATION: Final[str] = "prepare_iteration"

#: Activity name for posting Jira comments - used by the
#: storm-guard branch so an issue that hits the per-issue iteration
#: cap gets a user-visible heads-up.
_ACT_JIRA_ADD_COMMENT: Final[str] = "jira_add_comment"

#: Activity name for writing audit log entries - used by the
#: storm-guard branch so the operator has an authoritative record of
#: every refused ``[iterate]`` even if Jira is unavailable.
_ACT_AUDIT_WRITE: Final[str] = "audit_write"

#: Default activity timeout for short, best-effort side effects
#: (Jira comment, audit write). Two minutes mirrors the value used by
#: every other workflow in this worker.
_SHORT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: ``prepare_iteration`` is dominated by a couple of Postgres round
#: trips; two minutes is generous enough to absorb transient blips
#: without holding a worker slot indefinitely.
_PREPARE_ITERATION_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: Retry policy for ``prepare_iteration``. Three attempts is enough
#: for transient DB hiccups; persistent failures (UNIQUE-constraint
#: races, missing migration) are surfaced through the
#::class:`IterationContext.reason` field rather than retried forever.
_PREPARE_ITERATION_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Workflow input
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IterationWorkflowInput:
    """Input shape consumed by:class:`IterationWorkflow`.

 The webhook dispatcher constructs this envelope from the
 ``[iterate]`` payload. Fields mirror the dict the dispatcher
 historically passed (see:meth:`WebhookDispatcher._start_iteration`) so the wire shape
 stays backwards compatible - the only change in is that
 the worker now has a real workflow class on the receiving end
 instead of a placeholder.

 Attributes
 ----------
 trigger:
 Always ``"iterate"``. Carried through for symmetry with:class:`AutomationWorkflowInput.trigger_event` and so audit
 rows can correlate the entry point.
 issue_key:
 Jira issue key the ``[iterate]`` comment was posted on
 (e.g. ``"PAY-4211"``).
 department_id:
 Department slug resolved by the dispatcher from the assignee
 ``account_id``.
 extra_instructions:
 Free-form text the user supplied after ``[iterate]``.
 ``None`` when the comment body was just the keyword.
 comment_body:
 Full original comment body - kept verbatim so
 ``prepare_iteration`` can audit the source text and re-extract
 the instructions if needed (defence in depth against the
 dispatcher and the activity drifting on the parsing rule).
 actor_account_id:
 Atlassian ``accountId`` of the comment author. Re-checked by
 the activity for defence-in-depth on.
 issue_reporter_account_id:
 ``accountId`` of the issue reporter, when known. Forwarded so
 the activity's authorization gate can match the dispatcher's
 decision byte for byte.
 dept_config:
 Department configuration mapping. The activity only reads the
 ``approvers`` key today; passing the whole dict keeps the
 contract flexible for future authorization rules.
 available_capabilities / available_repos / available_spaces:
 Capability envelope passed straight through to the child:class:`AutomationWorkflow`. The dispatcher fills these from
 the dept config so the iteration path runs the same capability
 gate as a fresh assignment (,). All default to empty
 tuples so older callers that have not yet been updated still
 produce a valid input.
 default_language:
 ISO-639-1 code forwarded to:class:`AutomationWorkflow`.
 trace_id:
 Trace id propagated from the inbound webhook for log
 correlation. Empty string when the webhook layer did not
 supply one.
 """

    trigger: str
    issue_key: str
    department_id: str
    extra_instructions: str | None = None
    comment_body: str | None = None
    actor_account_id: str | None = None
    issue_reporter_account_id: str | None = None
    dept_config: dict[str, Any] | None = None
    available_capabilities: tuple[str, ...] = ()
    available_repos: tuple[str, ...] = ()
    available_spaces: tuple[str, ...] = ()
    default_language: str = "tr"
    trace_id: str = ""


# ---------------------------------------------------------------------------
# Workflow output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IterationWorkflowOutput:
    """Result of:class:`IterationWorkflow.run`.

 Attributes
 ----------
 decision:
 One of ``"dispatched"``, ``"unauthorized"``, ``"failed"``.
 ``"dispatched"`` means the child:class:`AutomationWorkflow`
 was started; the inner outcome surfaces on:attr:`automation_output`. ``"unauthorized"`` means:func:`prepare_iteration` rejected the request (/
 max-iteration / invalid path / DB insert race).
 ``"failed"`` means an unexpected exception was raised - the
 workflow logs the error and surfaces a clean envelope rather
 than letting Temporal retry forever.
 iteration_number:
 ``N+1`` when authorized; ``0`` otherwise.
 workspace_path:
 Canonical workspace path for iter-(N+1); empty when not
 authorized.
 reason:
 Stable reason code from:class:`IterationContext.reason` when
 ``decision != "dispatched"``; empty otherwise.
 child_workflow_id:
 Temporal workflow id of the dispatched:class:`AutomationWorkflow`; ``None`` when no child ran.
 automation_output:
 The child:class:`AutomationWorkflowOutput` when the dispatch
 completed; ``None`` when not authorized or the dispatch failed.
 """

    decision: str
    iteration_number: int = 0
    workspace_path: str = ""
    reason: str = ""
    child_workflow_id: str | None = None
    automation_output: AutomationWorkflowOutput | None = None


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn(name="IterationWorkflow")
class IterationWorkflow:
    """Thin entry point for ``[iterate]``-triggered re-runs.

 The class takes no constructor arguments (Temporal calls
 ``__init__`` with no positional args). State is built up
 through:meth:`run` from the:class:`IterationWorkflowInput`
 envelope.

 Lifecycle:

 1. Coerce the dispatcher's payload (which may arrive as either a
 dataclass or a plain dict - the dispatcher historically passed
 a dict, see:meth:`WebhookDispatcher._start_iteration`) into:class:`IterationWorkflowInput`.
 2. Run:func:`prepare_iteration` - see module docstring. On any
 non-authorized result the workflow exits with the carrying
 reason code so the dispatcher's audit row remains the only
 record of the rejection (no double-audit at this layer).
 3. Dispatch:class:`AutomationWorkflow` as a child with
 ``iteration=N+1`` and ``trigger_event="jira:iterate"`` so the
 rest of the pipeline runs identically to a fresh start. The
 child's outcome is awaited and surfaced through:attr:`IterationWorkflowOutput.automation_output`.
 """

    @workflow.run
    async def run(
        self, inp: IterationWorkflowInput | dict[str, Any]
    ) -> IterationWorkflowOutput:
        # 1. Coerce dict  dataclass. The dispatcher passes a plain
        # dict via ``args=[workflow_input]`` (see
        # WebhookDispatcher._start_iteration). Temporal's data
        # converter will hand us either shape depending on how the
        # caller registered the workflow input type - be liberal in
        # what we accept.
        coerced = _coerce_input(inp)

        # 2. Run prepare_iteration first (entry point).
        prep_input = PrepareIterationInput(
            issue_key=coerced.issue_key,
            comment_body=coerced.comment_body or "",
            comment_author_account_id=coerced.actor_account_id or "",
            issue_reporter_account_id=coerced.issue_reporter_account_id,
            dept_id=coerced.department_id,
            dept_config=coerced.dept_config or {},
            trace_id=coerced.trace_id,
        )

        try:
            context: IterationContext = await workflow.execute_activity(
                _ACT_PREPARE_ITERATION,
                args=[prep_input],
                result_type=IterationContext,
                start_to_close_timeout=_PREPARE_ITERATION_TIMEOUT,
                retry_policy=_PREPARE_ITERATION_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            # The activity itself swallows expected denial paths
            # and returns ``authorized=False`` - anything that
            # propagates here is a genuine fault (DB outage, etc.).
            # Surface a clean ``failed`` envelope rather than letting
            # the workflow loop on retry forever.
            workflow.logger.warning(
                "IterationWorkflow: prepare_iteration raised for %s: %s",
                coerced.issue_key,
                exc,
            )
            return IterationWorkflowOutput(
                decision="failed",
                reason="prepare_iteration_exception",
            )

        if not context.authorized:
            # Storm-guard: a ``max_iteration_exceeded`` rejection is
            # special - the dispatcher already authorized the
            # ``[iterate]`` (the comment author was an approver) but
            # the activity refused to allocate an N+1 row because the
            # per-issue cap was hit. We surface a user-visible Jira
            # comment + an audit row so the operator can see the
            # storm without rummaging through worker logs. Every
            # other denial path (``not_authorized``,
            # ``insert_failed``, ``invalid_workspace_path``) is left
            # to the dispatcher's existing audit row - no double-
            # audit at this layer.
            if context.reason == "max_iteration_exceeded":
                await self._handle_max_iteration_exceeded(
                    issue_key=coerced.issue_key,
                    department_id=coerced.department_id,
                    current_count=context.current_count,
                    trace_id=context.trace_id,
                )

            workflow.logger.info(
                "IterationWorkflow: prepare_iteration declined for %s "
                "(reason=%s)",
                coerced.issue_key,
                context.reason,
            )
            return IterationWorkflowOutput(
                decision="unauthorized",
                iteration_number=context.iteration_number,
                workspace_path=context.workspace_path,
                reason=context.reason,
            )

        # 3. Dispatch AutomationWorkflow as a child. The child runs
        # the full gateway pipeline (LLM analysis, capability gate,
        # branch-pattern rules, child-workflow routing) so the
        # iteration path is byte-identical to a fresh start except
        # for the ``iteration`` counter and the
        # ``trigger_event="jira:iterate"`` discriminator that audit
        # rows can correlate against.
        child_input = AutomationWorkflowInput(
            issue_key=coerced.issue_key,
            department_id=coerced.department_id,
            available_capabilities=coerced.available_capabilities,
            available_repos=coerced.available_repos,
            available_spaces=coerced.available_spaces,
            default_language=coerced.default_language,
            trigger_event="jira:iterate",
            iteration=context.iteration_number,
            raw_event=None,
            trace_id=context.trace_id,
        )

        # Use the workflow id the activity already persisted so the
        # ``shared.workflow_iterations`` row and the Temporal record
        # stay aligned. The activity returned a deterministic id of
        # the form ``iteration-{ISSUE_KEY}-{N}-{shortuuid}`` - replays
        # see the same value because Temporal stores activity results
        # in the workflow history.
        child_workflow_id = context.workflow_id

        try:
            child_handle = await workflow.start_child_workflow(
                "AutomationWorkflow",
                args=[child_input],
                id=child_workflow_id,
                task_queue=task_queue_for("AutomationWorkflow"),
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "IterationWorkflow: AutomationWorkflow start failed "
                "for %s iter-%d: %s",
                coerced.issue_key,
                context.iteration_number,
                exc,
            )
            return IterationWorkflowOutput(
                decision="failed",
                iteration_number=context.iteration_number,
                workspace_path=context.workspace_path,
                reason="child_dispatch_failed",
            )

        try:
            automation_output: AutomationWorkflowOutput = await child_handle
        except Exception as exc:  # noqa: BLE001
            # The child's failure is recorded via the gateway's own
            # audit pipeline; we surface a typed envelope so callers
            # observing this workflow's result can still reason about
            # the outcome.
            workflow.logger.warning(
                "IterationWorkflow: AutomationWorkflow child failed "
                "for %s iter-%d: %s",
                coerced.issue_key,
                context.iteration_number,
                exc,
            )
            return IterationWorkflowOutput(
                decision="failed",
                iteration_number=context.iteration_number,
                workspace_path=context.workspace_path,
                reason="child_workflow_failed",
                child_workflow_id=child_workflow_id,
            )

        return IterationWorkflowOutput(
            decision="dispatched",
            iteration_number=context.iteration_number,
            workspace_path=context.workspace_path,
            reason="",
            child_workflow_id=child_workflow_id,
            automation_output=automation_output,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _handle_max_iteration_exceeded(
        self,
        *,
        issue_key: str,
        department_id: str,
        current_count: int | None,
        trace_id: str,
    ) -> None:
        """Surface a storm-guard rejection to Jira + the audit log.

 Called from:meth:`run` when:func:`prepare_iteration` returns
 ``authorized=False`` with ``reason="max_iteration_exceeded"``.
 Both side effects are best-effort: a failed audit write does
 not silence the Jira comment, and a failed Jira comment does
 not silence the audit. The workflow continues to return its
 ``unauthorized`` envelope regardless.

 The Jira body is intentionally Turkish-prose (matching the
 rest of the worker's user-facing copy) and includes the
 cap + current count so the operator can see *why* the
 request was refused without opening the worker logs.
 """

        cap = MAX_ITERATIONS_PER_ISSUE
        # ``current_count`` is ``None`` only when the activity raised
        # before computing the count - fall back to the cap so the
        # message stays grammatical.
        count = current_count if current_count is not None else cap

        # Audit first so the operator has an authoritative record
        # even if Jira is unavailable.
        try:
            await workflow.execute_activity(
                _ACT_AUDIT_WRITE,
                args=[
                    {
                        "action": "iteration_max_exceeded",
                        "actor_role": "system",
                        "department_id": department_id,
                        "issue_key": issue_key,
                        "trace_id": trace_id,
                        "payload": {
                            "issue_key": issue_key,
                            "current_count": count,
                            "cap": cap,
                        },
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_PREPARE_ITERATION_RETRY,
            )
        except Exception:  # noqa: BLE001 - audit best-effort
            workflow.logger.warning(
                "IterationWorkflow: audit_write(iteration_max_exceeded) "
                "failed for %s - continuing",
                issue_key,
            )

        body = (
            f" Bu görev için maksimum {cap} iterasyon limitine "
            f"ulaşıldı (mevcut: {count}). Devam etmek için yeni bir "
            f"Jira görevi açın."
        )
        try:
            await workflow.execute_activity(
                _ACT_JIRA_ADD_COMMENT,
                args=[issue_key, body, department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_PREPARE_ITERATION_RETRY,
            )
        except Exception:  # noqa: BLE001 - comment best-effort
            workflow.logger.warning(
                "IterationWorkflow: jira_add_comment(max_exceeded) "
                "failed for %s - continuing",
                issue_key,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_input(
    inp: IterationWorkflowInput | dict[str, Any] | Any,
) -> IterationWorkflowInput:
    """Normalise the dispatcher payload into:class:`IterationWorkflowInput`.

 The webhook dispatcher passes a plain ``dict`` to:meth:`temporalio.client.Client.start_workflow` - see:meth:`WebhookDispatcher._start_iteration`. The Temporal data
 converter delivers it to the workflow as either:

 * a:class:`dict` (the default JSON converter when the workflow
 input has no registered class), or
 * an:class:`IterationWorkflowInput` (when a future caller / test
 passes a pre-built instance).

 This helper flattens both shapes into the dataclass so the rest
 of:meth:`IterationWorkflow.run` can rely on attribute access.
 Keys that are missing from the dict default to safe values
 (empty tuples / strings / ``None``) - the activity body re-checks
 authorization, so a malformed input cannot bypass the gate.
 """

    if isinstance(inp, IterationWorkflowInput):
        return inp

    # Anything dict-like (regular ``dict``, ``MappingProxyType``,...)
    # is unpacked field-by-field. Tuple coercion guards against
    # mutable-list inputs leaking into the frozen dataclass - the
    # input is then trivially replayable.
    if hasattr(inp, "get"):

        def _get(key: str, default: Any = None) -> Any:
            return inp.get(key, default)  # type: ignore[union-attr]

        def _tuple(key: str) -> tuple[str, ...]:
            value = _get(key, ())
            if value is None:
                return ()
            try:
                return tuple(str(v) for v in value)
            except TypeError:
                return ()

        return IterationWorkflowInput(
            trigger=str(_get("trigger", "iterate")),
            issue_key=str(_get("issue_key", "")),
            department_id=str(_get("department_id", "")),
            extra_instructions=_get("extra_instructions"),
            comment_body=_get("comment_body"),
            actor_account_id=_get("actor_account_id"),
            issue_reporter_account_id=_get("issue_reporter_account_id"),
            dept_config=_get("dept_config") or {},
            available_capabilities=_tuple("available_capabilities"),
            available_repos=_tuple("available_repos"),
            available_spaces=_tuple("available_spaces"),
            default_language=str(_get("default_language", "tr")),
            trace_id=str(_get("trace_id", "")),
        )

    # Last-resort fallback: build a minimal envelope from attribute
    # access. This branch exists so a future Temporal-side data
    # converter that delivers a typed-but-not-frozen object still
    # works - production hits the dataclass / dict branches above.
    return IterationWorkflowInput(
        trigger=str(getattr(inp, "trigger", "iterate")),
        issue_key=str(getattr(inp, "issue_key", "")),
        department_id=str(getattr(inp, "department_id", "")),
        extra_instructions=getattr(inp, "extra_instructions", None),
        comment_body=getattr(inp, "comment_body", None),
        actor_account_id=getattr(inp, "actor_account_id", None),
        issue_reporter_account_id=getattr(
            inp, "issue_reporter_account_id", None
        ),
        dept_config=getattr(inp, "dept_config", None) or {},
        available_capabilities=tuple(
            getattr(inp, "available_capabilities", ()) or ()
        ),
        available_repos=tuple(getattr(inp, "available_repos", ()) or ()),
        available_spaces=tuple(getattr(inp, "available_spaces", ()) or ()),
        default_language=str(getattr(inp, "default_language", "tr")),
        trace_id=str(getattr(inp, "trace_id", "")),
    )


__all__: tuple[str, ...] = (
    "IterationWorkflow",
    "IterationWorkflowInput",
    "IterationWorkflowOutput",
)
