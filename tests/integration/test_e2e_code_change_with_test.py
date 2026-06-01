"""End-to-end integration test for the ``code_change_with_test`` flow.

**Validates: Requirements 7.1, 7.3, 7.4** (spec
``platform-mimari-workflows`` task 15.5).

Scenario
--------

The :class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
``code_change_with_test`` body is the canonical happy-path flow for
LLM-driven code changes. The handler walks through six side-effecting
steps before exiting:

    1. ``set_assignee_to_bot`` — claim the Jira issue for the bot.
    2. ``precommit_scanner`` — gitleaks/bandit gate on the proposed
       diff (R7.10). A "block" decision aborts the run.
    3. ``bitbucket_create_commit`` — push the change to the
       ``ai/{issue_key}`` branch (R7.1, R7.2).
    4. Child :class:`ExecutionRunWorkflow` — run the dept-configured
       smoke test against the commit hash (R7.3). A non-``"passed"``
       result short-circuits the run with
       ``failure_reason="execution_run_failed"``; no PR is opened.
    5. PR-create tool (cloud or DC, picked by
       :func:`mcp_client.deployment_router.select_pr_create_tool`) —
       open the draft PR (R7.4 plus the PR-draft enforcement carried
       by the foundation MCP filter).
    6. ``jira_add_comment`` — post the "✅ Draft PR açıldı: …"
       completion line on the original Jira issue.

This file pins the activity-call sequence end-to-end against the
real Temporal time-skipping ``WorkflowEnvironment`` so signal
dispatch, sandbox enforcement, and replay determinism all
participate. Two scenarios are covered:

* ``test_code_change_with_test_happy_path`` — every step succeeds;
  the workflow exits ``status="completed"`` with ``iter_count==1``
  and the activity sequence matches the design contract above.

* ``test_code_change_with_test_test_failure_no_pr`` — the child
  :class:`ExecutionRunWorkflow` returns ``status="failed"``; the PR
  step MUST NOT fire and the failure summary MUST land in Jira. The
  workflow exits with ``failure_reason="execution_run_failed"``.

Activity stubs and the child workflow stub are registered through
``@activity.defn(name=...)`` / ``@workflow.defn(name=...)``
decorators with names matching the production lookups, so the
workflow body finds them via the worker's registry exactly as it
would in production.

Hosts without the embedded ``temporal-test-server`` skip cleanly
via the same module-level gate the existing spec-extension tests
in ``test_temporal_signal.py`` / ``test_temporal_loop_cap.py``
use — see :func:`_temporal_test_env_available` and
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
# Mirrors the bootstrap used by ``test_temporal_signal.py`` and
# ``test_temporal_loop_cap.py``. The agent-runner-worker ships its
# source under ``platform/workers/agent-runner-worker/src/`` and its
# modules import each other under the ``agent_runner`` namespace; the
# ``temporal-shared`` and ``mcp_client`` libraries follow the same
# layout. Adding their ``src/`` directories onto ``sys.path`` makes
# the imports below resolve without first installing the packages.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "mcp_client" / "src"
)

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
    where the binary is not bundled. Surface that cleanly as a
    skip — the integration suite stays green on machines that
    cannot host Temporal locally.
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
# Module-level child-workflow stubs for ``ExecutionRunWorkflow``.
#
# Temporal's ``@workflow.run`` decorator rejects local classes (the
# Temporal worker needs to register a globally referenceable class
# name), so the stubs MUST be declared at module scope. Two flavours
# cover the two branches under test: a "passed" stub for the happy
# path and a "failed" stub for the test-failure scenario. Each test
# registers only one of the two on its child-stub worker, and they
# carry the same registered name (``"ExecutionRunWorkflow"``) so the
# parent workflow's ``execute_child_workflow("ExecutionRunWorkflow", ...)``
# call resolves regardless of which class the worker holds.
#
# The ``temporal-shared.messages`` import sits inside an
# ``imports_passed_through`` block so the Temporal sandbox does not
# trip on the dataclass module's lazy ``Mapping`` typing. Nothing
# else executes at module-import time.
# ---------------------------------------------------------------------------

from temporalio import workflow as _wf  # noqa: E402 - module-level by design

with _wf.unsafe.imports_passed_through():
    from temporal_shared.messages import (  # noqa: E402
        ExecutionRunWorkflowOutput,
    )


@_wf.defn(name="ExecutionRunWorkflow", sandboxed=False)
class _ExecutionRunWorkflowPassedStub:
    """Module-level stub returning a passed test run.

    Registered on the happy-path test's child-stub worker. The
    ``status="passed"`` field flips the parent workflow's
    ``test_passed`` flag to True, so the body proceeds to the
    PR-create / Jira-completion-comment leg.
    """

    @_wf.run
    async def run(self, _input: Any) -> ExecutionRunWorkflowOutput:
        return ExecutionRunWorkflowOutput(
            status="passed",
            exit_code=0,
            stdout_uri="s3://ai-runs/stub/stdout.log",
            stderr_uri=None,
            duration_seconds=1.5,
            runner_id="runner-stub",
            failure_reason=None,
        )


@_wf.defn(name="ExecutionRunWorkflow", sandboxed=False)
class _ExecutionRunWorkflowFailedStub:
    """Module-level stub returning a failed test run.

    Registered on the test-failure scenario's child-stub worker. The
    ``status="failed"`` field flips the parent workflow's
    ``test_passed`` flag to False, so the body posts the failure
    summary comment, sets ``failure_reason="execution_run_failed"``,
    and returns early — no PR-create activity, no
    ``iter_advance_pr_supersede``.
    """

    @_wf.run
    async def run(self, _input: Any) -> ExecutionRunWorkflowOutput:
        return ExecutionRunWorkflowOutput(
            status="failed",
            exit_code=1,
            stdout_uri="s3://ai-runs/stub/stdout.log",
            stderr_uri="s3://ai-runs/stub/stderr.log",
            duration_seconds=2.5,
            runner_id="runner-stub",
            failure_reason="non_zero_exit",
        )


# ---------------------------------------------------------------------------
# Activity stub factory — every activity the ``code_change_with_test``
# handler invokes is registered with a thin recording stub. The brief
# from spec task 15.5 enumerates the full activity set:
#
#   * ``set_assignee_to_bot``
#   * ``precommit_scanner`` (returns ``{"decision": "pass"}``)
#   * ``bitbucket_create_commit`` (returns ``{"commit_hash": "abc123"}``)
#   * ``bitbucket_create_pull_request_cloud`` (returns
#     ``{"pr_id": 42, "url": "..."}``)
#   * ``iter_advance_pr_supersede`` — only fires when the previous
#     iteration's PR id is set; for ``iteration=1`` the workflow
#     skips this branch entirely. The stub is registered defensively
#     so a regression that fires it on the first iteration shows up
#     as a recorded call instead of an "activity not found" failure.
#   * ``jira_add_comment`` (best-effort)
#   * ``jira_build_issue_link`` (best-effort; not called by the
#     ``code_change_with_test`` body today, registered for future
#     compatibility per the task brief).
#
# Failures inside best-effort activities are swallowed by the
# workflow body (``# noqa: BLE001 - best effort``); the stubs below
# always succeed.
# ---------------------------------------------------------------------------


def _build_code_change_activities(
    log: ActivityCallLog,
    *,
    pr_id: int = 42,
    pr_url: str = "https://bitbucket.org/payments/payment-service/pull-requests/42",
    commit_hash: str = "abc123",
) -> list[Any]:
    """Build the bag of stub activities the workflow body invokes."""

    from temporalio import activity

    @activity.defn(name="set_assignee_to_bot")
    async def _set_assignee_to_bot(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        log.record("set_assignee_to_bot", (issue_key, dept_id), {})
        return {"ok": True}

    @activity.defn(name="precommit_scanner")
    async def _precommit_scanner(diff: str) -> dict[str, Any]:
        log.record("precommit_scanner", (diff,), {})
        return {"decision": "pass", "matched_patterns": []}

    @activity.defn(name="bitbucket_create_commit")
    async def _bitbucket_create_commit(*args: Any, **kwargs: Any) -> dict[str, Any]:
        log.record("bitbucket_create_commit", args, kwargs)
        return {
            "commit_hash": commit_hash,
            "branch_name": args[1] if len(args) > 1 else "",
        }

    @activity.defn(name="bitbucket_create_pull_request_cloud")
    async def _bitbucket_create_pull_request_cloud(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record(
            "bitbucket_create_pull_request_cloud", args, kwargs
        )
        return {
            "pr_id": pr_id,
            "id": pr_id,
            "url": pr_url,
            "draft": True,
        }

    @activity.defn(name="bitbucket_create_pull_request_dc")
    async def _bitbucket_create_pull_request_dc(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        # Defensive — the production deployment_router picks the
        # cloud variant when ``deployment="cloud"``. Registering the
        # DC variant too keeps the worker robust against a future
        # config flip without re-touching the test.
        log.record(
            "bitbucket_create_pull_request_dc", args, kwargs
        )
        return {"pr_id": pr_id, "id": pr_id, "url": pr_url, "draft": True}

    @activity.defn(name="iter_advance_pr_supersede")
    async def _iter_advance_pr_supersede(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("iter_advance_pr_supersede", args, kwargs)
        return {"ok": True}

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        log.record("jira_add_comment", (issue_key, body, dept_id), {})
        return None

    @activity.defn(name="jira_build_issue_link")
    async def _jira_build_issue_link(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        log.record("jira_build_issue_link", (issue_key, dept_id), {})
        return {
            "url": f"https://atlassian.example/browse/{issue_key}",
            "site_url": "https://atlassian.example",
        }

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        # ``audit_emit`` is invoked by the workflow body's
        # best-effort audit hooks (e.g. iter==3 banner). The
        # ``code_change_with_test`` flow at iteration=1 does not
        # trigger any audit; the stub is registered defensively
        # so the worker boot does not error if the body gains
        # an audit line in a future revision.
        log.record("audit_emit", (payload,), {})
        return None

    return [
        _set_assignee_to_bot,
        _precommit_scanner,
        _bitbucket_create_commit,
        _bitbucket_create_pull_request_cloud,
        _bitbucket_create_pull_request_dc,
        _iter_advance_pr_supersede,
        _jira_add_comment,
        _jira_build_issue_link,
        _audit_emit,
    ]


# ---------------------------------------------------------------------------
# Input fixture
# ---------------------------------------------------------------------------


def _make_code_change_input(
    *,
    issue_key: str,
    target_repo: str = "payment-service",
    target_branch: str = "develop",
    iteration: int = 1,
) -> Any:
    """Build a :class:`AgentRunnerWorkflowInput` for the happy path.

    The :class:`LlmAnalysisResult` carries ``workflow_type``,
    ``confidence``, ``target_repo`` and ``target_branch`` populated;
    ``output_actions`` is left empty so the body's
    :meth:`_maybe_execute_llm_output_actions` call is a cheap no-op
    (the spec brief explicitly says "no output_actions"). The
    invariants tested here focus on the activity sequence the
    handler invokes irrespective of any LLM-emitted side effects.
    """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type="code_change_with_test",
        confidence="high",
        target_repo=target_repo,
        target_branch=target_branch,
        title=f"Add /healthz endpoint for {issue_key}",
        rationale="Implement health probe and smoke test.",
        output_actions=(),
        token_usage=128,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type="code_change_with_test",
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
# Test 1 — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_code_change_with_test_happy_path() -> None:
    """**Validates: Requirements 7.1, 7.3, 7.4**

    Drive :class:`AgentRunnerWorkflow` through the full
    ``code_change_with_test`` happy path:

    1. ``set_assignee_to_bot`` succeeds.
    2. ``precommit_scanner`` returns ``{"decision": "pass"}``.
    3. ``bitbucket_create_commit`` returns a stable commit hash.
    4. The child :class:`ExecutionRunWorkflow` returns
       ``status="passed"``.
    5. ``bitbucket_create_pull_request_cloud`` returns a draft PR
       descriptor with ``pr_id=42``.
    6. ``jira_add_comment`` posts the "✅ Draft PR açıldı: …"
       completion line.

    Assertions
    ----------
    * Activity call sequence matches the design contract (R7.1, R7.3,
      R7.4): ``set_assignee_to_bot`` → ``precommit_scanner`` →
      ``bitbucket_create_commit`` → child workflow → PR-create tool
      → ``jira_add_comment``.
    * Branch name follows the ``ai/{issue_key}`` convention
      (R7.1 — :func:`temporal_shared.code_change.compute_branch_name`).
    * The PR-create activity ran exactly once (no double-fire).
    * ``iter_advance_pr_supersede`` MUST NOT fire on the first
      iteration (no previous PR to supersede).
    * The Jira comment carries the PR URL stub injected by the
      activity.
    * Final status is ``"completed"``; ``iter_count`` is ``1`` —
      the run-body's initial advance lifts the counter from 0 to 1
      and no further signals are dispatched in this scenario.
    * ``failure_reason`` is ``None``.
    """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _build_code_change_activities(log)
    ExecutionPassedStub = _ExecutionRunWorkflowPassedStub

    workflow_id = "agent-runner-jira-PAY-7510-happy"
    parent_task_queue = "agent-runner-tq-happy"
    # ``_run_execution_child`` hardcodes ``task_queue="execution-runner-tq"``
    # so the child-stub worker MUST listen on that exact queue or
    # the child workflow start will time out. The parent task queue
    # name is only used by the test driver, so it stays unique per
    # test for isolation.
    execution_task_queue = "execution-runner-tq"

    async with _start_time_skipping_or_skip() as env:
        # Two workers — one for the parent ``AgentRunnerWorkflow`` on
        # ``agent-runner-tq``, one for the child ``ExecutionRunWorkflow``
        # on ``execution-runner-tq``. The child task queue is
        # hardcoded by :meth:`AgentRunnerWorkflow._run_execution_child`
        # (it must match the production worker layout per
        # ``temporal-shared.workflow_registry.WORKFLOW_TASK_QUEUES``),
        # so the stub worker has to listen on the same queue or the
        # child workflow start will time out waiting for a poller.
        async with Worker(
            env.client,
            task_queue=parent_task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            async with Worker(
                env.client,
                task_queue=execution_task_queue,
                workflows=[ExecutionPassedStub],
            ):
                inp = _make_code_change_input(
                    issue_key="PAY-7510", iteration=1
                )
                handle = await env.client.start_workflow(
                    AgentRunnerWorkflow.__name__,
                    inp,
                    id=workflow_id,
                    task_queue=parent_task_queue,
                )
                result_raw: Any = await handle.result()

    result = _output_to_dict(result_raw)

    # ----- Activity call sequence -----------------------------------

    sequence = [
        name
        for name in log.names()
        if name in {
            "set_assignee_to_bot",
            "precommit_scanner",
            "bitbucket_create_commit",
            "bitbucket_create_pull_request_cloud",
            "jira_add_comment",
        }
    ]
    expected_prefix = [
        "set_assignee_to_bot",
        "precommit_scanner",
        "bitbucket_create_commit",
        "bitbucket_create_pull_request_cloud",
        "jira_add_comment",
    ]
    assert sequence == expected_prefix, (
        f"unexpected code-change activity sequence: {sequence!r}; "
        f"full call log: {log.names()!r}"
    )

    # ----- Branch name (R7.1) ---------------------------------------

    commit_args_list = log.args_for("bitbucket_create_commit")
    assert len(commit_args_list) == 1, (
        f"bitbucket_create_commit must run exactly once, got "
        f"{len(commit_args_list)}: {log.names()!r}"
    )
    commit_args = commit_args_list[0]
    branch_name = commit_args[1]
    assert branch_name == "ai/PAY-7510", (
        f"branch name must follow the ai/{{issue_key}} convention "
        f"on iteration=1; got {branch_name!r}"
    )

    # ----- PR-create activity (R7.4) --------------------------------

    pr_calls = log.count("bitbucket_create_pull_request_cloud")
    assert pr_calls == 1, (
        f"PR-create tool must fire exactly once on the happy path; "
        f"got {pr_calls} (call log: {log.names()!r})"
    )
    # The DC variant must NOT fire — deployment_router selected the
    # cloud variant.
    assert log.count("bitbucket_create_pull_request_dc") == 0, (
        f"DC PR-create tool must not fire when deployment=cloud; "
        f"call log: {log.names()!r}"
    )

    # ----- iter_advance_pr_supersede (R7.6 / R10.1) -----------------

    # Iteration 1 has no previous PR; the supersede branch must be
    # skipped entirely. A spurious call here would mean the
    # workflow walked the supersede path against a None previous
    # id, which the production code explicitly guards against.
    assert log.count("iter_advance_pr_supersede") == 0, (
        f"iter_advance_pr_supersede must not fire on iteration=1; "
        f"call log: {log.names()!r}"
    )

    # ----- Jira completion comment (R7.4) --------------------------

    comment_args_list = log.args_for("jira_add_comment")
    assert len(comment_args_list) == 1, (
        f"jira_add_comment must run exactly once on the happy path; "
        f"got {len(comment_args_list)}: {log.names()!r}"
    )
    issue_key, body, dept_id = comment_args_list[0]
    assert issue_key == "PAY-7510"
    assert dept_id == "payments"
    assert "Draft PR" in body or "draft" in body.lower(), (
        f"completion comment must mention the draft PR; got {body!r}"
    )
    # The PR url stub injected by the activity surfaces verbatim in
    # the comment body — confirms the workflow extracted the URL
    # from the activity result and forwarded it into Jira.
    assert "pull-requests/42" in body, (
        f"completion comment must carry the PR URL; got {body!r}"
    )

    # ----- Workflow output -----------------------------------------

    assert result["status"] == "completed", (
        f"expected status=completed on the happy path, got {result!r}"
    )
    assert result["iter_count"] == 1, (
        f"expected iter_count=1 (run-body initial advance only) on "
        f"iteration=1; got {result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None on the happy path, got {result!r}"
    )
    assert not result["partial_failure_actions"], (
        f"no partial failures expected on the happy path; got "
        f"{result['partial_failure_actions']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — test failure: no PR opened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_code_change_with_test_test_failure_no_pr() -> None:
    """**Validates: Requirements 7.1, 7.3, 7.4**

    When the child :class:`ExecutionRunWorkflow` returns
    ``status="failed"`` the ``code_change_with_test`` body MUST:

    1. NOT call the PR-create tool (R7.4 — PR opens only when the
       smoke test passes).
    2. Post a Jira comment summarising the failure (best-effort).
    3. Set ``failure_reason="execution_run_failed"`` so the parent
       :class:`AutomationWorkflow` (and any audit consumer) can
       discriminate between "tests failed" and a generic activity
       error.

    Trace
    -----

    * ``set_assignee_to_bot`` succeeds.
    * ``precommit_scanner`` returns ``{"decision": "pass"}``.
    * ``bitbucket_create_commit`` returns ``{"commit_hash": "abc123"}``.
    * The child workflow returns
      ``ExecutionRunWorkflowOutput(status="failed", ...)``.
    * The body's ``if not test_passed`` branch posts the
      "❌ Testler başarısız" comment via ``jira_add_comment``,
      sets ``self._failure_reason = "execution_run_failed"`` and
      returns early — no PR-create activity, no
      ``iter_advance_pr_supersede``.
    * The outer ``run`` body computes the final summary; with
      ``output_actions=()`` and no partial failures the formatter
      returns the empty string, so the body falls back to the
      legacy "✅ Tamamlandı." one-liner. The terminal status is
      therefore ``"completed"``, but ``failure_reason`` carries
      the stable category — the spec brief pins the assertion on
      ``failure_reason``, not ``status``, precisely so this
      asymmetry is detected.
    """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _build_code_change_activities(log)
    ExecutionFailedStub = _ExecutionRunWorkflowFailedStub

    workflow_id = "agent-runner-jira-PAY-7511-failure"
    parent_task_queue = "agent-runner-tq-failure"
    # ``_run_execution_child`` hardcodes ``task_queue="execution-runner-tq"``
    # so the child-stub worker MUST listen on that exact queue or the
    # child workflow start will time out. The parent task queue is
    # only used by the test driver, so it stays unique per test for
    # isolation.
    execution_task_queue = "execution-runner-tq"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=parent_task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            async with Worker(
                env.client,
                task_queue=execution_task_queue,
                workflows=[ExecutionFailedStub],
            ):
                inp = _make_code_change_input(
                    issue_key="PAY-7511", iteration=1
                )
                handle = await env.client.start_workflow(
                    AgentRunnerWorkflow.__name__,
                    inp,
                    id=workflow_id,
                    task_queue=parent_task_queue,
                )
                result_raw: Any = await handle.result()

    result = _output_to_dict(result_raw)

    # ----- No PR-create activity (R7.4) -----------------------------

    assert log.count("bitbucket_create_pull_request_cloud") == 0, (
        f"PR-create tool must NOT fire when the smoke test fails; "
        f"call log: {log.names()!r}"
    )
    assert log.count("bitbucket_create_pull_request_dc") == 0, (
        f"DC PR-create tool must NOT fire when the smoke test fails; "
        f"call log: {log.names()!r}"
    )
    assert log.count("iter_advance_pr_supersede") == 0, (
        f"iter_advance_pr_supersede must NOT fire when no PR opens; "
        f"call log: {log.names()!r}"
    )

    # ----- Failure comment posted ----------------------------------

    comment_args_list = log.args_for("jira_add_comment")
    assert len(comment_args_list) == 1, (
        f"exactly one Jira failure comment must be posted; got "
        f"{len(comment_args_list)} (log: {log.names()!r})"
    )
    issue_key, body, dept_id = comment_args_list[0]
    assert issue_key == "PAY-7511"
    assert dept_id == "payments"
    # Turkish failure prefix mandated by the spec text.
    assert "Testler başarısız" in body or "❌" in body, (
        f"failure comment must surface the test-failure prose; "
        f"got {body!r}"
    )

    # ----- Pre-PR activity sequence still ran -----------------------
    #
    # The handler must have walked through assignee → scanner →
    # commit before bailing out. A regression that short-circuits
    # earlier (e.g. precommit_scanner failing wrongly) would skip
    # one of these.

    assert log.count("set_assignee_to_bot") == 1
    assert log.count("precommit_scanner") == 1
    assert log.count("bitbucket_create_commit") == 1

    # ----- Workflow output (failure_reason) -------------------------

    assert result["failure_reason"] == "execution_run_failed", (
        f"failure_reason must be 'execution_run_failed' when the "
        f"smoke test fails; got {result!r}"
    )
    # ``iter_count`` still reflects the run-body's initial advance
    # — the test failure aborts the body but does not roll back
    # the counter.
    assert result["iter_count"] == 1, (
        f"expected iter_count=1 (run-body initial advance only); "
        f"got {result!r}"
    )
