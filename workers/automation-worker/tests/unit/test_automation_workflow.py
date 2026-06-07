"""Unit tests for ``AutomationWorkflow``.

The workflow body is exercised **without** a Temporal worker. Two
strategies cover the surface area the task ships:

* **AST inspection** - the workflow module obeys the determinism
  contract: no ``datetime.now`` / ``time.time`` /
  ``random`` / ``uuid`` / ``os.environ`` reads in the workflow body,
  activities referenced by string name only, no import of activity
  modules at module scope.

* **Direct pure-helper inspection** - the routing tables, the child
  workflow specification builder, the rule coercer, and the
  early-exit helpers. These methods do not consult any Temporal
  primitives so we can call them with the class ``__init__()`` and a
  plain :class:`AutomationWorkflowInput`.

The full ``run()`` body lives behind ``workflow.execute_activity`` and
``workflow.start_child_workflow`` calls, which require the Temporal
sandbox to drive - those paths are covered in
``tests/property/test_workflow_determinism_replay.py`` (history replay)
and the integration suite. The tests here stay deterministic and fast.
"""

from __future__ import annotations

import ast
import sys
from datetime import timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors and ``sys.path`` bootstrapping
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# pylint: disable=wrong-import-position
from automation_worker.workflows import (  # noqa: E402
    automation_workflow as automation_workflow_mod,
)
from automation_worker.workflows.automation_workflow import (  # noqa: E402
    AutomationWorkflow,
    _AGENT_RUNNER_WORKFLOW_TYPES,
    _AutomationStop,
    _BRANCH_AWARE_WORKFLOW_TYPES,
    _EXECUTION_RUN_WORKFLOW_TYPES,
    _format_ack_comment,
    _format_branch_rule_denied_comment,
    _format_missing_caps_comment,
    _format_unknown_workflow_type_comment,
)
from temporal_shared.branch_rules import (  # noqa: E402
    DEFAULT_BRANCH_PATTERN_RULES,
    BranchPatternRule,
    RouteDecision,
)
from temporal_shared.capabilities import (  # noqa: E402
    WORKFLOW_TYPE_CAPABILITIES,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    AutomationWorkflowInput,
    AutomationWorkflowOutput,
    ChildWorkflowSpec,
    ExecutionRunWorkflowInput,
    LlmAnalysisResult,
)


# ===========================================================================
# 1. Determinism contract - static (AST) checks
# ===========================================================================


class TestDeterminismStatic:
    """The workflow module body must be replay-safe. Only Temporal-blessed
    primitives (``workflow.now``, ``workflow.execute_activity``,
    ``workflow.start_child_workflow``, ``workflow.uuid4``) are allowed
    for non-determinism sources; activity callables must never be
    imported at module scope so the worker boot does not pull in
    network-side machinery before the sandbox is ready.
    """

    @pytest.fixture(scope="class")
    def module_source(self) -> str:
        path = Path(automation_workflow_mod.__file__)
        return path.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def module_tree(self, module_source: str) -> ast.Module:
        return ast.parse(module_source)

    def test_no_datetime_now_call(self, module_tree: ast.Module) -> None:
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"now", "utcnow"}:
                    receiver = node.func.value
                    is_workflow_now = (
                        isinstance(receiver, ast.Name)
                        and receiver.id == "workflow"
                    )
                    assert is_workflow_now, (
                        f"Forbidden non-deterministic time source "
                        f"{ast.dump(node.func)!r}; only workflow.now() "
                        f"is permitted."
                    )

    def test_no_time_module_calls(self, module_tree: ast.Module) -> None:
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "time", (
                    f"Forbidden ``time`` module reference {ast.dump(node)!r}; "
                    f"use workflow.now() / workflow.sleep() instead."
                )

    def test_no_random_or_uuid_module(self, module_tree: ast.Module) -> None:
        # ``random`` and ``uuid`` modules must not be referenced; only
        # ``workflow.uuid4()`` is allowed.
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id not in {"random", "uuid"}, (
                    f"Forbidden non-deterministic ID/random source "
                    f"{ast.dump(node)!r}."
                )

    def test_no_os_environ_read(self, module_tree: ast.Module) -> None:
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr in {"environ", "getenv"}:
                    pytest.fail(
                        f"Forbidden direct env read {ast.dump(node)!r}; "
                        f"use an activity instead."
                    )

    def test_activities_referenced_as_string_names(
        self, module_source: str
    ) -> None:
        # Every activity used by the workflow must appear as a quoted
        # string literal - confirms the workflow uses
        # ``execute_activity("name", ...)`` rather than a callable
        # reference (which would force importing the activity module
        # at workflow-module import time).
        for activity_name in (
            "jira_add_comment",
            "llm_analyze_task",
            "load_branch_pattern_rules",
            "audit_write",
        ):
            assert (
                f'"{activity_name}"' in module_source
                or f"'{activity_name}'" in module_source
            ), f"Activity {activity_name!r} not referenced as a string literal."

    def test_no_activity_module_imports_at_module_scope(
        self, module_tree: ast.Module
    ) -> None:
        # Imports inside ``with workflow.unsafe.imports_passed_through():``
        # are nested under a ``With`` node and therefore not part of
        # ``module_tree.body`` - only top-level imports are scanned here.
        for node in module_tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                target = (
                    node.module
                    if isinstance(node, ast.ImportFrom)
                    else node.names[0].name
                )
                assert target is None or "activities" not in target, (
                    f"Workflow module must not import activity modules "
                    f"at module scope: found {target!r}."
                )


