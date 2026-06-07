"""HTTP tests for ``POST /admin/prompts/{path:path}/sandbox-test``.

Covers the sandbox-test endpoint:

* The endpoint forwards the prompt body + sample input to
  :class:`PromptSandbox` and projects the :class:`SandboxResult`
  faithfully into the response model.
* The endpoint accepts EITHER ``body`` (raw draft body for unsaved
  edits) OR ``branch`` (read the body off a draft branch). Supplying
  both / neither is rejected with HTTP 400.
* The sandbox cost record always carries ``cost_tag="sandbox"`` so
  ``BudgetCapPolicy`` excludes it from production budget aggregates.
* Template format violations are rejected with HTTP 422 *before* the
  LLM is invoked for fast feedback to the developer.
* The endpoint is gated by ``require_admin`` like every other
  ``/admin/prompts`` route.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Bootstrap sys.path so the tests can be run via ``pytest`` directly
# from the service root without requiring ``pip install -e``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for lib_dir in (
    _WORKSPACE_ROOT / "libs" / "auth-shared" / "src",
    _WORKSPACE_ROOT / "libs" / "audit_logger" / "src",
    _WORKSPACE_ROOT / "libs" / "git-shared" / "src",
    _WORKSPACE_ROOT / "libs" / "prompts" / "src",
):
    if lib_dir.is_dir() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))


import git  # noqa: E402

from audit_logger import AuditEvent  # noqa: E402
from git_shared import GitRepo  # noqa: E402

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.prompts_git import (  # noqa: E402
    get_clock,
    get_prompt_sandbox,
    get_prompts_audit_sink,
    get_prompts_git_repo,
    get_prompts_pg_pool,
    get_prompts_pr_opener,
    router as prompts_git_router,
)
from src.sandbox import (  # noqa: E402
    CostEntryLike,
    LlmInvocationResult,
    PromptSandbox,
    SandboxResult,
)


# ---------------------------------------------------------------------------
# Fixtures + fakes
# ---------------------------------------------------------------------------


SEED_BODY = (
    "# Seed prompt\n"
    "Hello {bot_username}, departman {department_id}.\n"
)


@pytest.fixture
def repo_path(tmp_path: Path) -> Path:
    """Initialise a fresh git repo with a seeded prompt file on main."""

    target = tmp_path / "repo"
    target.mkdir()
    repo = git.Repo.init(str(target), initial_branch="main")
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Seed Author")
        cfg.set_value("user", "email", "seed@example.com")
        cfg.set_value("core", "autocrlf", "false")

    seed = target / "prompts" / "assistant_chat.md"
    seed.parent.mkdir(parents=True)
    seed.write_bytes(SEED_BODY.encode("utf-8"))
    repo.index.add([str(seed)])
    repo.index.commit("seed")
    return target


@pytest.fixture
def git_repo(repo_path: Path) -> GitRepo:
    return GitRepo(repo_path=repo_path)


class _StubAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StubPrOpener:
    """Unused by sandbox tests but required by the router wiring."""

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> Any:  # pragma: no cover - unused
        raise AssertionError("PR opener should not be called by sandbox tests")


class _RecordingLlm:
    """Capture every ``invoke`` call; return a scripted result."""

    def __init__(self, result: LlmInvocationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or LlmInvocationResult(
            response_text="hello from sandbox",
            token_in=42,
            token_out=7,
            cost_usd=Decimal("0.012345"),
            model="qwen2.5-coder",
            provider="vllm",
        )
        self._raise: Exception | None = None

    async def invoke(
        self,
        *,
        system: str,
        user: str,
        cost_tag: str,
    ) -> LlmInvocationResult:
        self.calls.append(
            {"system": system, "user": user, "cost_tag": cost_tag}
        )
        if self._raise is not None:
            raise self._raise
        return self._result


class _RecordingCostTracker:
    def __init__(self) -> None:
        self.records: list[CostEntryLike] = []

    async def record(self, entry: CostEntryLike) -> None:
        self.records.append(entry)


# ---------------------------------------------------------------------------
# Fake asyncpg pool - records the INSERT against ``prompt_sandbox_runs``
# so tests can assert the row contents without standing up Postgres.
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Asyncpg-connection-shaped fake.

    Records every ``fetchval`` call so the test can inspect the SQL,
    the bound args and the (configurable) returned id. Also exposes
    ``execute`` as a no-op for symmetry with other writers.
    """

    def __init__(
        self,
        *,
        return_id: str | None = "11111111-1111-1111-1111-111111111111",
        raise_on_fetchval: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._return_id = return_id
        self._raise = raise_on_fetchval

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if self._raise is not None:
            raise self._raise
        return self._return_id

    async def execute(self, query: str, *args: Any) -> Any:  # pragma: no cover
        self.calls.append((query, args))
        return None


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(
        self,
        *,
        connection: _FakeConnection | None = None,
    ) -> None:
        self.connection = connection or _FakeConnection()

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self.connection)


