"""Unit tests for the ``iteration_manager`` activity.

Strategy
--------

The activity itself is a thin orchestration layer over four pure
helpers (``is_iterate_command``, ``extract_extra_instructions``,
``is_authorized_for_iterate``, ``build_iteration_workspace_path``)
plus an :class:`IterationStore` collaborator. The pure helpers are
exercised directly with example-level inputs; the activity is exercised
with an in-memory fake store registered through
:func:`set_iteration_store`.

The DB-backed :class:`PostgresIterationStore` is exercised via a fake
asyncpg-shaped pool so the SQL surface is covered without spinning up
Postgres.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# pylint: disable=wrong-import-position
from automation_worker.activities import iteration_manager as im  # noqa: E402
from automation_worker.activities.iteration_manager import (  # noqa: E402
    DEFAULT_WORKSPACE_BASE_PATH,
    MAX_ITERATION_NUMBER,
    IterationContext,
    IterationRecord,
    IterationStore,
    PostgresIterationStore,
    PrepareIterationInput,
    build_iteration_workspace_path,
    extract_extra_instructions,
    get_iteration_store,
    is_authorized_for_iterate,
    is_iterate_command,
    prepare_iteration,
    set_db_pool,
    set_iteration_store,
    set_workspace_base_path,
)


# ---------------------------------------------------------------------------
# In-memory fake IterationStore
# ---------------------------------------------------------------------------


@dataclass
class _InMemoryStore:
    """Hand-rolled :class:`IterationStore` implementation for unit tests."""

    rows: list[IterationRecord] = field(default_factory=list)
    raise_on_insert: BaseException | None = None

    async def latest_iteration(
        self, issue_key: str
    ) -> IterationRecord | None:
        candidates = [r for r in self.rows if r.issue_key == issue_key]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.iteration_number)

    async def insert_iteration(
        self,
        *,
        issue_key: str,
        iteration_number: int,
        workflow_id: str,
        previous_branch: str | None,
        previous_pr_id: int | None,
        workspace_path: str,
        status: str,
    ) -> None:
        if self.raise_on_insert is not None:
            raise self.raise_on_insert
        # Mimic the UNIQUE(issue_key, iteration_number) constraint.
        for existing in self.rows:
            if (
                existing.issue_key == issue_key
                and existing.iteration_number == iteration_number
            ):
                raise RuntimeError(
                    f"duplicate iteration ({issue_key}, {iteration_number})"
                )
        self.rows.append(
            IterationRecord(
                issue_key=issue_key,
                iteration_number=iteration_number,
                workflow_id=workflow_id,
                previous_branch=previous_branch,
                previous_pr_id=previous_pr_id,
                workspace_path=workspace_path,
                status=status,
                created_at=datetime.now(timezone.utc),
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Ensure each test starts with a clean module-level registry."""
    snapshot = (
        im._db_pool,  # noqa: SLF001
        im._iteration_store,  # noqa: SLF001
        im._workspace_base_path,  # noqa: SLF001
    )
    im._db_pool = None  # noqa: SLF001
    im._iteration_store = None  # noqa: SLF001
    im._workspace_base_path = DEFAULT_WORKSPACE_BASE_PATH  # noqa: SLF001
    yield
    (
        im._db_pool,  # noqa: SLF001
        im._iteration_store,  # noqa: SLF001
        im._workspace_base_path,  # noqa: SLF001
    ) = snapshot


def _make_input(
    *,
    comment_body: str = "[iterate]",
    author: str = "user-alice",
    reporter: str | None = "user-bob",
    approvers: list[str] | None = None,
    issue_key: str = "PAY-4211",
    dept_id: str = "platform",
) -> PrepareIterationInput:
    return PrepareIterationInput(
        issue_key=issue_key,
        comment_body=comment_body,
        comment_author_account_id=author,
        issue_reporter_account_id=reporter,
        dept_id=dept_id,
        dept_config={"approvers": approvers if approvers is not None else []},
        trace_id="",
    )


# ===========================================================================
# 1. Pure helpers
# ===========================================================================


class TestIsIterateCommand:
    """``[iterate]`` keyword detection."""

    @pytest.mark.parametrize(
        "body",
        [
            "[iterate]",
            "[ITERATE]",
            "[Iterate] please add backoff",
            "  [iterate]\nMore text",
            "Prefix [iterate] suffix",
        ],
    )
    def test_positive(self, body: str) -> None:
        assert is_iterate_command(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "",
            None,
            "iterate",
            "[iterat]",
            "no command here",
            "[approve]",
        ],
    )
    def test_negative(self, body: str | None) -> None:
        assert is_iterate_command(body) is False