# ===========================================================================
# 2. Routing tables - exhaustive coverage of WORKFLOW_TYPE_CAPABILITIES
# ===========================================================================


class TestRoutingTables:
    """Every workflow type in :data:`WORKFLOW_TYPE_CAPABILITIES` must
    map to exactly one of the two child workflow groups. The tables
    are the routing source of truth for ``_build_child_spec``."""

    def test_every_workflow_type_routes_to_exactly_one_group(self) -> None:
        agent = _AGENT_RUNNER_WORKFLOW_TYPES
        executor = _EXECUTION_RUN_WORKFLOW_TYPES
        # No overlap.
        assert agent.isdisjoint(executor), (
            f"AgentRunner and ExecutionRun routing sets overlap: "
            f"{agent & executor}"
        )
        # Every known workflow type lands in one of the two sets.
        all_known = set(WORKFLOW_TYPE_CAPABILITIES.keys())
        routed = agent | executor
        # ``multi_step`` and historical aliases such as ``research_basic``
        # may exist in either capability table or routing set during
        # the migration window. Just assert that at least the
        # design-listed routing targets are present.
        for wf_type in (
            "code_change_with_test",
            "code_change_commit_only",
            "pr_review",
            "confluence_doc_create",
            "confluence_doc_update",
            "multi_step",
        ):
            assert wf_type in agent, (
                f"Expected {wf_type!r} to route to AgentRunnerWorkflow."
            )
        for wf_type in ("remote_ssh_test_only", "noop_test"):
            assert wf_type in executor, (
                f"Expected {wf_type!r} to route to ExecutionRunWorkflow."
            )
        # Every capability-table key has a routing decision.
        unrouted = all_known - routed
        assert not unrouted, (
            f"Workflow types in WORKFLOW_TYPE_CAPABILITIES without a "
            f"routing entry: {sorted(unrouted)}"
        )

    def test_branch_aware_set_is_subset_of_agent_runner(self) -> None:
        # Branch-pattern rules only apply to the code-change /
        # pr_review group, which is itself a subset of the
        # agent-runner-targeted workflow types.
        assert _BRANCH_AWARE_WORKFLOW_TYPES <= _AGENT_RUNNER_WORKFLOW_TYPES

    def test_branch_aware_set_matches_design(self) -> None:
        # design.md §"Workflow Type Routing" lists exactly these three
        # workflow types as branch-aware.
        assert _BRANCH_AWARE_WORKFLOW_TYPES == frozenset(
            {
                "code_change_with_test",
                "code_change_commit_only",
                "pr_review",
            }
        )


