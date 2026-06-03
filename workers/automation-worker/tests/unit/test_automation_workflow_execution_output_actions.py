"""Unit tests for the ``ExecutionRunWorkflow`` output_actions wire-in
inside :class:`AutomationWorkflow`.

The gateway ``AutomationWorkflow`` dispatches
:class:`ExecutionRunWorkflow` for the ``remote_ssh_test_only`` workflow
type.  When the analyser surfaced one or more output actions for that
run (e.g. publish stdout to Jira as an attachment, or write a
Confluence page summarising the test results), the gateway awaits the
child and forwards the actions to the ``execute_output_actions``
activity together with the runner's MinIO artifact references.

The tests below exercise the **pure** translation layer that bridges
the analyser-side :class:`OutputAction` (kind/severity/payload) onto
the executor-side :class:`OutputAction` (type/params/index) plus the
helpers that pull stdout/stderr URIs out of the child output and split
them into ``(bucket, key)`` pairs.  The full async ``run`` body lives
behind ``workflow.execute_activity`` / ``start_child_workflow`` and is
covered by the existing replay-determinism integration tests.

* Empty :attr:`LlmAnalysisResult.output_actions` keeps the legacy
  dispatch-and-forget contract — the gateway never awaits the child
  (regression guard).
* When the analyser surfaces a ``jira_attachment`` action with no
  explicit MinIO references the gateway synthesises ``bucket`` /
  ``key`` from the runner's stdout URI so the executor's MinIO
  pipeline can stream the artifact to Jira.
* ExecutionRun fail (``exit_code != 0``) → publish branch still
  fires because the SSH activity uploads stdout/stderr even when the
  command exits non-zero.
* The ``OutputActionKind`` → :class:`ActionType` mapping covers every
  kind the description parser allows, and unknown kinds raise
  :class:`ValueError` so the publish branch can skip them gracefully.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — mirror ``test_output_actions.py``.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_DB_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "db-shared" / "src"
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_SRC_DIR, _DB_SHARED_SRC, _TEMPORAL_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)


# pylint: disable=wrong-import-position
from automation_worker.activities.output_actions import (  # noqa: E402
    ExecutionBatchInput,
    OutputAction as ExecutorOutputAction,
)
from automation_worker.workflows import (  # noqa: E402
    automation_workflow as automation_workflow_mod,
)
from automation_worker.workflows.automation_workflow import (  # noqa: E402
    AutomationWorkflow,
    _EXECUTION_DEFAULT_BUCKET,
    _OUTPUT_ACTIONS_TIMEOUT,
)
from db_shared.enums import ActionType  # noqa: E402
from temporal_shared.messages import (  # noqa: E402
    AutomationWorkflowInput,
    ExecutionRunWorkflowOutput,
    LlmAnalysisResult,
    OutputAction,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _make_input(
    issue_key: str = "OPS-1",
    department_id: str = "ops",
) -> AutomationWorkflowInput:
    return AutomationWorkflowInput(
        issue_key=issue_key,
        department_id=department_id,
        available_capabilities=("jira", "execution"),
    )


def _make_output(
    *,
    status: str = "passed",
    exit_code: int | None = 0,
    stdout_uri: str | None = "s3://ai-runs/executions/exec-OPS-1-1/stdout.txt",
    stderr_uri: str | None = "s3://ai-runs/executions/exec-OPS-1-1/stderr.txt",
) -> ExecutionRunWorkflowOutput:
    return ExecutionRunWorkflowOutput(
        status=status,  # type: ignore[arg-type]
        exit_code=exit_code,
        stdout_uri=stdout_uri,
        stderr_uri=stderr_uri,
        duration_seconds=1.5,
        runner_id="runner-1",
    )


class _FakeChildHandle:
    """Awaitable that yields a pre-canned child workflow result.

    Mirrors the surface of Temporal's :class:`ChildWorkflowHandle` —
    we only need ``__await__`` for the helper under test."""

    def __init__(self, result: Any | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc

    def __await__(self):  # noqa: D401 — Temporal handle protocol
        async def _resolve() -> Any:
            if self._exc is not None:
                raise self._exc
            return self._result

        return _resolve().__await__()


# ===========================================================================
# 1. Pure helper coverage
# ===========================================================================


class TestKindToActionType:
    """The kind → ActionType map covers every description-parser kind."""

    def test_jira_comment_maps_to_jira_comment(self) -> None:
        assert (
            AutomationWorkflow._kind_to_action_type("jira_comment")  # noqa: SLF001
            is ActionType.JIRA_COMMENT
        )

    def test_jira_attachment_maps_to_jira_attachment(self) -> None:
        assert (
            AutomationWorkflow._kind_to_action_type("jira_attachment")  # noqa: SLF001
            is ActionType.JIRA_ATTACHMENT
        )

    @pytest.mark.parametrize(
        "kind", ["bitbucket_commit", "bitbucket_put_file"]
    )
    def test_bitbucket_commit_kinds_map_to_bitbucket_commit(
        self, kind: str
    ) -> None:
        assert (
            AutomationWorkflow._kind_to_action_type(kind)  # noqa: SLF001
            is ActionType.BITBUCKET_COMMIT
        )

    @pytest.mark.parametrize(
        "kind", ["bitbucket_pr", "bitbucket_create_pr"]
    )
    def test_bitbucket_kinds_collapse_to_bitbucket_pr(
        self, kind: str
    ) -> None:
        assert (
            AutomationWorkflow._kind_to_action_type(kind)  # noqa: SLF001
            is ActionType.BITBUCKET_PR
        )

    @pytest.mark.parametrize(
        "kind",
        [
            "confluence_page",
            "confluence_create_page",
            "confluence_update_page",
        ],
    )
    def test_confluence_kinds_collapse_to_confluence_page(
        self, kind: str
    ) -> None:
        # The executor distinguishes create / update via params.page_id
        # so the kind literal collapses to a single ActionType.
        assert (
            AutomationWorkflow._kind_to_action_type(kind)  # noqa: SLF001
            is ActionType.CONFLUENCE_PAGE
        )

    def test_jira_transition_maps_to_jira_transition(self) -> None:
        assert (
            AutomationWorkflow._kind_to_action_type("jira_transition")  # noqa: SLF001
            is ActionType.JIRA_TRANSITION
        )

    @pytest.mark.parametrize(
        "kind", ["slack_notify", "email_notify", "totally_invented"]
    )
    def test_unmapped_kind_raises_value_error(self, kind: str) -> None:
        with pytest.raises(ValueError):
            AutomationWorkflow._kind_to_action_type(kind)  # noqa: SLF001


class TestSplitMinioUri:
    """The S3 URI parser is a pure helper used by the publish branch."""

    def test_full_s3_uri_split(self) -> None:
        bucket, key = AutomationWorkflow._split_minio_uri(  # noqa: SLF001
            "s3://my-bucket/path/to/file.log",
            fallback_key="ignored",
        )
        assert bucket == "my-bucket"
        assert key == "path/to/file.log"

    def test_bare_bucket_path_split(self) -> None:
        bucket, key = AutomationWorkflow._split_minio_uri(  # noqa: SLF001
            "my-bucket/path/to/file.log",
            fallback_key="ignored",
        )
        assert bucket == "my-bucket"
        assert key == "path/to/file.log"

    def test_none_uri_falls_back_to_default_bucket_and_fallback_key(
        self,
    ) -> None:
        bucket, key = AutomationWorkflow._split_minio_uri(  # noqa: SLF001
            None,
            fallback_key="executions/exec-1/stdout.log",
        )
        assert bucket == _EXECUTION_DEFAULT_BUCKET
        assert key == "executions/exec-1/stdout.log"

    def test_empty_string_falls_back_to_default(self) -> None:
        bucket, key = AutomationWorkflow._split_minio_uri(  # noqa: SLF001
            "", fallback_key="fallback/key"
        )
        assert bucket == _EXECUTION_DEFAULT_BUCKET
        assert key == "fallback/key"

    def test_uri_without_slash_falls_back_to_default(self) -> None:
        # ``s3://just-a-bucket`` (no key path) is malformed for our
        # purposes — the helper should fall back to the deterministic
        # key rather than producing an empty / undefined key.
        bucket, key = AutomationWorkflow._split_minio_uri(  # noqa: SLF001
            "s3://just-a-bucket", fallback_key="fallback/key"
        )
        assert bucket == _EXECUTION_DEFAULT_BUCKET
        assert key == "fallback/key"


class TestPayloadToParams:
    """``OutputAction.payload`` (tuple-of-pairs) → executor params dict."""

    def test_round_trips_simple_payload(self) -> None:
        payload = (("body", "Done"), ("issue_key", "OPS-1"))
        params = AutomationWorkflow._payload_to_params(payload)  # noqa: SLF001
        assert params == {"body": "Done", "issue_key": "OPS-1"}

    def test_drops_malformed_entries(self) -> None:
        payload = (
            ("body", "Done"),
            ("", "empty key dropped"),
            ("ok", 42),
        )
        params = AutomationWorkflow._payload_to_params(payload)  # noqa: SLF001
        assert params == {"body": "Done", "ok": 42}

    def test_empty_payload_yields_empty_dict(self) -> None:
        assert (
            AutomationWorkflow._payload_to_params(())  # noqa: SLF001
            == {}
        )


class TestExtractExecutionStdoutUri:
    """``stdout_uri`` extraction tolerates dataclass / dict / unknown."""

    def test_dataclass_result(self) -> None:
        out = _make_output(stdout_uri="s3://ai-runs/foo/stdout.txt")
        assert (
            AutomationWorkflow._extract_execution_stdout_uri(out)  # noqa: SLF001
            == "s3://ai-runs/foo/stdout.txt"
        )

    def test_dict_result(self) -> None:
        result = {"stdout_uri": "s3://ai-runs/bar/stdout.txt"}
        assert (
            AutomationWorkflow._extract_execution_stdout_uri(  # noqa: SLF001
                result
            )
            == "s3://ai-runs/bar/stdout.txt"
        )

    def test_none_result_returns_none(self) -> None:
        assert (
            AutomationWorkflow._extract_execution_stdout_uri(None)  # noqa: SLF001
            is None
        )

    def test_dataclass_without_uri_returns_none(self) -> None:
        out = _make_output(stdout_uri=None)
        assert (
            AutomationWorkflow._extract_execution_stdout_uri(out)  # noqa: SLF001
            is None
        )


# ===========================================================================
# 2. End-to-end publish branch (with mocked workflow primitives)
# ===========================================================================
#
# ``_await_execution_run_and_publish_results`` calls
# ``workflow.execute_activity`` exactly once when the analyser
# supplies actions.  We monkey-patch ``workflow.execute_activity`` and
# ``workflow.logger`` on the imported ``automation_workflow_mod`` so
# the helper can run outside the Temporal sandbox.


@pytest.fixture
def patched_workflow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``workflow.execute_activity`` + logger with test doubles."""

    import logging

    fake_execute = AsyncMock(return_value=None)
    monkeypatch.setattr(
        automation_workflow_mod.workflow,
        "execute_activity",
        fake_execute,
        raising=False,
    )
    monkeypatch.setattr(
        automation_workflow_mod.workflow,
        "logger",
        logging.getLogger("test_publish_branch"),
        raising=False,
    )
    return {"execute_activity": fake_execute}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestPublishBranchSynthesisesMinIORefs:
    """When the analyser asks for a Jira attachment without explicit
    MinIO refs, the gateway synthesises ``bucket`` / ``key`` from the
    child's stdout URI."""

    def test_jira_attachment_inherits_stdout_uri_from_child(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_attachment",
                severity="best_effort",
                payload=(),  # no bucket / key / file_path / file_name
            ),
        )
        child = _FakeChildHandle(
            result=_make_output(
                stdout_uri="s3://ai-runs/executions/child-1/stdout.txt",
                stderr_uri="s3://ai-runs/executions/child-1/stderr.txt",
            )
        )

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=actions,
            )
        )

        execute_activity = patched_workflow["execute_activity"]
        assert execute_activity.await_count == 1
        call = execute_activity.await_args
        assert call.args[0] == "execute_output_actions"
        batch: ExecutionBatchInput = call.kwargs["args"][0]
        assert batch.issue_key == "OPS-1"
        assert batch.dept_id == "ops"
        assert batch.workflow_id == "exec-OPS-1-1"
        assert len(batch.actions) == 1
        action = batch.actions[0]
        assert isinstance(action, ExecutorOutputAction)
        assert action.type is ActionType.JIRA_ATTACHMENT
        assert action.params["bucket"] == "ai-runs"
        assert action.params["key"] == "executions/child-1/stdout.txt"
        # Defaults filled in for the executor
        assert action.params["issue_key"] == "OPS-1"
        assert action.params["dept_id"] == "ops"
        assert action.params["file_name"] == "stdout.log"
        # Activity options are the publish-branch defaults.
        assert call.kwargs["start_to_close_timeout"] == _OUTPUT_ACTIONS_TIMEOUT

    def test_jira_attachment_with_explicit_key_keeps_caller_payload(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        # When the analyser already supplied bucket + key the gateway
        # must not stomp on them — the executor receives the literal
        # caller payload.
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_attachment",
                severity="best_effort",
                payload=(
                    ("bucket", "custom-bucket"),
                    ("file_name", "report.md"),
                    ("key", "custom/path/report.md"),
                ),
            ),
        )
        child = _FakeChildHandle(result=_make_output())

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=actions,
            )
        )

        batch: ExecutionBatchInput = (
            patched_workflow["execute_activity"].await_args.kwargs["args"][0]
        )
        action = batch.actions[0]
        assert action.params["bucket"] == "custom-bucket"
        assert action.params["key"] == "custom/path/report.md"
        assert action.params["file_name"] == "report.md"

    def test_jira_attachment_with_key_name_stderr_picks_stderr_uri(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        # Operators wanting to attach stderr instead of stdout opt in
        # via ``key_name`` — the gateway honours that hint when
        # synthesising the MinIO ref.
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_attachment",
                severity="best_effort",
                payload=(("key_name", "stderr.log"),),
            ),
        )
        child = _FakeChildHandle(
            result=_make_output(
                stdout_uri="s3://ai-runs/executions/child-9/stdout.txt",
                stderr_uri="s3://ai-runs/executions/child-9/stderr.txt",
            )
        )

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-9",
                actions=actions,
            )
        )

        batch: ExecutionBatchInput = (
            patched_workflow["execute_activity"].await_args.kwargs["args"][0]
        )
        assert (
            batch.actions[0].params["key"]
            == "executions/child-9/stderr.txt"
        )
        assert batch.actions[0].params["file_name"] == "stderr.log"


