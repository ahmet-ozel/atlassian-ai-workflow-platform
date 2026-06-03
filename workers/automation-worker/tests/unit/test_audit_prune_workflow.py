"""Unit tests for ``AuditPruneWorkflow``.

The tests exercise the workflow body **without** spinning up a Temporal
worker. Two strategies cover the surface:

* **AST/source inspection** — verifies the workflow module obeys the
  determinism contract: no
  ``datetime.now``, ``time.time``, ``random``, ``uuid``, ``os.environ``
  reads in the workflow body; activities referenced by string name
  only; no import of activity modules at workflow-module import time.

* **Direct ``run`` execution** with a fake ``temporalio.workflow``
  module patched into ``sys.modules`` so ``workflow.execute_activity``,
  ``workflow.now()`` and ``workflow.logger`` all resolve to recording
  doubles. This lets us assert the exact activity call sequence,
  retry policies, timeouts, and the mandatory failure-alarm contract
  deterministically.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors and ``sys.path`` bootstrapping
# ---------------------------------------------------------------------------
#
# The worker package ships under ``src/automation_worker/`` and is
# imported as ``automation_worker.workflows.audit_prune``. Adding the
# worker's ``src/`` directory to ``sys.path`` matches the layout used
# by ``hatchling`` (``packages = ["src/automation_worker"]``).

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------
#
# The module imports ``temporalio.workflow`` (and ``temporalio.common``)
# at import time. ``temporalio`` is a runtime dependency of the worker;
# we let it import normally — the module is import-clean even outside a
# Temporal sandbox because it never actually *invokes* workflow
# primitives at module scope. The runtime tests below replace
# ``workflow.execute_activity`` and ``workflow.now`` per-test using
# ``monkeypatch``.

# pylint: disable=wrong-import-position
from automation_worker.workflows import audit_prune as audit_prune_mod  # noqa: E402
from automation_worker.workflows.audit_prune import (  # noqa: E402
    AUDIT_PRUNE_CRON_SCHEDULE,
    AUDIT_PRUNE_WORKFLOW_ID,
    AUTOMATION_TASK_QUEUE,
    DEFAULT_RETENTION_DAYS,
    AuditArchiveResult,
    AuditDeleteResult,
    AuditPruneReport,
    AuditPruneWorkflow,
)


# ===========================================================================
# 1. Public-constant contract tests
# ===========================================================================


class TestPublicConstants:
    """The cron schedule, task queue, and workflow ID are part of the
    public contract. Cron schedule registration relies on the exact string
    values."""

    def test_task_queue_is_automation_tq(self) -> None:
        # Cron registration uses ``task_queue="automation-tq"``.
        assert AUTOMATION_TASK_QUEUE == "automation-tq"

    def test_workflow_id_is_audit_prune_cron(self) -> None:
        # Cron registration uses ``id="audit-prune-cron"``.
        assert AUDIT_PRUNE_WORKFLOW_ID == "audit-prune-cron"

    def test_cron_schedule_is_daily_at_03_00_utc(self) -> None:
        # Cron runs daily at 03:00 UTC.
        assert AUDIT_PRUNE_CRON_SCHEDULE == "0 3 * * *"

    def test_default_retention_days_is_90(self) -> None:
        # RETENTION_DAYS defaults to 90.
        assert DEFAULT_RETENTION_DAYS == 90


# ===========================================================================
# 2. Determinism contract — static (AST) checks
# ===========================================================================


class TestDeterminismStatic:
    """The workflow module body must be replay-safe: only Temporal-
    deterministic primitives are allowed. We enforce this by AST-walking
    the workflow source and rejecting forbidden symbols.
    """

    @pytest.fixture(scope="class")
    def module_source(self) -> str:
        path = Path(audit_prune_mod.__file__)
        return path.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def module_tree(self, module_source: str) -> ast.Module:
        return ast.parse(module_source)

    def test_no_datetime_now_call(self, module_tree: ast.Module) -> None:
        # ``datetime.now()`` and ``datetime.utcnow()`` are non-deterministic
        # — replay would produce different cutoffs. Only ``workflow.now()``
        # is allowed for the time source.
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # Reject ``<anything>.now()`` and ``<anything>.utcnow()``
                # *unless* the receiver is ``workflow``.
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
        # ``time.time()`` / ``time.monotonic()`` are non-deterministic.
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "time", (
                    f"Forbidden ``time`` module reference {ast.dump(node)!r}; "
                    f"use workflow.now() / workflow.sleep() instead."
                )

    def test_no_random_or_uuid(self, module_tree: ast.Module) -> None:
        # ``random.*`` and ``uuid.uuid4()`` are non-deterministic.
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id not in {"random", "uuid"}, (
                    f"Forbidden non-deterministic ID/random source "
                    f"{ast.dump(node)!r}."
                )

    def test_no_os_environ_read(self, module_tree: ast.Module) -> None:
        # ``os.environ[...]`` / ``os.getenv(...)`` would let the workflow
        # body diverge based on host configuration; env reads must
        # happen inside activities.
        for node in ast.walk(module_tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr in {
                    "environ",
                    "getenv",
                }:
                    pytest.fail(
                        f"Forbidden direct env read {ast.dump(node)!r}; "
                        f"use an activity instead."
                    )

    def test_no_activity_module_imports_at_module_scope(
        self, module_tree: ast.Module
    ) -> None:
        # The workflow must reference activities **by string name** so
        # the module loads cleanly even before ``automation_worker.
        # activities.audit_prune`` exists. Reject any import of
        # ``automation_worker.activities.*`` at module scope.
        for node in module_tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                target = (
                    node.module
                    if isinstance(node, ast.ImportFrom)
                    else node.names[0].name
                )
                assert target is None or "automation_worker.activities" not in target, (
                    f"Workflow module must not import activity modules "
                    f"at module scope: found {target!r}."
                )

    def test_activity_names_referenced_as_strings(
        self, module_source: str
    ) -> None:
        # Every activity used by the workflow must appear as a quoted
        # string literal somewhere in the source — confirms the
        # workflow uses ``execute_activity("name", ...)`` rather than a
        # callable reference.
        for activity_name in (
            "get_retention_setting",
            "archive_audit_to_minio",
            "delete_audit_older_than",
            "notify_audit_prune_failed",
        ):
            assert (
                f'"{activity_name}"' in module_source
                or f"'{activity_name}'" in module_source
            ), f"Activity {activity_name!r} not referenced as a string literal."


# ===========================================================================
# 3. Retry policy / timeout configuration
# ===========================================================================


class TestRetryPolicyConfiguration:
    """The retry policies and activity timeouts are part of the
    workflow's reliability contract. These tests assert against the
    module-level constants so a refactor that flattens them inline still
    surfaces here as a regression."""

    def test_default_retry_caps_attempts(self) -> None:
        # Three short attempts is the contract; an unlimited retry
        # would block the next day's cron tick from firing.
        policy = audit_prune_mod._DEFAULT_RETRY  # noqa: SLF001
        assert policy.maximum_attempts == 3
        assert policy.initial_interval == timedelta(seconds=1)
        assert policy.backoff_coefficient == 2.0
        assert policy.maximum_interval == timedelta(seconds=30)

    def test_lookup_retry_caps_attempts(self) -> None:
        policy = audit_prune_mod._LOOKUP_RETRY  # noqa: SLF001
        assert policy.maximum_attempts == 3
        assert policy.initial_interval == timedelta(seconds=1)
        assert policy.backoff_coefficient == 2.0

    def test_notify_retry_has_three_attempts(self) -> None:
        # The mandatory admin alarm must retry — operators cannot
        # learn about the failure unless Slack receives the message.
        policy = audit_prune_mod._NOTIFY_RETRY  # noqa: SLF001
        assert policy.maximum_attempts == 3
        assert policy.initial_interval == timedelta(seconds=1)
        assert policy.backoff_coefficient == 2.0

    def test_timeouts_match_design(self) -> None:
        # Mirrors the workflow timeout budgets.
        assert audit_prune_mod._GET_RETENTION_TIMEOUT == timedelta(seconds=10)  # noqa: SLF001
        assert audit_prune_mod._ARCHIVE_TIMEOUT == timedelta(minutes=30)  # noqa: SLF001
        assert audit_prune_mod._DELETE_TIMEOUT == timedelta(minutes=10)  # noqa: SLF001
        assert audit_prune_mod._NOTIFY_TIMEOUT == timedelta(seconds=30)  # noqa: SLF001


# ===========================================================================
# 4. ``run`` execution — activity sequence, idempotence, failure path
# ===========================================================================


@dataclass
class _ActivityCall:
    """Recorded ``workflow.execute_activity`` invocation."""

    name: str
    args: tuple[Any, ...]
    start_to_close_timeout: timedelta | None
    retry_policy: Any | None


@dataclass
class _FakeWorkflow:
    """Drop-in fake for the ``temporalio.workflow`` module.

    Replaces the three workflow primitives the audit prune workflow
    body actually uses:

    * ``workflow.now()`` — returns ``self.now_value`` exactly. Tests
      assert the cutoff is derived from this.
    * ``workflow.execute_activity(name, *args, ...)`` — records the
      call into ``self.calls`` and returns the next value from
      ``self.responses[name]`` (a list; popped left-to-right). If the
      response value is an ``Exception`` instance it is *raised*
      instead of returned, modelling activity failures.
    * ``workflow.logger`` — a no-op ``logging.Logger`` so
      ``workflow.logger.error(...)`` calls inside the workflow body
      do not blow up.

    The fake is installed onto the module under test by patching
    ``audit_prune_mod.workflow`` so the workflow code resolves the
    primitives through us instead of the real ``temporalio.workflow``.
    """

    now_value: datetime
    responses: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[_ActivityCall] = field(default_factory=list)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("test.audit_prune")
    )

    def now(self) -> datetime:  # noqa: D401 — mirror Temporal API
        return self.now_value

    async def execute_activity(
        self,
        name: str,
        *args: Any,
        start_to_close_timeout: timedelta | None = None,
        retry_policy: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        self.calls.append(
            _ActivityCall(
                name=name,
                args=tuple(args),
                start_to_close_timeout=start_to_close_timeout,
                retry_policy=retry_policy,
            )
        )
        queue = self.responses.get(name)
        if not queue:
            raise AssertionError(
                f"Unexpected activity call {name!r}; no response programmed."
            )
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.fixture
def fixed_now() -> datetime:
    """A stable, timezone-aware reference instant for the tests."""
    return datetime(2025, 6, 15, 3, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake_workflow(monkeypatch: pytest.MonkeyPatch, fixed_now: datetime) -> _FakeWorkflow:
    """Install a :class:`_FakeWorkflow` over ``audit_prune.workflow``.

    Returns the fake so individual tests can inspect ``.calls`` and
    program ``.responses``.
    """
    fake = _FakeWorkflow(now_value=fixed_now)
    monkeypatch.setattr(audit_prune_mod, "workflow", fake, raising=True)
    return fake


# ---------------------------------------------------------------------------
# Helpers for running the (async) workflow body
# ---------------------------------------------------------------------------


def _run_workflow() -> AuditPruneReport:
    """Drive ``AuditPruneWorkflow().run()`` to completion synchronously."""
    instance = AuditPruneWorkflow()
    return asyncio.run(instance.run())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    """Cron archives old ``audit_events`` to MinIO, then deletes them.

    Also covers the activity order invariant: archive must precede delete.
    """

    def test_returns_audit_prune_report_with_typed_results(
        self, fake_workflow: _FakeWorkflow, fixed_now: datetime
    ) -> None:
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [
                AuditArchiveResult(
                    archived_rows=42,
                    archive_uri="s3://audit-archive/2025/06/15/audit-1.jsonl.gz",
                )
            ],
            "delete_audit_older_than": [AuditDeleteResult(deleted_rows=42)],
        }

        report = _run_workflow()

        assert isinstance(report, AuditPruneReport)
        assert report.archived_rows == 42
        assert report.deleted_rows == 42
        assert report.retention_days == 90
        assert report.cutoff == fixed_now - timedelta(days=90)
        assert report.archive_uri.endswith("audit-1.jsonl.gz")

    def test_activity_call_order_archive_then_delete(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        # ``archive_audit_to_minio`` must run before
        # ``delete_audit_older_than`` — deleting before archiving
        # would lose audit data on a partial failure.
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [AuditArchiveResult(0, "")],
            "delete_audit_older_than": [AuditDeleteResult(0)],
        }
        _run_workflow()

        names = [c.name for c in fake_workflow.calls]
        assert names == [
            "get_retention_setting",
            "archive_audit_to_minio",
            "delete_audit_older_than",
        ]

    def test_cutoff_uses_workflow_now(
        self, fake_workflow: _FakeWorkflow, fixed_now: datetime
    ) -> None:
        # The cutoff passed to archive + delete activities must equal
        # ``workflow.now() - timedelta(days=retention_days)``.
        fake_workflow.responses = {
            "get_retention_setting": [30],
            "archive_audit_to_minio": [AuditArchiveResult(0, "")],
            "delete_audit_older_than": [AuditDeleteResult(0)],
        }
        _run_workflow()

        archive_call = next(
            c for c in fake_workflow.calls if c.name == "archive_audit_to_minio"
        )
        delete_call = next(
            c for c in fake_workflow.calls if c.name == "delete_audit_older_than"
        )
        expected_cutoff = fixed_now - timedelta(days=30)
        assert archive_call.args == (expected_cutoff,)
        assert delete_call.args == (expected_cutoff,)

    def test_activity_options_match_module_constants(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [AuditArchiveResult(0, "")],
            "delete_audit_older_than": [AuditDeleteResult(0)],
        }
        _run_workflow()

        by_name = {c.name: c for c in fake_workflow.calls}
        # noqa references silence "private member access" — these are
        # the source of truth for the activity options under test.
        assert (
            by_name["get_retention_setting"].start_to_close_timeout
            == audit_prune_mod._GET_RETENTION_TIMEOUT  # noqa: SLF001
        )
        assert (
            by_name["get_retention_setting"].retry_policy
            is audit_prune_mod._LOOKUP_RETRY  # noqa: SLF001
        )
        assert (
            by_name["archive_audit_to_minio"].start_to_close_timeout
            == audit_prune_mod._ARCHIVE_TIMEOUT  # noqa: SLF001
        )
        assert (
            by_name["archive_audit_to_minio"].retry_policy
            is audit_prune_mod._DEFAULT_RETRY  # noqa: SLF001
        )
        assert (
            by_name["delete_audit_older_than"].start_to_close_timeout
            == audit_prune_mod._DELETE_TIMEOUT  # noqa: SLF001
        )
        assert (
            by_name["delete_audit_older_than"].retry_policy
            is audit_prune_mod._DEFAULT_RETRY  # noqa: SLF001
        )

    def test_falsy_retention_falls_back_to_default(
        self, fake_workflow: _FakeWorkflow, fixed_now: datetime
    ) -> None:
        # Defensive default — a misconfigured ``get_retention_setting``
        # returning 0 / None must not silently delete the entire audit
        # log. The workflow falls back to ``DEFAULT_RETENTION_DAYS``
        # (90) instead.
        for bogus_value in (None, 0, -5):
            fake_workflow.calls.clear()
            fake_workflow.responses = {
                "get_retention_setting": [bogus_value],
                "archive_audit_to_minio": [AuditArchiveResult(0, "")],
                "delete_audit_older_than": [AuditDeleteResult(0)],
            }
            report = _run_workflow()
            assert report.retention_days == DEFAULT_RETENTION_DAYS
            assert report.cutoff == fixed_now - timedelta(
                days=DEFAULT_RETENTION_DAYS
            )

    def test_int_returning_activities_handled_gracefully(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        # Dataclass-returning activities are preferred, but an activity stub
        # returning a plain int must still produce a sensible report.
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [7],
            "delete_audit_older_than": [7],
        }
        report = _run_workflow()
        assert report.archived_rows == 7
        assert report.deleted_rows == 7
        assert report.archive_uri == ""

        fake_workflow.calls.clear()
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [
                {
                    "archived_rows": 3,
                    "archive_uri": "s3://audit-archive/2025/06/15/audit-2.jsonl.gz",
                }
            ],
            "delete_audit_older_than": [{"deleted_rows": 2}],
        }
        report = _run_workflow()
        assert report.archived_rows == 3
        assert report.deleted_rows == 2
        assert report.archive_uri.endswith("audit-2.jsonl.gz")


# ---------------------------------------------------------------------------
# Failure path — mandatory admin Slack alarm
# ---------------------------------------------------------------------------


class TestFailurePathMandatoryAlarm:
    """Any failure on the data path invokes
    ``notify_audit_prune_failed`` (mandatory admin alarm) before the
    original exception propagates."""

    def test_archive_failure_triggers_admin_alarm_and_reraises(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        injected = RuntimeError("MinIO unreachable")
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [injected],
            # delete should never be called
            "delete_audit_older_than": [AuditDeleteResult(0)],
            "notify_audit_prune_failed": [None],
        }

        with pytest.raises(RuntimeError, match="MinIO unreachable"):
            _run_workflow()

        names = [c.name for c in fake_workflow.calls]
        assert "delete_audit_older_than" not in names
        assert names[-1] == "notify_audit_prune_failed"

        notify_call = fake_workflow.calls[-1]
        assert notify_call.args == ("RuntimeError: MinIO unreachable",)
        assert (
            notify_call.start_to_close_timeout
            == audit_prune_mod._NOTIFY_TIMEOUT  # noqa: SLF001
        )
        assert notify_call.retry_policy is audit_prune_mod._NOTIFY_RETRY  # noqa: SLF001

    def test_delete_failure_triggers_admin_alarm_and_reraises(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        # If archive succeeds but delete fails (e.g. transient DB
        # outage between the two) the alarm is still mandatory and
        # the original exception still propagates.
        injected = ConnectionError("postgres timeout")
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [AuditArchiveResult(10, "s3://...")],
            "delete_audit_older_than": [injected],
            "notify_audit_prune_failed": [None],
        }

        with pytest.raises(ConnectionError, match="postgres timeout"):
            _run_workflow()

        names = [c.name for c in fake_workflow.calls]
        assert names == [
            "get_retention_setting",
            "archive_audit_to_minio",
            "delete_audit_older_than",
            "notify_audit_prune_failed",
        ]
        notify_call = fake_workflow.calls[-1]
        assert notify_call.args == ("ConnectionError: postgres timeout",)

    def test_alarm_failure_does_not_mask_original_exception(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        # Even if the alarm activity itself fails (Slack down), the
        # *original* prune exception must surface so operators can act
        # on the root cause instead of debugging the alarm path.
        original = RuntimeError("DB exploded")
        fake_workflow.responses = {
            "get_retention_setting": [90],
            "archive_audit_to_minio": [original],
            "notify_audit_prune_failed": [
                RuntimeError("Slack webhook 503")
            ],
        }

        with pytest.raises(RuntimeError, match="DB exploded"):
            _run_workflow()

    def test_get_retention_setting_failure_does_not_alarm(
        self, fake_workflow: _FakeWorkflow
    ) -> None:
        # The lookup activity is *outside* the explicit try/except in
        # the workflow body — its retry policy already gives us 3
        # attempts. If even those fail we want the original exception
        # to propagate; we deliberately do **not** alarm in this
        # phase because the workflow has not yet computed a cutoff /
        # touched any audit data, so there is no retention-side
        # corruption risk to alarm about.
        injected = RuntimeError("config-flag DB unreachable")
        fake_workflow.responses = {
            "get_retention_setting": [injected],
            # archive / delete / notify should never be called
        }

        with pytest.raises(RuntimeError, match="config-flag DB unreachable"):
            _run_workflow()

        names = [c.name for c in fake_workflow.calls]
        assert names == ["get_retention_setting"]


# ---------------------------------------------------------------------------
# Idempotent run semantics
# ---------------------------------------------------------------------------


class TestIdempotentRunSemantics:
    """A second cron tick on the same day with no new audit rows must
    be a safe no-op (zero archived, zero deleted) and must not raise.

    The workflow body itself does not implement an idempotence guard; that
    responsibility is delegated to the activities. This test verifies the
    contract the workflow expects:
    repeat calls with identical inputs produce identical reports."""

    def test_two_runs_with_zero_rows_each_succeed(
        self, fake_workflow: _FakeWorkflow, fixed_now: datetime
    ) -> None:
        # Run #1 — empty archive / empty delete.
        fake_workflow.responses = {
            "get_retention_setting": [90, 90],
            "archive_audit_to_minio": [
                AuditArchiveResult(0, ""),
                AuditArchiveResult(0, ""),
            ],
            "delete_audit_older_than": [
                AuditDeleteResult(0),
                AuditDeleteResult(0),
            ],
        }
        report1 = _run_workflow()
        report2 = _run_workflow()

        assert report1 == report2
        assert report1.archived_rows == 0
        assert report1.deleted_rows == 0
        assert report1.cutoff == fixed_now - timedelta(days=90)


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


class TestResultDataclasses:
    """The three result dataclasses are part of the activity-side
    contract, so freezing them and asserting fields here keeps the interface
    stable."""

    def test_audit_archive_result_is_frozen(self) -> None:
        result = AuditArchiveResult(archived_rows=5, archive_uri="s3://x")
        with pytest.raises(AttributeError):
            result.archived_rows = 6  # type: ignore[misc]

    def test_audit_delete_result_is_frozen(self) -> None:
        result = AuditDeleteResult(deleted_rows=5)
        with pytest.raises(AttributeError):
            result.deleted_rows = 6  # type: ignore[misc]

    def test_audit_prune_report_is_frozen(self) -> None:
        report = AuditPruneReport(
            archived_rows=1,
            deleted_rows=1,
            cutoff=datetime(2025, 1, 1, tzinfo=timezone.utc),
            retention_days=90,
            archive_uri="s3://x",
        )
        with pytest.raises(AttributeError):
            report.archived_rows = 2  # type: ignore[misc]

    def test_audit_prune_report_has_expected_fields(self) -> None:
        # The admin UI archive index reads these field names; freezing the
        # names keeps that integration safe.
        fields_set = {
            "archived_rows",
            "deleted_rows",
            "cutoff",
            "retention_days",
            "archive_uri",
        }
        # ``__dataclass_fields__`` is the canonical per-class field map.
        assert set(AuditPruneReport.__dataclass_fields__) == fields_set


# ---------------------------------------------------------------------------
# Decorator / registration smoke check
# ---------------------------------------------------------------------------


class TestWorkflowRegistration:
    """The class must be a Temporal workflow with name
    ``"AuditPruneWorkflow"`` so ``Worker(workflows=[AuditPruneWorkflow])``
    in the boot script registers it correctly."""

    def test_class_has_temporal_workflow_marker(self) -> None:
        # ``temporalio.workflow.defn`` attaches private markers to the
        # decorated class. We look for any attribute whose name starts
        # with ``__temporal`` (the SDK's namespace convention) so the
        # test stays resilient to minor SDK refactors.
        markers = [
            name for name in vars(AuditPruneWorkflow) if "temporal" in name.lower()
        ]
        assert markers, (
            "AuditPruneWorkflow must be decorated with @workflow.defn — "
            f"no temporal-namespace attributes found on the class: "
            f"{list(vars(AuditPruneWorkflow))}"
        )

    def test_run_method_is_async(self) -> None:
        import inspect

        assert inspect.iscoroutinefunction(AuditPruneWorkflow.run), (
            "AuditPruneWorkflow.run must be an async coroutine — "
            "Temporal workflows are awaited by the worker."
        )
