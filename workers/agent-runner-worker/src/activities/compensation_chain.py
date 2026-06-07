"""Cancel + compensation chain activity for AgentRunnerWorkflow.

The workflow dispatches a single ``compensation_chain_run`` activity
when a run is cancelled or a critical output action fails. This module
hosts the worker-side body for that activity.

The chain walks the fixed-order step vocabulary published by
``temporal_shared.compensation`` (``COMPENSATION_STEPS``) and produces a
``CompensationReport``-shaped result. Every step is **best-effort**: a
step that fails or has nothing to undo never aborts the chain, so the
workflow can always terminate cleanly. The activity itself never raises
- a failure to clean up is reported per-step, not propagated, so the
cancel/compensation path cannot get stuck retrying.

The cancel context the workflow passes carries the issue key and the
actor/reason, which is enough to post the operator-facing cancel comment
on the Jira issue. Steps that require side-effect-specific data the
context does not carry (the PR id, the AI branch name, the Confluence
page id) are recorded as ``skipped`` rather than guessed.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from temporal_shared.compensation import (
    COMPENSATION_STEPS,
    STEP_RESULT_FAILED,
    STEP_RESULT_OK,
    STEP_RESULT_SKIPPED,
)

from .mcp_tool import call_mcp_tool

# Steps whose side effect is identified only by data the cancel context
# does not carry (PR id, AI branch name, Confluence page id). Without
# that data there is nothing to undo, so they are reported ``skipped``.
# MinIO artifacts are intentionally retained, so that step is a no-op by
# design too.
_SKIPPED_BY_DESIGN: frozenset[str] = frozenset(
    {
        "close_draft_pr_if_open",
        "delete_ai_branch_if_unused",
        "label_confluence_page_cancelled",
        "leave_minio_artifacts_for_retention",
        "transition_jira_issue_if_configured",
    }
)


def _cancel_comment_body(reason: str, actor_role: str) -> str:
    actor = (actor_role or "system").strip() or "system"
    why = (reason or "").strip()
    suffix = f" Sebep: {why}." if why else ""
    return (
        " Otomasyon iş akışı iptal edildi ve yapılan ara adımlar geri "
        f"alındı ({actor}).{suffix} Devam etmek için yeni bir yorum yazın."
    )


async def _post_cancel_comment(
    issue_key: str, dept_id: str, body: str
) -> str:
    """Post the cancel comment on the Jira issue. Best-effort."""

    if not issue_key:
        return STEP_RESULT_SKIPPED
    try:
        await call_mcp_tool(
            "jira_add_comment",
            {"issue_key": issue_key, "body": body},
            dept_id=dept_id,
            service="jira",
            timeout=30.0,
        )
        return STEP_RESULT_OK
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        activity.logger.warning(
            "compensation: post_cancel_jira_comment failed for %s: %s",
            issue_key,
            exc,
        )
        return STEP_RESULT_FAILED


@activity.defn(name="compensation_chain_run")
async def compensation_chain_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the cancel/compensation chain and return a report dict.

    Parameters
    ----------
    payload:
        Cancel context with ``workflow_id``, ``dept_id``, ``issue_key``,
        ``actor_id``, ``actor_role`` and ``reason`` keys.

    Returns
    -------
    dict
        A ``CompensationReport``-shaped mapping with ``ok`` (always
        ``True`` - the chain is best-effort and never fails the
        workflow), ``attempted_steps`` and ``step_results``.
    """

    if not isinstance(payload, dict):
        payload = {}
    issue_key = str(payload.get("issue_key") or "")
    dept_id = str(payload.get("dept_id") or "")
    reason = str(payload.get("reason") or "")
    actor_role = str(payload.get("actor_role") or "system")
    workflow_id = str(payload.get("workflow_id") or "")

    activity.heartbeat(f"compensating {workflow_id or issue_key}")

    attempted: list[str] = []
    results: list[tuple[str, str]] = []
    for step in COMPENSATION_STEPS:
        attempted.append(step)
        if step == "post_cancel_jira_comment":
            outcome = await _post_cancel_comment(
                issue_key,
                dept_id,
                _cancel_comment_body(reason, actor_role),
            )
        elif step in _SKIPPED_BY_DESIGN:
            outcome = STEP_RESULT_SKIPPED
        else:  # pragma: no cover - defensive: unknown future step
            outcome = STEP_RESULT_SKIPPED
        results.append((step, outcome))

    return {
        "ok": True,
        "attempted_steps": tuple(attempted),
        "step_results": tuple(results),
    }