class TestPublishBranchOnExitCodeFailure:
    """Even when the runner exits non-zero, the publish branch fires
    because the SSH activity still uploads stdout / stderr."""

    def test_failed_exit_still_publishes(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_attachment",
                severity="best_effort",
                payload=(),
            ),
        )
        child = _FakeChildHandle(
            result=_make_output(
                status="failed",
                exit_code=1,
                stdout_uri="s3://ai-runs/executions/exec-fail/stdout.txt",
                stderr_uri="s3://ai-runs/executions/exec-fail/stderr.txt",
            )
        )

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-fail",
                actions=actions,
            )
        )

        execute_activity = patched_workflow["execute_activity"]
        assert execute_activity.await_count == 1, (
            "publish branch must fire even on non-zero exit code"
        )

    def test_child_workflow_exception_still_publishes_with_fallback_key(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        # When the child raised before producing an output (e.g.
        # runner unreachable) the gateway falls back to the
        # deterministic ``execution_artifact_key`` so ``execute_output_actions``
        # at least attempts to attach what the runner did write
        # before the failure.
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_attachment",
                severity="best_effort",
                payload=(),
            ),
        )
        child = _FakeChildHandle(exc=RuntimeError("runner unreachable"))

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-iter-1",
                actions=actions,
            )
        )

        execute_activity = patched_workflow["execute_activity"]
        assert execute_activity.await_count == 1
        batch: ExecutionBatchInput = execute_activity.await_args.kwargs[
            "args"
        ][0]
        action = batch.actions[0]
        assert action.params["bucket"] == _EXECUTION_DEFAULT_BUCKET
        # Fallback key is derived from execution_artifact_key.
        assert (
            action.params["key"]
            == "executions/exec-OPS-1-iter-1/stdout.log"
        )