class TestExtractExtraInstructions:
    """Extra instruction extraction."""

    def test_no_command(self) -> None:
        assert extract_extra_instructions("just a comment") is None

    def test_command_only(self) -> None:
        assert extract_extra_instructions("[iterate]") is None

    def test_command_only_with_whitespace(self) -> None:
        assert extract_extra_instructions("[iterate]   \n  ") is None

    def test_extracts_remainder(self) -> None:
        body = "[iterate] add exponential backoff to the retry helper"
        assert (
            extract_extra_instructions(body)
            == "add exponential backoff to the retry helper"
        )

    def test_case_insensitive_keyword(self) -> None:
        body = "[ITERATE] please tighten the test"
        assert (
            extract_extra_instructions(body) == "please tighten the test"
        )

    def test_first_match_wins(self) -> None:
        body = "[iterate] first chunk\n[iterate] second chunk"
        # Everything after the *first* match is preserved verbatim
        # (the second [iterate] is part of the instruction text).
        assert extract_extra_instructions(body) == (
            "first chunk\n[iterate] second chunk"
        )

    def test_none_input(self) -> None:
        assert extract_extra_instructions(None) is None


class TestIsAuthorizedForIterate:
    """Authorization predicate."""

    def test_in_approvers(self) -> None:
        assert is_authorized_for_iterate(
            author_account_id="alice",
            approvers=["alice", "bob"],
            issue_reporter_account_id="charlie",
        )

    def test_is_reporter(self) -> None:
        assert is_authorized_for_iterate(
            author_account_id="charlie",
            approvers=["alice", "bob"],
            issue_reporter_account_id="charlie",
        )

    def test_neither_approver_nor_reporter(self) -> None:
        assert not is_authorized_for_iterate(
            author_account_id="dave",
            approvers=["alice", "bob"],
            issue_reporter_account_id="charlie",
        )

    def test_empty_author_never_authorized(self) -> None:
        # Misconfigured webhook → empty actor accountId. Must reject.
        assert not is_authorized_for_iterate(
            author_account_id="",
            approvers=["alice", "bob", ""],
            issue_reporter_account_id="",
        )

    def test_empty_approvers_with_reporter_match(self) -> None:
        assert is_authorized_for_iterate(
            author_account_id="charlie",
            approvers=[],
            issue_reporter_account_id="charlie",
        )

    def test_no_reporter_falls_back_to_approvers(self) -> None:
        assert is_authorized_for_iterate(
            author_account_id="alice",
            approvers=["alice"],
            issue_reporter_account_id=None,
        )
        assert not is_authorized_for_iterate(
            author_account_id="dave",
            approvers=["alice"],
            issue_reporter_account_id=None,
        )


class TestBuildIterationWorkspacePath:
    """Workspace path builder."""

    def test_canonical_output(self) -> None:
        assert (
            build_iteration_workspace_path(
                "/var/ai-runner", "PAY-4211", 2
            )
            == "/var/ai-runner/PAY-4211/iter-2"
        )

    def test_strips_trailing_slashes(self) -> None:
        assert (
            build_iteration_workspace_path(
                "/var/ai-runner///", "PAY-1", 1
            )
            == "/var/ai-runner/PAY-1/iter-1"
        )

    def test_underscored_project_key(self) -> None:
        assert (
            build_iteration_workspace_path(
                "/runner", "OPS_CORE-12", 5
            )
            == "/runner/OPS_CORE-12/iter-5"
        )

    @pytest.mark.parametrize(
        "bad_key",
        [
            "lowercase-1",
            "MISSING_NUMBER-",
            "PROJ-",
            "PROJ-abc",
            "..",
            "../etc/passwd",
            "PROJ-1;rm -rf",
        ],
    )
    def test_rejects_invalid_issue_key(self, bad_key: str) -> None:
        with pytest.raises(ValueError):
            build_iteration_workspace_path("/x", bad_key, 1)

    @pytest.mark.parametrize("bad_iter", [0, -1, MAX_ITERATION_NUMBER + 1])
    def test_rejects_out_of_range_iteration(self, bad_iter: int) -> None:
        with pytest.raises(ValueError):
            build_iteration_workspace_path("/x", "PAY-1", bad_iter)

    def test_rejects_bool_iteration(self) -> None:
        with pytest.raises(ValueError):
            build_iteration_workspace_path("/x", "PAY-1", True)  # type: ignore[arg-type]

    def test_rejects_non_str_issue_key(self) -> None:
        with pytest.raises(ValueError):
            build_iteration_workspace_path("/x", 123, 1)  # type: ignore[arg-type]