# ===========================================================================
# 3. Retry policy / timeout configuration
# ===========================================================================


class TestActivityOptions:
    """Activity timeouts and retry policies are part of the workflow's
    reliability contract."""

    def test_short_timeout_is_two_minutes(self) -> None:
        assert automation_workflow_mod._SHORT_TIMEOUT == timedelta(minutes=2)  # noqa: SLF001

    def test_llm_timeout_is_five_minutes(self) -> None:
        assert automation_workflow_mod._LLM_TIMEOUT == timedelta(minutes=5)  # noqa: SLF001

    def test_default_retry_caps_attempts_at_five(self) -> None:
        policy = automation_workflow_mod._DEFAULT_RETRY  # noqa: SLF001
        assert policy.maximum_attempts == 5
        assert policy.initial_interval == timedelta(seconds=1)
        assert policy.backoff_coefficient == 2.0

    def test_llm_retry_caps_attempts_at_three(self) -> None:
        # LLM analysis is non-idempotent (token cap, prompt-validation
        # failures). Three attempts is the upper bound.
        policy = automation_workflow_mod._LLM_RETRY  # noqa: SLF001
        assert policy.maximum_attempts == 3


# ===========================================================================
# 4. Pure formatter helpers
# ===========================================================================


class TestFormatters:
    """The Turkish-prose formatters are part of the user-visible audit
    trail; lock their key tokens so an accidental re-word during a
    refactor surfaces as a regression."""

    def test_ack_comment_mentions_pickup(self) -> None:
        body = _format_ack_comment()
        assert "🤖" in body
        assert "Task" in body or "task" in body

    def test_missing_caps_comment_lists_capabilities(self) -> None:
        body = _format_missing_caps_comment(
            "code_change_with_test", ("bitbucket", "execution")
        )
        assert "code_change_with_test" in body
        assert "bitbucket" in body
        assert "execution" in body
        # Capabilities are sorted in the body for stability.
        assert body.index("bitbucket") < body.index("execution")

    def test_missing_caps_comment_uses_denial_marker(self) -> None:
        body = _format_missing_caps_comment("pr_review", ("bitbucket",))
        assert "⛔" in body

    def test_unknown_workflow_type_comment_quotes_value(self) -> None:
        body = _format_unknown_workflow_type_comment("totally_invalid")
        assert "totally_invalid" in body
        assert "⛔" in body

    def test_branch_rule_denied_comment_includes_branch_and_glob(
        self,
    ) -> None:
        decision = RouteDecision(
            allowed=False,
            reason="hotfix_requires_pr",
            matched_glob="hotfix/*",
        )
        body = _format_branch_rule_denied_comment(
            "code_change_commit_only", "hotfix/PAY-1", decision
        )
        assert "hotfix/PAY-1" in body
        assert "code_change_commit_only" in body
        assert "hotfix/*" in body
        assert "hotfix_requires_pr" in body


# ===========================================================================
# 5. ``_build_child_spec`` - routing decision logic
# ===========================================================================


def _make_input(
    issue_key: str = "PAY-4211",
    department_id: str = "payments",
    available_capabilities: tuple[str, ...] = (
        "jira",
        "bitbucket",
        "execution",
    ),
) -> AutomationWorkflowInput:
    return AutomationWorkflowInput(
        issue_key=issue_key,
        department_id=department_id,
        available_capabilities=available_capabilities,
    )


def _make_analysis(
    workflow_type: str = "code_change_with_test",
    target_repo: str | None = "payment-callbacks",
    target_branch: str | None = "feature/PAY-4211",
) -> LlmAnalysisResult:
    return LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_repo=target_repo,
        target_branch=target_branch,
        title="implement payment callback",
        rationale="user requested code change",
    )