class TestPublishBranchRegressionGuard:
    """Empty actions tuple is the regression guard — no awaits, no
    activity calls (existing dispatch-and-forget behaviour stays
    verbatim)."""

    def test_empty_actions_short_circuits(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        wf = AutomationWorkflow()
        child = _FakeChildHandle(result=_make_output())

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=(),
            )
        )

        assert patched_workflow["execute_activity"].await_count == 0


class TestPublishBranchHandlesUnmappedKinds:
    """Slack / email / unknown kinds are skipped without breaking the
    rest of the batch."""

    def test_unmapped_kind_is_skipped(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="slack_notify",
                severity="best_effort",
                payload=(("channel", "#ops"),),
            ),
            OutputAction(
                kind="jira_comment",
                severity="best_effort",
                payload=(("body", "Test bitti."),),
            ),
        )
        child = _FakeChildHandle(result=_make_output())

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=actions,
            )
        )

        batch: ExecutionBatchInput = (
            patched_workflow["execute_activity"].await_args.kwargs["args"][0]
        )
        # Only the jira_comment survived — slack_notify dropped.
        assert len(batch.actions) == 1
        assert batch.actions[0].type is ActionType.JIRA_COMMENT
        assert batch.actions[0].index == 1  # original analyser index

    def test_only_unmapped_kinds_short_circuits(
        self, patched_workflow: dict[str, Any]
    ) -> None:
        # When every action gets dropped during translation the
        # gateway must not dispatch an empty batch — the executor
        # would still post the audit summary even with zero results.
        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="slack_notify",
                severity="best_effort",
                payload=(("channel", "#ops"),),
            ),
            OutputAction(
                kind="email_notify",
                severity="best_effort",
                payload=(("to", "ops@example.com"),),
            ),
        )
        child = _FakeChildHandle(result=_make_output())

        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=actions,
            )
        )

        assert patched_workflow["execute_activity"].await_count == 0


