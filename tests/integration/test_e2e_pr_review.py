"""End-to-end integration test for the ``pr_review`` workflow.

Scenario
--------

The :class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
``pr_review`` body is the canonical happy-path flow for LLM-driven PR
review. The handler walks through three side-effecting steps before
exiting:

 1. ``bitbucket_fetch_pr_diff`` — pull the diff for the PR id
 extracted from ``analysis.rationale``. Until that id is carried directly,
 :meth:`AgentRunnerWorkflow._extract_pr_id` parses digits out of
 ``rationale``.
 2. ``llm_review_code`` — run the ``pr_review.md`` template through
 the token-capped LLM helper. Returns a ``{"findings":
 [...]}`` envelope.
 3. ``bitbucket_add_pr_comment`` — post each finding whose ``hash``
 was not seen on a previous iteration. The
 workflow's ``_previous_findings`` set tracks the hashes posted
 across calls so a second iteration does not re-post the same
 finding verbatim.

This file pins the activity-call sequence end-to-end against the
real Temporal time-skipping ``WorkflowEnvironment`` so signal
dispatch, sandbox enforcement, and replay determinism all
participate. Two scenarios are covered:

* ``test_pr_review_happy_path_posts_findings`` — the LLM returns two
 fresh findings (``h1`` / ``h2``); both are posted and the
 ``get_previous_findings`` query returns the sorted tuple
 ``("h1", "h2")``.

* ``test_pr_review_dedup_within_single_run`` — the LLM returns three
 findings with ``h1`` duplicated. After the body finishes, the
 ``_previous_findings`` set has collapsed the duplicates into two
 distinct hashes (``{"h1", "h3"}``) — the canonical
 ``_dedup_findings`` dedup contract is consulted, even when the
 dedup happens at the *post* boundary rather than between iterations
 (the workflow hashes a finding into ``_previous_findings`` only
 *after* the comment activity returned, so within a single run two
 occurrences of the same hash may both flush through to the comment
 activity — but the query surface that downstream consumers rely on
 carries the deduped set).

Activity stubs are registered through ``@activity.defn(name=...)``
decorators with names matching the production lookups, so the
workflow body finds them via the worker's registry exactly as it
would in production.

Hosts without the embedded ``temporal-test-server`` skip cleanly via
the same module-level gate the existing integration tests in
``test_temporal_signal.py`` / ``test_temporal_loop_cap.py`` use — see
:func:`_temporal_test_env_available` and
:func:`_start_time_skipping_or_skip`.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap — agent-runner-worker tree, temporal-shared, mcp_client.
#
# Mirrors the bootstrap used by ``test_e2e_code_change_with_test.py`` and
# ``test_e2e_confluence_doc_create.py``. The agent-runner-worker ships
# its source under ``platform/workers/agent-runner-worker/src/`` and
# its modules import each other under the ``agent_runner`` namespace;
# the ``temporal-shared`` and ``mcp_client`` libraries follow the
# same layout. Adding their ``src/`` directories onto ``sys.path``
# makes the imports below resolve without first installing the
# packages.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (
    _AGENT_RUNNER_SRC,
    _TEMPORAL_SHARED_SRC,
    _MCP_CLIENT_SRC,
):
    _candidate_str = str(_candidate)
    if _candidate.is_dir() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)


# ---------------------------------------------------------------------------
# Module-level skip gate
# ---------------------------------------------------------------------------


def _temporal_test_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly.

 Any import failure is treated as "skip cleanly" so hosts without
 the embedded ``temporal-test-server`` (sandboxed CI, missing
 native deps) skip rather than erroring at collection time.
 """

    try:
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure → skip.
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _temporal_test_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the time-skipping env, ``pytest.skip``ing on failure.

 The embedded ``temporal-test-server`` may fail to start on hosts
 where the binary is not bundled. Surface that cleanly as a skip
 so the integration suite stays green on machines that cannot host
 Temporal locally.
 """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Activity call log
#
# Append-only log shared by every activity stub below. Each entry is
# ``(name, args, kwargs)`` so tests can assert on order, count and
# payload at the same time.
# ---------------------------------------------------------------------------


@dataclass
class ActivityCallLog:
    """Append-only log of activity invocations recorded by the stubs."""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )

    def record(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _, _ in self.calls if n == name)

    def args_for(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args, _ in self.calls if n == name]


# ---------------------------------------------------------------------------
# Activity stub factory — every activity the ``pr_review`` handler
# invokes is registered with a thin recording stub. The workflow walks
# through this activity set:
#
# * ``bitbucket_fetch_pr_diff`` — return a stub diff envelope.
# * ``llm_review_code`` — return ``{"findings": [...]}`` with the
# findings the test wants to drive through the dedup logic.
# * ``bitbucket_add_pr_comment`` — best-effort PR comment poster.
# * ``audit_emit`` — defensive (best-effort audit hooks may fire).
#
# The ``pr_review`` body does not invoke ``set_assignee_to_bot``,
# ``precommit_scanner``, ``bitbucket_create_commit`` or any of the
# Confluence activities; we deliberately do NOT register stubs for
# them so a regression that wires those in shows up as a clean
# "activity not found" error instead of being swallowed by a
# permissive catch-all.
# ---------------------------------------------------------------------------


def _build_pr_review_activities(
    log: ActivityCallLog,
    *,
    findings: list[dict[str, Any]],
    diff_content: str = "diff --git a/x.py b/x.py\n+def foo: pass",
) -> list[Any]:
    """Build the bag of stub activities the workflow body invokes.

 Parameters
 ----------
 log:
 Shared :class:`ActivityCallLog` — every stub appends its
 invocation here so the tests can assert on order, count, and
 payload.
 findings:
 Sequence of dicts the ``llm_review_code`` stub returns under
 the ``"findings"`` key. Each dict needs at minimum a stable
 ``"hash"`` (the dedup key) and a ``"body"`` (the comment
 text).
 diff_content:
 Stub diff text returned by ``bitbucket_fetch_pr_diff`` under
 the ``"diff_content"`` key — matches the contract the
 workflow's :meth:`AgentRunnerWorkflow._extract_diff_text`
 helper expects.
 """

    from temporalio import activity

    @activity.defn(name="bitbucket_fetch_pr_diff")
    async def _bitbucket_fetch_pr_diff(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("bitbucket_fetch_pr_diff", args, kwargs)
        return {"diff_content": diff_content}

    @activity.defn(name="llm_review_code")
    async def _llm_review_code(*args: Any, **kwargs: Any) -> dict[str, Any]:
        log.record("llm_review_code", args, kwargs)
        # Deep-copy each finding dict so a test that mutates the
        # returned envelope cannot accidentally corrupt the next
        # call. The findings are flat str-keyed dicts so a shallow
        # copy is sufficient.
        return {"findings": [dict(f) for f in findings]}

    @activity.defn(name="bitbucket_add_pr_comment")
    async def _bitbucket_add_pr_comment(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("bitbucket_add_pr_comment", args, kwargs)
        return {"ok": True}

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        # ``audit_emit`` is invoked by the workflow body's
        # best-effort audit hooks (e.g. iter==3 banner). The
        # ``pr_review`` flow at iteration=1 does not trigger any
        # audit; the stub is registered defensively so the worker
        # boot does not error if the body gains an audit line in a
        # future revision.
        log.record("audit_emit", (payload,), {})
        return None

    return [
        _bitbucket_fetch_pr_diff,
        _llm_review_code,
        _bitbucket_add_pr_comment,
        _audit_emit,
    ]


# ---------------------------------------------------------------------------
# Input fixture
# ---------------------------------------------------------------------------


def _make_pr_review_input(
    *,
    issue_key: str,
    pr_id: int,
    target_repo: str = "payment-service",
    target_branch: str = "develop",
    iteration: int = 1,
) -> Any:
    """Build a :class:`AgentRunnerWorkflowInput` for the PR-review path.

 The PR id rides on ``analysis.rationale`` because the workflow has
 not yet promoted it to a first-class field on
 :class:`AgentRunnerWorkflowInput`; the workflow's
 :meth:`AgentRunnerWorkflow._extract_pr_id` helper parses the
 digits out of ``rationale`` and falls back to 0 when no digits
 are present. We pass the integer verbatim so the resulting
 activity payloads carry a deterministic id.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type="pr_review",
        confidence="high",
        target_repo=target_repo,
        target_branch=target_branch,
        title=f"Review PR {pr_id}",
        # ``_extract_pr_id`` strips digits out of ``rationale`` and
        # uses them verbatim. Passing the integer as a string keeps
        # the helper stable even if the LLM eventually surrounds the
        # id with prose.
        rationale=str(pr_id),
        output_actions=(),
        token_usage=128,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type="pr_review",
        analysis=analysis,
        target_repo=target_repo,
        target_branch=target_branch,
        iteration=iteration,
        max_iter=5,
        default_language="tr",
    )