def _build_app(
    git_repo: GitRepo,
    sandbox: PromptSandbox,
    *,
    admin_claims: AuthClaims | None = None,
    prefix: str = "prompts/",
    pool: _FakePool | None = None,
    audit_sink: "_StubAuditSink | None" = None,
) -> FastAPI:
    """Wire the router with overridden dependencies."""

    app = FastAPI()
    app.include_router(prompts_git_router)
    app.state.prompts_dir_prefix = prefix

    sink = audit_sink or _StubAuditSink()
    pr_opener = _StubPrOpener()

    app.dependency_overrides[require_admin] = lambda: (
        admin_claims or AuthClaims(sub="alice", groups=("admin",))
    )
    app.dependency_overrides[get_prompts_git_repo] = lambda: git_repo
    app.dependency_overrides[get_prompts_audit_sink] = lambda: sink
    app.dependency_overrides[get_prompts_pr_opener] = lambda: pr_opener
    app.dependency_overrides[get_clock] = lambda: (lambda: 1.0)
    app.dependency_overrides[get_prompt_sandbox] = lambda: sandbox
    app.dependency_overrides[get_prompts_pg_pool] = lambda: pool
    return app


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestSandboxTestWithBody:
    def test_returns_projected_sandbox_result(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        fixed_time = datetime(2025, 3, 1, 8, 30, tzinfo=timezone.utc)
        sandbox = PromptSandbox(
            llm=llm,
            cost_tracker=tracker,
            activity_id_factory=lambda: "act-1",
            clock=lambda: fixed_time,
        )
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "explain what you do",
                "body": SEED_BODY,
                "dept_id": "payment",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["path"] == "prompts/assistant_chat.md"
        assert body["response_text"] == "hello from sandbox"
        assert body["token_in"] == 42
        assert body["token_out"] == 7
        # cost_usd is serialised as a string to preserve Decimal precision.
        assert body["cost_usd"] == "0.012345"
        assert body["invoked_at"] == fixed_time.isoformat()
        assert body["model"] == "qwen2.5-coder"
        assert body["provider"] == "vllm"
        # The isolation contract - every sandbox response carries
        # cost_tag="sandbox" so the admin UI can render the badge.
        assert body["cost_tag"] == "sandbox"

    def test_llm_invoked_with_cost_tag_sandbox(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "u-input",
                "body": "system body without placeholders",
            },
        )

        assert len(llm.calls) == 1
        call = llm.calls[0]
        assert call["system"] == "system body without placeholders"
        assert call["user"] == "u-input"
        # The cost_tag is the only knob production providers see.
        assert call["cost_tag"] == "sandbox"

    def test_cost_record_carries_sandbox_tag_and_user(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "u",
                "body": "system",
                "dept_id": "research",
            },
        )

        assert len(tracker.records) == 1
        entry = tracker.records[0]
        # The router populates user_id from the OIDC ``sub`` claim,
        # which the test fixture stubs to "alice".
        assert entry.user_id == "alice"
        assert entry.dept_id == "research"
        assert entry.cost_tag == "sandbox"
        # Sandbox runs are not Temporal workflows.
        assert entry.workflow_id is None