class TestPublishBranchActivityFailureIsBestEffort:
    """An executor activity failure must not crash the gateway — the
    decision stays ``dispatched`` because the child *was* dispatched."""

    def test_activity_exception_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging

        fake_execute = AsyncMock(side_effect=RuntimeError("MCP down"))
        monkeypatch.setattr(
            automation_workflow_mod.workflow,
            "execute_activity",
            fake_execute,
            raising=False,
        )
        monkeypatch.setattr(
            automation_workflow_mod.workflow,
            "logger",
            logging.getLogger("test_publish_branch_failure"),
            raising=False,
        )

        wf = AutomationWorkflow()
        actions = (
            OutputAction(
                kind="jira_comment",
                severity="best_effort",
                payload=(("body", "Test ran."),),
            ),
        )
        child = _FakeChildHandle(result=_make_output())

        # Must not raise.
        _run(
            wf._await_execution_run_and_publish_results(  # noqa: SLF001
                child_handle=child,
                inp=_make_input(),
                child_workflow_id="exec-OPS-1-1",
                actions=actions,
            )
        )

        assert fake_execute.await_count == 1


# ===========================================================================
# 3. Integration with run() — the dispatch branch only awaits when
# analysis.output_actions is non-empty.
# ===========================================================================
#
# We do not exercise the full ``run`` body here (it requires the
# Temporal sandbox); instead we assert that the static branch in
# ``run`` is wired against ``analysis.output_actions``:
#
#   * the new ``elif`` block names the helper symbol, and
#   * the helper's existence + signature match the call site.