class TestBuildChildSpec:
    """``_build_child_spec`` is a pure helper - no Temporal primitives
    consulted, so we can call it on a fresh instance without setup."""

    def test_code_change_routes_to_agent_runner(self) -> None:
        wf = AutomationWorkflow()
        spec = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="automation-jira-PAY-4211",
            inp=_make_input(),
            analysis=_make_analysis("code_change_with_test"),
        )
        assert spec.workflow_name == "AgentRunnerWorkflow"
        assert spec.task_queue == "agent-runner-tq"
        assert spec.workflow_id == (
            "AgentRunnerWorkflow-automation-jira-PAY-4211-iter-1"
        )
        assert spec.parent_close_policy == "ABANDON"

    def test_pr_review_routes_to_agent_runner(self) -> None:
        wf = AutomationWorkflow()
        spec = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="automation-bb-callbacks-pr-1",
            inp=_make_input(),
            analysis=_make_analysis(
                "pr_review", target_branch="feature/x"
            ),
        )
        assert spec.workflow_name == "AgentRunnerWorkflow"
        assert spec.task_queue == "agent-runner-tq"

    def test_remote_ssh_routes_to_execution_runner(self) -> None:
        wf = AutomationWorkflow()
        spec = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="automation-jira-PAY-1",
            inp=_make_input(),
            analysis=_make_analysis(
                "remote_ssh_test_only",
                target_repo=None,
                target_branch=None,
            ),
        )
        assert spec.workflow_name == "ExecutionRunWorkflow"
        assert spec.task_queue == "execution-runner-tq"
        assert spec.parent_close_policy == "ABANDON"

    def test_noop_test_routes_to_execution_runner(self) -> None:
        wf = AutomationWorkflow()
        spec = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="automation-jira-OPS-1",
            inp=_make_input(),
            analysis=_make_analysis(
                "noop_test", target_repo=None, target_branch=None
            ),
        )
        assert spec.workflow_name == "ExecutionRunWorkflow"
        assert spec.task_queue == "execution-runner-tq"
        assert spec.parent_close_policy == "TERMINATE"

    def test_child_id_is_deterministic_no_uuid(self) -> None:
        # The child id must depend only on the parent id and a fixed
        # iteration counter - not on ``uuid.uuid4`` or
        # ``workflow.uuid4`` (replay determinism).
        wf = AutomationWorkflow()
        analysis = _make_analysis("code_change_with_test")
        spec_a = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="parent-1",
            inp=_make_input(),
            analysis=analysis,
        )
        spec_b = wf._build_child_spec(  # noqa: SLF001
            parent_workflow_id="parent-1",
            inp=_make_input(),
            analysis=analysis,
        )
        assert spec_a.workflow_id == spec_b.workflow_id


# ===========================================================================
# 6. ``_child_args`` - input dataclass shape per child
# ===========================================================================


class TestChildArgs:
    def test_agent_runner_args_carry_full_envelope(self) -> None:
        wf = AutomationWorkflow()
        inp = _make_input(issue_key="PAY-4211", department_id="payments")
        analysis = _make_analysis(
            "code_change_with_test",
            target_repo="payment-callbacks",
            target_branch="feature/PAY-4211",
        )
        spec = ChildWorkflowSpec(
            workflow_name="AgentRunnerWorkflow",
            workflow_id="agent-PAY-4211",
            task_queue="agent-runner-tq",
        )

        args = wf._child_args(spec, inp, analysis)  # noqa: SLF001

        assert len(args) == 1
        child_input = args[0]
        assert isinstance(child_input, AgentRunnerWorkflowInput)
        assert child_input.issue_key == "PAY-4211"
        assert child_input.department_id == "payments"
        assert child_input.workflow_type == "code_change_with_test"
        assert child_input.target_repo == "payment-callbacks"
        assert child_input.target_branch == "feature/PAY-4211"
        assert child_input.analysis is analysis

    def test_execution_runner_args_use_execution_envelope(self) -> None:
        wf = AutomationWorkflow()
        inp = _make_input(issue_key="OPS-1", department_id="ops")
        analysis = _make_analysis(
            "remote_ssh_test_only", target_repo=None, target_branch=None
        )
        spec = ChildWorkflowSpec(
            workflow_name="ExecutionRunWorkflow",
            workflow_id="exec-OPS-1",
            task_queue="execution-runner-tq",
        )

        args = wf._child_args(spec, inp, analysis)  # noqa: SLF001

        assert len(args) == 1
        child_input = args[0]
        assert isinstance(child_input, ExecutionRunWorkflowInput)
        assert child_input.department_id == "ops"
        # parent_workflow_id is set to the issue key for human-readable
        # audit correlation.
        assert child_input.parent_workflow_id == "OPS-1"