# ===========================================================================
# 2. prepare_iteration activity — happy path
# ===========================================================================


class TestPrepareIterationHappyPath:
    """End-to-end activity contract for an authorized [iterate]."""

    def test_first_iteration_starts_at_one(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(
                    comment_body="[iterate] add retry",
                    approvers=["user-alice"],
                    author="user-alice",
                )
            )
        )

        assert result.authorized is True
        assert result.reason == ""
        assert result.iteration_number == 1
        assert result.previous_branch is None
        assert result.previous_pr_id is None
        assert result.extra_instructions == "add retry"
        assert result.dept_id == "platform"
        assert result.workspace_path.endswith("/PAY-4211/iter-1")
        assert result.workflow_id.startswith("iteration-PAY-4211-1-")
        assert result.trace_id  # auto-generated
        # Persisted exactly once with status pending.
        assert len(store.rows) == 1
        assert store.rows[0].iteration_number == 1
        assert store.rows[0].status == "pending"

    def test_subsequent_iteration_increments_and_carries_pr(self) -> None:
        store = _InMemoryStore(
            rows=[
                IterationRecord(
                    issue_key="PAY-4211",
                    iteration_number=2,
                    workflow_id="iteration-PAY-4211-2-abcd",
                    previous_branch="feature/PAY-4211-fix",
                    previous_pr_id=199,
                    workspace_path="/var/ai-runner/PAY-4211/iter-2",
                    status="completed",
                    created_at=datetime.now(timezone.utc),
                )
            ]
        )
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(
                    comment_body="[iterate] tighten error path",
                    approvers=["user-alice"],
                    author="user-alice",
                )
            )
        )

        assert result.authorized is True
        assert result.iteration_number == 3
        assert result.previous_branch == "feature/PAY-4211-fix"
        assert result.previous_pr_id == 199
        assert result.extra_instructions == "tighten error path"
        assert result.workspace_path.endswith("/PAY-4211/iter-3")
        # Workspace paths are unique across iterations.
        assert (
            result.workspace_path != store.rows[0].workspace_path
        )
        # Persistence.
        assert len(store.rows) == 2
        assert store.rows[1].iteration_number == 3
        assert store.rows[1].previous_pr_id == 199

    def test_reporter_can_iterate_without_being_approver(self) -> None:
        # Reporter authorization path.
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(
                    comment_body="[iterate]",
                    approvers=["other-user"],
                    author="user-bob",  # matches reporter
                    reporter="user-bob",
                )
            )
        )

        assert result.authorized is True
        assert result.iteration_number == 1
        assert len(store.rows) == 1

    def test_trace_id_propagated_when_supplied(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)

        inp = PrepareIterationInput(
            issue_key="PAY-4211",
            comment_body="[iterate]",
            comment_author_account_id="user-alice",
            issue_reporter_account_id="user-bob",
            dept_id="platform",
            dept_config={"approvers": ["user-alice"]},
            trace_id="trace-deadbeef",
        )

        result = asyncio.run(prepare_iteration(inp))

        assert result.authorized is True
        assert result.trace_id == "trace-deadbeef"

    def test_workspace_base_path_override_applied(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)
        set_workspace_base_path("/srv/runner")

        result = asyncio.run(
            prepare_iteration(
                _make_input(approvers=["user-alice"], author="user-alice")
            )
        )

        assert result.workspace_path == "/srv/runner/PAY-4211/iter-1"


# ===========================================================================
# 3. prepare_iteration — denial paths
# ===========================================================================