class TestSandboxTestWithBranch:
    def test_reads_body_from_draft_branch(
        self, git_repo: GitRepo
    ) -> None:
        # Manually create a draft branch with an updated body.
        new_body = SEED_BODY + "\nDraft addition.\n"
        git_repo.create_branch_from_main("draft/alice-100")
        git_repo.write_file(
            "prompts/assistant_chat.md", new_body, branch="draft/alice-100"
        )
        from git_shared import GitAuthor

        git_repo.commit(
            "draft/alice-100",
            message="draft commit",
            author=GitAuthor(name="alice", email="alice@example.com"),
        )

        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "user input",
                "branch": "draft/alice-100",
            },
        )

        assert response.status_code == 200, response.text
        # The LLM saw the draft branch's body, not main's.
        assert llm.calls[0]["system"] == new_body

    def test_unknown_branch_returns_404(self, git_repo: GitRepo) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "u",
                "branch": "draft/alice-9999",
            },
        )

        assert response.status_code == 404
        assert llm.calls == []

    def test_invalid_branch_shape_rejected(self, git_repo: GitRepo) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "u",
                "branch": "main",  # not a draft/* branch
            },
        )

        assert response.status_code == 400
        assert llm.calls == []


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestSandboxTestValidation:
    def test_both_body_and_branch_rejected(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={
                "sample_input": "u",
                "body": "system",
                "branch": "draft/alice-1",
            },
        )

        assert response.status_code == 400
        assert llm.calls == []

    def test_neither_body_nor_branch_rejected(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u"},
        )

        assert response.status_code == 400
        assert llm.calls == []

    def test_empty_sample_input_rejected_by_pydantic(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "", "body": "system"},
        )

        # Pydantic min_length=1 rejection lands at the validation
        # layer with HTTP 422.
        assert response.status_code == 422
        assert llm.calls == []

    def test_template_format_failure_rejected_before_llm(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        # Single ``{`` is invalid (must be escaped as ``{{``).
        bad = "Body with { unbalanced brace\n"

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": bad},
        )

        assert response.status_code == 422
        # The LLM was NOT invoked - the developer gets a fast,
        # deterministic feedback loop without paying for a sandbox
        # call that would have failed deeper in the stack.
        assert llm.calls == []
        assert tracker.records == []

    def test_traversal_path_rejected(self, git_repo: GitRepo) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        client = TestClient(_build_app(git_repo, sandbox))

        response = client.post(
            "/admin/prompts/..%2Fetc%2Fpasswd/sandbox-test",
            json={"sample_input": "u", "body": "x"},
        )

        # FastAPI may collapse the URL-encoded ``..`` before reaching
        # our handler; either way we should get a 4xx.
        assert response.status_code in (400, 404, 422)
        assert llm.calls == []


# ---------------------------------------------------------------------------
# Service-readiness
# ---------------------------------------------------------------------------


class TestSandboxNotReady:
    def test_returns_503_when_sandbox_not_wired(
        self, git_repo: GitRepo
    ) -> None:
        # Build the app *without* overriding ``get_prompt_sandbox``;
        # the dependency will raise 503 because
        # ``app.state.prompt_sandbox`` is None.
        app = FastAPI()
        app.include_router(prompts_git_router)
        app.state.prompts_dir_prefix = "prompts/"
        app.state.prompt_sandbox = None

        sink = _StubAuditSink()
        app.dependency_overrides[require_admin] = lambda: AuthClaims(
            sub="alice", groups=("admin",)
        )
        app.dependency_overrides[get_prompts_git_repo] = lambda: git_repo
        app.dependency_overrides[get_prompts_audit_sink] = lambda: sink
        app.dependency_overrides[get_prompts_pr_opener] = lambda: _StubPrOpener()
        app.dependency_overrides[get_clock] = lambda: (lambda: 1.0)

        client = TestClient(app)
        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": "system"},
        )

        assert response.status_code == 503
        assert response.json()["detail"]["reason"] == "prompt_sandbox_unavailable"


# ---------------------------------------------------------------------------
# Sandbox run persistence + audit
# ---------------------------------------------------------------------------


import hashlib  # noqa: E402  (kept adjacent to the 11.1 tests)