# ===========================================================================
# 7. ``_coerce_rules`` - config loader output normalisation
# ===========================================================================


class TestCoerceRules:
    def test_none_falls_back_to_defaults(self) -> None:
        wf = AutomationWorkflow()
        assert (
            wf._coerce_rules(None)  # noqa: SLF001
            is DEFAULT_BRANCH_PATTERN_RULES
        )

    def test_single_rule_wrapped_into_tuple(self) -> None:
        wf = AutomationWorkflow()
        rule = BranchPatternRule(
            glob="release/*",
            allowed_workflow_types=frozenset({"pr_review"}),
        )
        coerced = wf._coerce_rules(rule)  # noqa: SLF001
        assert coerced == (rule,)

    def test_dict_list_decoded_into_rule_tuple(self) -> None:
        wf = AutomationWorkflow()
        coerced = wf._coerce_rules(  # noqa: SLF001
            [
                {
                    "glob": "hotfix/*",
                    "denied_workflow_types": ["code_change_commit_only"],
                    "reason": "hotfix_requires_pr",
                },
                {
                    "glob": "release/*",
                    "allowed_workflow_types": ["pr_review"],
                },
            ]
        )
        assert len(coerced) == 2
        assert coerced[0].glob == "hotfix/*"
        assert "code_change_commit_only" in coerced[0].denied_workflow_types
        assert coerced[1].glob == "release/*"
        assert "pr_review" in coerced[1].allowed_workflow_types

    def test_unknown_item_shape_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``_coerce_rules`` calls ``workflow.logger.warning`` for the
        # warning side-channel, which would normally require a
        # Temporal event loop. Replace the logger with a plain
        # ``logging.Logger`` so the helper can be unit-tested outside
        # the sandbox.
        import logging

        monkeypatch.setattr(
            automation_workflow_mod.workflow,
            "logger",
            logging.getLogger("test"),
            raising=False,
        )
        wf = AutomationWorkflow()
        assert (
            wf._coerce_rules([42, "rule"])  # noqa: SLF001
            is DEFAULT_BRANCH_PATTERN_RULES
        )

    def test_typeerror_iterating_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An object that raises during iteration must not crash the
        # workflow body - the helper should return the pinned defaults.
        import logging

        monkeypatch.setattr(
            automation_workflow_mod.workflow,
            "logger",
            logging.getLogger("test"),
            raising=False,
        )
        wf = AutomationWorkflow()
        assert (
            wf._coerce_rules(123)  # noqa: SLF001
            is DEFAULT_BRANCH_PATTERN_RULES
        )

    def test_pure_rule_list_passes_through(self) -> None:
        wf = AutomationWorkflow()
        rules = (
            BranchPatternRule(
                glob="hotfix/*",
                denied_workflow_types=frozenset({"code_change_commit_only"}),
            ),
        )
        coerced = wf._coerce_rules(rules)  # noqa: SLF001
        assert coerced == rules


# ===========================================================================
# 8. ``_stop_to_output`` - early-exit envelope mapping
# ===========================================================================