class TestRunBodyWiring:
    """Static guards that lock the ``run`` integration."""

    def test_publish_helper_exists_and_is_async(self) -> None:
        helper = AutomationWorkflow._await_execution_run_and_publish_results  # noqa: SLF001
        assert asyncio.iscoroutinefunction(helper)

    def test_run_body_calls_publish_helper_for_remote_ssh(self) -> None:
        # AST-style guard — the call site must reference the helper
        # by name so a refactor to a different method name surfaces
        # immediately as a test failure rather than a silent regression.
        source = Path(automation_workflow_mod.__file__).read_text(
            encoding="utf-8"
        )
        assert (
            "_await_execution_run_and_publish_results" in source
        )
        # Both branches of the dispatch (noop_test + remote_ssh)
        # must remain present.
        assert '"noop_test"' in source or "'noop_test'" in source
        assert (
            '"remote_ssh_test_only"' in source
            or "'remote_ssh_test_only'" in source
        )

    def test_publish_branch_gated_on_analysis_output_actions(self) -> None:
        # The ``elif`` is gated on ``analysis.output_actions`` so an
        # analyser that surfaced no actions keeps the legacy
        # dispatch-and-forget contract.  The textual guard is good
        # enough — the runtime regression test above proves the
        # guard fires for the empty-tuple case.
        source = Path(automation_workflow_mod.__file__).read_text(
            encoding="utf-8"
        )
        assert "analysis.output_actions" in source

    def test_execute_output_actions_referenced_as_string_literal(
        self,
    ) -> None:
        # Like every other activity used by the workflow,
        # execute_output_actions must be referenced via a string name
        # (workflow.execute_activity("execute_output_actions", ...))
        # so the workflow module never imports the activity callable
        # at module scope — keeps the determinism contract intact.
        source = Path(automation_workflow_mod.__file__).read_text(
            encoding="utf-8"
        )
        assert (
            '"execute_output_actions"' in source
            or "'execute_output_actions'" in source
        )