class TestSandboxRunPersistence:
    """The endpoint inserts every successful run into
    ``automation.prompt_sandbox_runs`` and surfaces the row id as
    ``sandbox_run_id`` (additive). A ``prompt_sandbox_run_recorded``
    audit row carries ``actor_id, prompt_path, sandbox_run_id,
    passed`` so the audit chain reflects every promote-eligible
    test.
    """

    def test_response_carries_sandbox_run_id_when_pool_present(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        pool = _FakePool(
            connection=_FakeConnection(
                return_id="22222222-2222-2222-2222-222222222222"
            )
        )
        client = TestClient(_build_app(git_repo, sandbox, pool=pool))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": SEED_BODY},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (
            body["sandbox_run_id"]
            == "22222222-2222-2222-2222-222222222222"
        )

    def test_insert_targets_prompt_sandbox_runs_with_correct_args(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        pool = _FakePool()
        client = TestClient(_build_app(git_repo, sandbox, pool=pool))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "explain", "body": SEED_BODY},
        )

        assert response.status_code == 200, response.text
        # Exactly one INSERT against prompt_sandbox_runs.
        assert len(pool.connection.calls) == 1
        sql, args = pool.connection.calls[0]
        assert "INSERT INTO automation.prompt_sandbox_runs" in sql
        assert "RETURNING id" in sql
        # Column order mirrors the migration:
        # (prompt_path, draft_branch, sample_input, prompt_body_hash,
        # response_text, token_in, token_out, cost_usd, passed, actor_id)
        assert args[0] == "prompts/assistant_chat.md"
        # Inline body  sentinel, not NULL (column is NOT NULL).
        assert args[1] == "__inline_body__"
        assert args[2] == "explain"
        assert args[3] == hashlib.sha256(SEED_BODY.encode("utf-8")).hexdigest()
        assert args[4] == "hello from sandbox"
        assert args[5] == 42  # token_in
        assert args[6] == 7   # token_out
        assert args[7] == Decimal("0.012345")
        assert args[8] is True  # passed - successful sandbox run
        assert args[9] == "alice"  # actor_id from OIDC sub

    def test_insert_carries_branch_when_branch_provided(
        self, git_repo: GitRepo
    ) -> None:
        # Seed a draft branch so the body resolves cleanly.
        new_body = SEED_BODY + "\nmore\n"
        git_repo.create_branch_from_main("draft/alice-101")
        git_repo.write_file(
            "prompts/assistant_chat.md", new_body, branch="draft/alice-101"
        )
        from git_shared import GitAuthor

        git_repo.commit(
            "draft/alice-101",
            message="draft commit",
            author=GitAuthor(name="alice", email="alice@example.com"),
        )

        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        pool = _FakePool()
        client = TestClient(_build_app(git_repo, sandbox, pool=pool))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "branch": "draft/alice-101"},
        )

        assert response.status_code == 200, response.text
        _sql, args = pool.connection.calls[0]
        # draft_branch column carries the actual branch name.
        assert args[1] == "draft/alice-101"
        # prompt_body_hash matches the draft branch body (not main).
        assert args[3] == hashlib.sha256(new_body.encode("utf-8")).hexdigest()

    def test_audit_row_emitted_with_required_payload(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        pool = _FakePool(
            connection=_FakeConnection(
                return_id="33333333-3333-3333-3333-333333333333"
            )
        )
        sink = _StubAuditSink()
        client = TestClient(
            _build_app(git_repo, sandbox, pool=pool, audit_sink=sink)
        )

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": SEED_BODY},
        )

        assert response.status_code == 200
        # Exactly one audit row is emitted by the sandbox-test path.
        actions = [e.action for e in sink.events]
        assert "prompt_sandbox_run_recorded" in actions
        recorded = next(
            e for e in sink.events if e.action == "prompt_sandbox_run_recorded"
        )
        assert recorded.actor_id == "alice"
        assert recorded.actor_role == "admin"
        assert recorded.dept_id is None
        assert recorded.resource == "prompt:prompts/assistant_chat.md"
        assert recorded.result == "ok"
        # Payload carries exactly the four expected fields.
        assert recorded.payload == {
            "actor_id": "alice",
            "prompt_path": "prompts/assistant_chat.md",
            "sandbox_run_id": "33333333-3333-3333-3333-333333333333",
            "passed": True,
        }


class TestSandboxRunPersistenceDegrades:
    """When the asyncpg pool is missing or the INSERT fails the
    endpoint still returns the LLM result - the persisted record is
    a best-effort enabler for the promote endpoint.
    """

    def test_no_pool_returns_null_sandbox_run_id(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        # Note: pool=None - the dependency override returns None and
        # the helper short-circuits.
        client = TestClient(_build_app(git_repo, sandbox, pool=None))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": SEED_BODY},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["sandbox_run_id"] is None
        # The LLM response is still the source of truth for the caller.
        assert body["response_text"] == "hello from sandbox"

    def test_audit_marked_error_when_pool_missing(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        sink = _StubAuditSink()
        client = TestClient(
            _build_app(git_repo, sandbox, pool=None, audit_sink=sink)
        )

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": SEED_BODY},
        )

        assert response.status_code == 200
        recorded = next(
            e for e in sink.events if e.action == "prompt_sandbox_run_recorded"
        )
        # Result reflects that the persisted record was not written.
        assert recorded.result == "error"
        assert recorded.payload["sandbox_run_id"] is None
        assert recorded.payload["passed"] is True

    def test_insert_failure_returns_null_run_id(
        self, git_repo: GitRepo
    ) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        pool = _FakePool(
            connection=_FakeConnection(
                raise_on_fetchval=ConnectionRefusedError("pg down")
            )
        )
        client = TestClient(_build_app(git_repo, sandbox, pool=pool))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/sandbox-test",
            json={"sample_input": "u", "body": SEED_BODY},
        )

        # Endpoint must NOT 5xx - sandbox response is still surfaced.
        assert response.status_code == 200
        assert response.json()["sandbox_run_id"] is None