class TestStopToOutput:
    def test_denied_stop_maps_to_denied_output(self) -> None:
        stop = _AutomationStop(
            decision="denied",
            workflow_type="code_change_with_test",
            summary="missing caps",
            failure_reason="missing_capability",
            missing_capabilities=("bitbucket", "execution"),
        )
        out = AutomationWorkflow._stop_to_output(stop)
        assert isinstance(out, AutomationWorkflowOutput)
        assert out.decision == "denied"
        assert out.workflow_type == "code_change_with_test"
        assert out.child_workflow_id is None
        assert out.failure_reason == "missing_capability"
        assert out.missing_capabilities == ("bitbucket", "execution")

    def test_out_of_scope_stop_maps_to_out_of_scope_output(self) -> None:
        stop = _AutomationStop(
            decision="out_of_scope",
            workflow_type="code_change_commit_only",
            summary="branch denied",
            failure_reason="branch_rule_denied",
        )
        out = AutomationWorkflow._stop_to_output(stop)
        assert out.decision == "out_of_scope"
        assert out.failure_reason == "branch_rule_denied"
        # Default empty tuple when not supplied.
        assert out.missing_capabilities == ()

    def test_failed_stop_maps_to_failed_output(self) -> None:
        stop = _AutomationStop(
            decision="failed",
            workflow_type=None,
            summary="LLM exploded",
            failure_reason="task_analysis_failed",
        )
        out = AutomationWorkflow._stop_to_output(stop)
        assert out.decision == "failed"
        assert out.workflow_type is None
        assert out.failure_reason == "task_analysis_failed"


# ===========================================================================
# 9. needs_info signal handling
# ===========================================================================
#
# The gateway has a Temporal signal handler + wait_condition so a Jira reply
# can drive a re-analysis of an ambiguous
# ``llm_analyze_task`` result.  These tests cover the pure parts of
# that path:
#
# * Signal-handler tolerance for str / dict / None payloads.
# * Pure formatter helpers used to build the Jira comments.
# * The fast-path branch of ``_handle_needs_info_loop`` that returns
#   the analysis untouched when confidence is high or no questions
#   were asked.
#
# The full async loop (signal arrival, timeout, loop cap) lives behind
# ``workflow.execute_activity`` and ``workflow.wait_condition`` and is
# covered by the integration suite (history replay).


from automation_worker.workflows.automation_workflow import (  # noqa: E402
    _NEEDS_INFO_MAX_ITERATIONS,
    _NEEDS_INFO_TIMEOUT,
    _SIGNAL_INFO_RECEIVED,
    _format_needs_info_comment,
    _format_needs_info_loop_cap_comment,
    _format_needs_info_timeout_comment,
)


class TestNeedsInfoConstants:
    """Lock the timeout / cap values so accidental edits surface."""

    def test_signal_name_matches_dispatcher_contract(self) -> None:
        # The webhook dispatcher emits ``info_received``.
        assert _SIGNAL_INFO_RECEIVED == "info_received"

    def test_timeout_is_seven_days(self) -> None:
        # Bumped from ``timedelta(hours=24)`` to ``timedelta(days=7)`` so the
        # parked window matches the Turkish prose in
        # ``_format_needs_info_timeout_comment`` ("7 gün") and the
        # sibling ``agent_runner.SIGNAL_WAIT_TIMEOUT`` constant.
        assert _NEEDS_INFO_TIMEOUT == timedelta(days=7)

    def test_max_iterations_is_three(self) -> None:
        # Mirrors AgentRunnerWorkflow ``needs_info_streak`` cap so operators
        # see consistent behaviour across pathways.
        assert _NEEDS_INFO_MAX_ITERATIONS == 3