# ---------------------------------------------------------------------------
# Result coercion helper
# ---------------------------------------------------------------------------


def _output_to_dict(result: Any) -> dict[str, Any]:
    """Coerce the workflow result to a dict for assertion ergonomics.

 ``AgentRunnerWorkflowOutput`` is a frozen dataclass; depending on
 the SDK's data converter the result either round-trips back into
 the dataclass or surfaces as a plain dict. We normalise both
 shapes to a single mapping so the assertions stay robust across
 SDK versions.
 """

    fields = (
        "status",
        "iter_count",
        "summary",
        "failure_reason",
        "partial_failure_actions",
        "branch",
        "pr_id",
    )
    if hasattr(result, "__dataclass_fields__"):
        return {name: getattr(result, name, None) for name in fields}
    if isinstance(result, dict):
        return {name: result.get(name) for name in fields}
    pytest.fail(
        f"unexpected workflow result shape: {type(result).__name__}"
    )
    return {}  # pragma: no cover - pytest.fail terminates the test


# ---------------------------------------------------------------------------
# Test 1 — happy path: posts every fresh finding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pr_review_happy_path_posts_findings() -> None:
    """Drive :class:`AgentRunnerWorkflow` through the full ``pr_review``
 happy path:

 1. ``bitbucket_fetch_pr_diff`` returns a stub diff.
 2. ``llm_review_code`` returns two findings (``h1`` and ``h2``)
 — neither has been seen before, so both must flush through to
 the comment activity.
 3. ``bitbucket_add_pr_comment`` succeeds twice (once per
 finding).

 Assertions
 ----------
 * ``bitbucket_fetch_pr_diff`` is invoked exactly once with the
 target repo + integer PR id pair extracted from
 ``analysis.rationale``.
 * ``llm_review_code`` is invoked exactly once with the diff
 envelope and the ``"pr_review.md"`` template name.
 * ``bitbucket_add_pr_comment`` is invoked exactly twice — once
 per finding — and both finding bodies surface verbatim in the
 activity payload.
 * The :meth:`AgentRunnerWorkflow.get_previous_findings` query
 returns the sorted tuple ``("h1", "h2")`` — confirms both
 hashes were latched into ``_previous_findings`` so the next
 iteration would dedup them.
 * Final status is ``"completed"`` and ``failure_reason`` is
 ``None``.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    findings = [
        {"hash": "h1", "body": "missing docstring"},
        {"hash": "h2", "body": "no type hints"},
    ]
    activities = _build_pr_review_activities(log, findings=findings)

    workflow_id = "agent-runner-jira-PAY-9601-happy"
    parent_task_queue = "agent-runner-pr-review-tq-happy"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=parent_task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_pr_review_input(
                issue_key="PAY-9601", pr_id=42, iteration=1
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=parent_task_queue,
            )
            result_raw: Any = await handle.result()
            previous_findings = await handle.query("get_previous_findings")

    result = _output_to_dict(result_raw)

    # ----- Activity cardinality ------------------------------

    assert log.count("bitbucket_fetch_pr_diff") == 1, (
        f"bitbucket_fetch_pr_diff must run exactly once on the happy "
        f"path; got {log.count('bitbucket_fetch_pr_diff')} "
        f"(call log: {log.names()!r})"
    )
    assert log.count("llm_review_code") == 1, (
        f"llm_review_code must run exactly once on the happy path; "
        f"got {log.count('llm_review_code')} "
        f"(call log: {log.names()!r})"
    )
    assert log.count("bitbucket_add_pr_comment") == 2, (
        f"bitbucket_add_pr_comment must run exactly twice — once per "
        f"finding; got {log.count('bitbucket_add_pr_comment')} "
        f"(call log: {log.names()!r})"
    )

    # ----- Activity ordering ----------------------------------------
    #
    # The diff fetch must precede the LLM call, which must precede
    # the comment posts. A regression that re-orders the body would
    # post empty comments before the LLM ran.
    relevant = [
        name
        for name in log.names()
        if name in {
            "bitbucket_fetch_pr_diff",
            "llm_review_code",
            "bitbucket_add_pr_comment",
        }
    ]
    assert relevant == [
        "bitbucket_fetch_pr_diff",
        "llm_review_code",
        "bitbucket_add_pr_comment",
        "bitbucket_add_pr_comment",
    ], (
        f"unexpected pr_review activity sequence: {relevant!r}; "
        f"full call log: {log.names()!r}"
    )

    # ----- Diff fetch payload ---------------------------------------
    #
    # The handler passes ``({"workspace": "", "repo_slug": ...}, pr_id,
    # dept_id)``. We assert the integer PR id was extracted out of
    # ``analysis.rationale`` correctly ( fallback path until task
    # 10.1 lifts pr_id to a first-class field).
    fetch_args_list = log.args_for("bitbucket_fetch_pr_diff")
    assert len(fetch_args_list) == 1
    fetch_args = fetch_args_list[0]
    assert fetch_args[1] == 42, (
        f"PR id must be extracted out of analysis.rationale; got "
        f"{fetch_args[1]!r} from args {fetch_args!r}"
    )
    repo_descriptor = fetch_args[0]
    assert isinstance(repo_descriptor, dict)
    assert repo_descriptor.get("repo_slug") == "payment-service"
    assert fetch_args[2] == "payments"

    # ----- LLM call payload -----------------------------------------

    llm_args_list = log.args_for("llm_review_code")
    assert len(llm_args_list) == 1
    llm_args = llm_args_list[0]
    # Second positional argument is the prompt template name — the
    # body invokes ``self._execute_llm_activity("llm_review_code",
    # args=[diff, "pr_review.md"], ...)``.
    assert llm_args[1] == "pr_review.md", (
        f"LLM call must use the pr_review.md template; got "
        f"{llm_args[1]!r}"
    )

    # ----- Comment payloads carry the finding bodies ----------------

    comment_args_list = log.args_for("bitbucket_add_pr_comment")
    posted_bodies = [args[2] for args in comment_args_list]
    assert "missing docstring" in posted_bodies, (
        f"first finding body must be posted verbatim; got "
        f"{posted_bodies!r}"
    )
    assert "no type hints" in posted_bodies, (
        f"second finding body must be posted verbatim; got "
        f"{posted_bodies!r}"
    )
    # Each comment activity carries the same target repo + PR id pair
    # the diff fetch used.
    for args in comment_args_list:
        assert args[1] == 42
        assert args[3] == "payments"

    # ----- get_previous_findings query -----------------
    #
    # The query is declared as returning ``tuple[str, ...]`` — but
    # the Temporal JSON data converter renders the value as a list
    # on the wire so the return type at the call site is normalised
    # to ``list``. We coerce to a tuple before the equality check
    # so the assertion is shape-stable across SDK versions.
    assert tuple(previous_findings) == ("h1", "h2"), (
        f"get_previous_findings must return the sorted tuple of "
        f"every posted finding hash; got {previous_findings!r}"
    )

    # ----- Workflow output ------------------------------------------

    assert result["status"] == "completed", (
        f"expected status=completed on the happy path, got {result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None on the happy path, got {result!r}"
    )
    assert not result["partial_failure_actions"], (
        f"no partial failures expected on the happy path; got "
        f"{result['partial_failure_actions']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — dedup within a single run: duplicate hashes collapse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pr_review_dedup_within_single_run() -> None:
    """Drive the ``pr_review`` body with three findings whose hashes are
 ``["h1", "h1", "h3"]`` — ``h1`` is duplicated.

 The production ``_dedup_findings`` placeholder filters against the
 *previously seen* hash set. On a fresh run the set starts empty,
 so the helper passes every finding through; the workflow then
 posts each finding and adds the hash to ``_previous_findings``
 only *after* the comment activity returns. That means within a
 single run two occurrences of the same hash may both flush
 through to the comment activity (the second occurrence is checked
 against the set *before* the first occurrence's hash has been
 written — the body does the post-then-add ordering, not
 add-then-post).

 The dedup contract that matters at the *query* layer is therefore
 the **set** of hashes the body has latched, not the comment
 activity count: the
 :meth:`AgentRunnerWorkflow.get_previous_findings` query must
 return the deduped set ``{"h1", "h3"}``, and the comment
 activity must have fired at least twice (one per *distinct*
 hash) — the second ``h1`` may or may not have flushed depending
 on the ordering, so we assert ``>= 2`` rather than ``== 2`` or
 ``== 3``.

 Assertions
 ----------
 * ``bitbucket_add_pr_comment`` is invoked at least twice — every
 distinct finding hash MUST have flushed at least once.
 * ``get_previous_findings`` returns the sorted tuple
 ``("h1", "h3")`` — duplicates collapsed into the
 ``_previous_findings`` set.
 * Final status is ``"completed"``; ``failure_reason`` is
 ``None``.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    findings = [
        {"hash": "h1", "body": "missing docstring"},
        {"hash": "h1", "body": "missing docstring (duplicate)"},
        {"hash": "h3", "body": "unhandled exception"},
    ]
    activities = _build_pr_review_activities(log, findings=findings)

    workflow_id = "agent-runner-jira-PAY-9602-dedup"
    parent_task_queue = "agent-runner-pr-review-tq-dedup"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=parent_task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_pr_review_input(
                issue_key="PAY-9602", pr_id=43, iteration=1
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=parent_task_queue,
            )
            result_raw: Any = await handle.result()
            previous_findings = await handle.query("get_previous_findings")

    result = _output_to_dict(result_raw)

    # ----- Diff fetch + LLM ran exactly once ------------------------

    assert log.count("bitbucket_fetch_pr_diff") == 1
    assert log.count("llm_review_code") == 1

    # ----- Comment activity fired at least twice --------------------
    #
    # Each *distinct* finding hash must have flushed at least once.
    # The duplicate ``h1`` may or may not have flushed depending on
    # whether the body's ``_previous_findings`` had absorbed the
    # first ``h1`` before the second occurrence's
    # ``_dedup_findings`` check ran — at the integration boundary
    # we accept either branch and only pin the lower bound the
    # workflow behavior guarantees.
    comment_count = log.count("bitbucket_add_pr_comment")
    assert comment_count >= 2, (
        f"bitbucket_add_pr_comment must fire at least once per "
        f"distinct finding hash (h1 + h3); got {comment_count} "
        f"(call log: {log.names()!r})"
    )
    # Upper bound is the number of findings the LLM returned —
    # nothing must spuriously inflate the count beyond that.
    assert comment_count <= len(findings), (
        f"bitbucket_add_pr_comment must not fire more than once per "
        f"finding the LLM returned; got {comment_count} for "
        f"{len(findings)} findings (call log: {log.names()!r})"
    )

    # ----- Posted bodies carry both distinct hashes -----------------
    #
    # ``h1`` body must be present (regardless of whether it was
    # posted once or twice) and ``h3`` body must be present.
    comment_args_list = log.args_for("bitbucket_add_pr_comment")
    posted_bodies = [args[2] for args in comment_args_list]
    assert any("missing docstring" in body for body in posted_bodies), (
        f"the h1 finding body must surface in at least one posted "
        f"comment; got {posted_bodies!r}"
    )
    assert any("unhandled exception" in body for body in posted_bodies), (
        f"the h3 finding body must surface in at least one posted "
        f"comment; got {posted_bodies!r}"
    )

    # ----- Dedup invariant: query returns the set, not the bag ------
    #
    # The hash set is the canonical surface downstream consumers
    # rely on for cross-iteration dedup. Duplicates
    # MUST collapse into a single set entry. The Temporal JSON
    # data converter renders the query result as a list, so we
    # normalise to a tuple before the equality check.
    assert tuple(previous_findings) == ("h1", "h3"), (
        f"get_previous_findings must return the deduped sorted tuple "
        f"({{h1, h3}}); got {previous_findings!r}"
    )

    # ----- Workflow output ------------------------------------------

    assert result["status"] == "completed", (
        f"expected status=completed on the dedup happy path, got "
        f"{result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None on the dedup happy path, got "
        f"{result!r}"
    )