class TestPrepareIterationDenialPaths:
    """Authorization gate, persistence failures, max-iteration cap."""

    def test_unauthorized_author_returns_deny(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(
                    approvers=["alice"],
                    author="dave",  # not in approvers, not reporter
                    reporter="bob",
                )
            )
        )

        assert result.authorized is False
        assert result.reason == "not_authorized"
        assert result.iteration_number == 0
        assert result.workflow_id == ""
        assert result.workspace_path == ""
        assert result.previous_branch is None
        assert result.previous_pr_id is None
        # No persistence for an unauthorized request.
        assert store.rows == []

    def test_empty_author_denied(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(approvers=["alice"], author="")
            )
        )

        assert result.authorized is False
        assert result.reason == "not_authorized"
        assert store.rows == []

    def test_max_iteration_exceeded(self) -> None:
        store = _InMemoryStore(
            rows=[
                IterationRecord(
                    issue_key="PAY-4211",
                    iteration_number=MAX_ITERATION_NUMBER,
                    workflow_id="iteration-PAY-4211-cap-aaaa",
                    previous_branch=None,
                    previous_pr_id=None,
                    workspace_path=(
                        f"/var/ai-runner/PAY-4211/iter-{MAX_ITERATION_NUMBER}"
                    ),
                    status="completed",
                    created_at=datetime.now(timezone.utc),
                )
            ]
        )
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(approvers=["user-alice"], author="user-alice")
            )
        )

        assert result.authorized is False
        assert result.reason == "max_iteration_exceeded"
        # No new row inserted.
        assert len(store.rows) == 1

    def test_insert_failure_returns_deny(self) -> None:
        store = _InMemoryStore(
            raise_on_insert=RuntimeError("unique violation"),
        )
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(approvers=["user-alice"], author="user-alice")
            )
        )

        assert result.authorized is False
        assert result.reason == "insert_failed"
        assert store.rows == []

    def test_invalid_workspace_path_returns_deny(self) -> None:
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(
            prepare_iteration(
                _make_input(
                    issue_key="not-a-jira-key",
                    approvers=["user-alice"],
                    author="user-alice",
                )
            )
        )

        assert result.authorized is False
        assert result.reason == "invalid_workspace_path"
        assert store.rows == []


# ===========================================================================
# 4. PostgresIterationStore — SQL surface
# ===========================================================================


@dataclass
class _FakeConn:
    fetchrow_response: dict[str, Any] | None = None
    executes: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    fetches: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.fetches.append((query, args))
        return self.fetchrow_response

    async def execute(self, query: str, *args: Any) -> str:
        self.executes.append((query, args))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self):  # type: ignore[no-untyped-def]
        yield self._conn


class TestPostgresIterationStore:
    """SQL contract of the default :class:`IterationStore` implementation."""

    def test_latest_iteration_returns_none_when_empty(self) -> None:
        conn = _FakeConn(fetchrow_response=None)
        store = PostgresIterationStore(pool=_FakePool(conn))
        result = asyncio.run(store.latest_iteration("PAY-4211"))
        assert result is None
        assert len(conn.fetches) == 1
        # Issue key was bound as the only parameter.
        assert conn.fetches[0][1] == ("PAY-4211",)

    def test_latest_iteration_maps_row_correctly(self) -> None:
        now = datetime.now(timezone.utc)
        conn = _FakeConn(
            fetchrow_response={
                "issue_key": "PAY-4211",
                "iteration_number": 7,
                "workflow_id": "iteration-PAY-4211-7-abcd",
                "previous_branch": "feature/PAY-4211",
                "previous_pr_id": 199,
                "workspace_path": "/var/ai-runner/PAY-4211/iter-7",
                "status": "completed",
                "created_at": now,
            }
        )
        store = PostgresIterationStore(pool=_FakePool(conn))
        result = asyncio.run(store.latest_iteration("PAY-4211"))
        assert isinstance(result, IterationRecord)
        assert result.iteration_number == 7
        assert result.previous_pr_id == 199
        assert result.previous_branch == "feature/PAY-4211"
        assert result.created_at == now

    def test_insert_iteration_emits_expected_sql(self) -> None:
        conn = _FakeConn()
        store = PostgresIterationStore(pool=_FakePool(conn))
        asyncio.run(
            store.insert_iteration(
                issue_key="PAY-4211",
                iteration_number=3,
                workflow_id="iteration-PAY-4211-3-abcd",
                previous_branch="feature/x",
                previous_pr_id=100,
                workspace_path="/var/ai-runner/PAY-4211/iter-3",
                status="pending",
            )
        )
        assert len(conn.executes) == 1
        query, params = conn.executes[0]
        assert "INSERT INTO shared.workflow_iterations" in query
        assert params == (
            "PAY-4211",
            3,
            "iteration-PAY-4211-3-abcd",
            "feature/x",
            100,
            "/var/ai-runner/PAY-4211/iter-3",
            "pending",
        )


class TestStoreResolution:
    """``get_iteration_store`` returns the override or builds a Postgres one."""

    def test_override_wins(self) -> None:
        fake = _InMemoryStore()
        set_iteration_store(fake)
        assert get_iteration_store() is fake

    def test_falls_back_to_postgres_store(self) -> None:
        conn = _FakeConn()
        set_db_pool(_FakePool(conn))
        store = get_iteration_store()
        assert isinstance(store, PostgresIterationStore)

    def test_no_store_no_pool_raises(self) -> None:
        with pytest.raises(RuntimeError):
            get_iteration_store()