class TestNeedsInfoFormatters:
    """The Turkish-prose formatters are user-visible and must stay
    stable across refactors."""

    def test_question_comment_lists_each_question_as_bullet(self) -> None:
        body = _format_needs_info_comment(
            ("Hangi repo?", "Hangi branch?")
        )
        assert "Hangi repo?" in body
        assert "Hangi branch?" in body
        # Bullets - render as • per the helper.
        assert body.count("•") == 2
        # The user-facing reply guidance must mention the comment loop.
        assert "yorumun altına" in body

    def test_question_comment_robust_to_empty_strings(self) -> None:
        body = _format_needs_info_comment(("", "Hangi branch?", ""))
        # Empty entries are filtered, not rendered as blank bullets.
        assert body.count("•") == 1
        assert "Hangi branch?" in body

    def test_question_comment_falls_back_when_questions_empty(
        self,
    ) -> None:
        body = _format_needs_info_comment(())
        assert "•" in body
        # Generic fallback prompt for the user.
        assert "eksik detayları" in body

    def test_timeout_comment_mentions_seven_days_and_stale(self) -> None:
        body = _format_needs_info_timeout_comment()
        # Turkish prose now mentions "7 gün" instead of "24 saat".
        assert "7 gün" in body
        assert "stale" in body
        # Hourglass marker so operators can grep for timeout events.
        assert "⌛" in body

    def test_loop_cap_comment_mentions_iteration_bound(self) -> None:
        body = _format_needs_info_loop_cap_comment()
        # Octagonal-stop marker matches the rest of the
        # "automation gave up" comments in this module.
        assert "🛑" in body
        assert "düşük güven" in body


class TestInfoReceivedSignalHandler:
    """The signal handler is a pure mutator - no Temporal primitives,
    so we can call it on a fresh instance."""

    def test_string_payload_stored_and_flag_flipped(self) -> None:
        wf = AutomationWorkflow()
        assert wf._info_received is False  # noqa: SLF001
        wf.info_received("Repo: org/foo, branch: develop")
        assert wf._info_received is True  # noqa: SLF001
        assert (
            wf._pending_comment_body  # noqa: SLF001
            == "Repo: org/foo, branch: develop"
        )
        assert wf._info_received_history == [  # noqa: SLF001
            "Repo: org/foo, branch: develop"
        ]

    def test_dict_payload_extracts_comment_body_key(self) -> None:
        # Some Temporal data converters wrap a single positional arg
        # into ``{"comment_body": "..."}``; the handler must cope.
        wf = AutomationWorkflow()
        wf.info_received({"comment_body": "ek bilgi"})
        assert wf._info_received is True  # noqa: SLF001
        assert wf._pending_comment_body == "ek bilgi"  # noqa: SLF001

    def test_none_payload_still_flips_flag_but_records_empty(
        self,
    ) -> None:
        # Empty / None bodies still flip the wait-condition edge so the
        # workflow can decide what to do (re-emit the question), but
        # nothing is appended to the history.
        wf = AutomationWorkflow()
        wf.info_received(None)
        assert wf._info_received is True  # noqa: SLF001
        assert wf._pending_comment_body == ""  # noqa: SLF001
        assert wf._info_received_history == []  # noqa: SLF001

    def test_multiple_signals_accumulate_history(self) -> None:
        wf = AutomationWorkflow()
        wf.info_received("first reply")
        wf.info_received("second reply")
        wf.info_received("third reply")
        assert wf._info_received_history == [  # noqa: SLF001
            "first reply",
            "second reply",
            "third reply",
        ]
        # Only the most recent body is exposed for re-analysis input.
        assert wf._pending_comment_body == "third reply"  # noqa: SLF001

    def test_signal_decorator_uses_info_received_name(self) -> None:
        # The Temporal SDK exposes the signal name on the bound method
        # via the ``__temporal_signal_definition`` attribute.  We
        # assert the name here so a refactor to a different attribute
        # name surfaces immediately rather than at runtime.
        wf = AutomationWorkflow()
        defn = getattr(
            wf.info_received,
            "__temporal_signal_definition",
            None,
        )
        # Some SDK versions store the definition on the underlying
        # function instead of the bound method - fall back to the
        # function attribute when the bound-method probe missed.
        if defn is None:
            defn = getattr(
                AutomationWorkflow.info_received,
                "__temporal_signal_definition",
                None,
            )
        assert defn is not None, (
            "info_received is not registered as a Temporal signal."
        )
        assert getattr(defn, "name", None) == "info_received"


class TestNeedsInfoLoopFastPath:
    """``_handle_needs_info_loop`` returns the analysis untouched when
    no needs_info handling is required.  These cases hit the early-exit
    guard so they can be exercised without any Temporal primitives."""

    @pytest.mark.asyncio
    async def test_high_confidence_passes_through(self) -> None:
        wf = AutomationWorkflow()
        analysis = LlmAnalysisResult(
            workflow_type="code_change_with_test",
            confidence="high",
            target_repo="org/repo",
            target_branch="develop",
        )
        result = await wf._handle_needs_info_loop(  # noqa: SLF001
            inp=_make_input(),
            analysis=analysis,
        )
        assert result is analysis

    @pytest.mark.asyncio
    async def test_medium_confidence_passes_through(self) -> None:
        wf = AutomationWorkflow()
        analysis = LlmAnalysisResult(
            workflow_type="code_change_with_test",
            confidence="medium",
            target_repo="org/repo",
            target_branch="develop",
            needs_info_questions=("Should we use feature flag?",),
        )
        result = await wf._handle_needs_info_loop(  # noqa: SLF001
            inp=_make_input(),
            analysis=analysis,
        )
        # Medium confidence does NOT trigger the wait - the gateway
        # proceeds to dispatch even when the LLM has open questions.
        assert result is analysis

    @pytest.mark.asyncio
    async def test_low_confidence_with_no_questions_passes_through(
        self,
    ) -> None:
        # Low confidence without clarification questions has no
        # actionable needs_info shape - falling through to dispatch
        # is the correct behaviour (the capability gate or LLM
        # workflow will surface a richer error if needed).
        wf = AutomationWorkflow()
        analysis = LlmAnalysisResult(
            workflow_type="code_change_with_test",
            confidence="low",
            needs_info_questions=(),
        )
        result = await wf._handle_needs_info_loop(  # noqa: SLF001
            inp=_make_input(),
            analysis=analysis,
        )
        assert result is analysis


class TestWithCommentAppended:
    """``_with_comment_appended`` is a pure helper - currently a
    pass-through but the contract is that calling it never throws
    and never mutates the input."""

    def test_returns_input_unchanged_for_string(self) -> None:
        inp = _make_input(issue_key="PAY-1")
        result = AutomationWorkflow._with_comment_appended(  # noqa: SLF001
            inp, "Repo: org/foo"
        )
        assert result is inp
        # Input is frozen - assert the field wasn't mutated.
        assert result.issue_key == "PAY-1"

    def test_returns_input_unchanged_for_empty_string(self) -> None:
        inp = _make_input()
        result = AutomationWorkflow._with_comment_appended(inp, "")  # noqa: SLF001
        assert result is inp


# ===========================================================================
# 10. Determinism - the new wait_condition + signal additions still obey
# the replay contract. These tests extend the AST-level
# checks at the top of the file with the new activity name and confirm
# that the only ``wait_condition`` call sits inside the needs_info
# helper (no top-level random / sleep was added).
# ===========================================================================


class TestNeedsInfoDeterminism:
    """Replay-safety checks for the needs_info additions."""

    @pytest.fixture(scope="class")
    def module_source(self) -> str:
        path = Path(automation_workflow_mod.__file__)
        return path.read_text(encoding="utf-8")

    def test_jira_transition_issue_referenced_as_string(
        self, module_source: str
    ) -> None:
        # The needs_info loop calls jira_transition_issue; like every other
        # activity, it must be referenced by name.
        assert (
            '"jira_transition_issue"' in module_source
            or "'jira_transition_issue'" in module_source
        )

    def test_only_workflow_wait_condition_is_used(
        self, module_source: str
    ) -> None:
        # ``asyncio.sleep`` / ``time.sleep`` would be replay-unsafe.
        # The needs_info loop must use ``workflow.wait_condition``
        # (the Temporal-blessed deterministic timer).
        assert "workflow.wait_condition" in module_source
        # No raw asyncio.sleep allowed.
        assert "asyncio.sleep" not in module_source
        # No top-level ``time.sleep`` allowed.
        assert "time.sleep" not in module_source

    def test_signal_decorator_present(self, module_source: str) -> None:
        # The signal handler must be registered with @workflow.signal
        # and reference the public signal name.
        assert "@workflow.signal" in module_source
        assert "info_received" in module_source
