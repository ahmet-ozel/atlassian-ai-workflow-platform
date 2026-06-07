"""FastAPI router tests for ``src.routers.prompts_git``.

These tests wire the PromptsGitRouter into a throwaway FastAPI app
with a real on-disk git repository (created in a ``tmp_path`` fixture)
and a stub :class:`PullRequestOpener`. The goal is to verify the
router-level glue end-to-end:

* Listing / reading / drafting / PR-opening exercise the router contract.
* ``validate_template_format`` rejects bodies with unbalanced braces
  or unknown placeholders before any git mutation runs.
* Path-traversal attempts are rejected at the API boundary.
* Audit events are emitted for every mutation outcome
  (``prompt_draft_created``, ``prompt_pr_opened``,
  ``prompt_render_failed``, ``prompt_pr_conflict``).

The tests do **not** monkey-patch the underlying ``GitRepo`` - they
construct a real one against a fresh repo. This catches regressions
in the GitPython integration layer without relying on heavy mocks.
"""

from __future__ import annotations

import sys
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
from git_shared import (  # noqa: E402
    GitRepo,
    MergeConflictError,
    PullRequestError,
    PullRequestRef,
)

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.prompts_git import (  # noqa: E402
    get_clock,
    get_prompts_audit_sink,
    get_prompts_git_repo,
    get_prompts_pr_opener,
    router as prompts_git_router,
)


# ---------------------------------------------------------------------------
# Fixtures
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
    """Capture audit events emitted by the router."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StubPrOpener:
    """Stand-in for the Bitbucket PR opener."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_response: PullRequestRef | None = None
        self._raise: Exception | None = None

    def set_next_response(self, ref: PullRequestRef) -> None:
        self._next_response = ref

    def set_next_error(self, exc: Exception) -> None:
        self._raise = exc

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequestRef:
        self.calls.append(
            {
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            }
        )
        if self._raise is not None:
            raise self._raise
        if self._next_response is None:
            return PullRequestRef(
                provider="stub",
                id="42",
                url="https://bitbucket.example/pr/42",
                source_branch=source_branch,
                target_branch=target_branch,
            )
        return self._next_response


def _build_app(
    git_repo: GitRepo,
    audit_sink: _StubAuditSink,
    pr_opener: _StubPrOpener,
    *,
    admin_claims: AuthClaims | None = None,
    fixed_clock: float = 1_700_000_000.0,
    prefix: str = "prompts/",
) -> FastAPI:
    """Wire the router with overridden dependencies."""

    app = FastAPI()
    app.include_router(prompts_git_router)
    app.state.prompts_dir_prefix = prefix

    app.dependency_overrides[require_admin] = lambda: (
        admin_claims or AuthClaims(sub="alice", groups=("admin",))
    )
    app.dependency_overrides[get_prompts_git_repo] = lambda: git_repo
    app.dependency_overrides[get_prompts_audit_sink] = lambda: audit_sink
    app.dependency_overrides[get_prompts_pr_opener] = lambda: pr_opener
    app.dependency_overrides[get_clock] = lambda: (lambda: fixed_clock)
    return app


# ---------------------------------------------------------------------------
# GET /admin/prompts
# ---------------------------------------------------------------------------


class TestListPrompts:
    def test_lists_seeded_prompt(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.get("/admin/prompts")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"] == [
            {
                "path": "prompts/assistant_chat.md",
                "commit_hash": pytest.approx(  # type: ignore[arg-type]
                    body["items"][0]["commit_hash"]
                ),
                "size_bytes": None,
            }
        ]
        # commit_hash is a 7-char short SHA.
        assert len(body["items"][0]["commit_hash"]) == 7

    def test_filters_by_prefix(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        # Use a prefix that excludes the seeded file.
        client = TestClient(
            _build_app(git_repo, sink, pr_opener, prefix="other/")
        )

        response = client.get("/admin/prompts")

        assert response.status_code == 200
        assert response.json() == {"items": []}


# ---------------------------------------------------------------------------
# GET /admin/prompts/{path}
# ---------------------------------------------------------------------------


class TestReadPrompt:
    def test_reads_seed_body(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.get("/admin/prompts/prompts/assistant_chat.md")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["path"] == "prompts/assistant_chat.md"
        assert body["body"] == SEED_BODY
        assert body["branch"] == "main"
        assert len(body["commit_hash"]) == 7

    def test_missing_path_returns_404(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.get("/admin/prompts/does-not-exist.md")

        assert response.status_code == 404

    def test_traversal_path_rejected(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.get("/admin/prompts/..%2Fetc%2Fpasswd")

        # FastAPI may collapse the URL-encoded ``..`` before reaching
        # our handler; either way we should get a 4xx (400 from our
        # validator or 404 from the missing file).
        assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path}/draft
# ---------------------------------------------------------------------------


class TestCreateDraft:
    def test_happy_path(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        new_body = (
            "# Seed prompt\n"
            "Hello {bot_username}, departman {department_id}.\n"
            "Yeni satır eklendi.\n"
        )

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/draft",
            json={"body": new_body},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["path"] == "prompts/assistant_chat.md"
        assert body["branch"].startswith("draft/alice-")
        assert len(body["short_hash"]) == 7

        # The change is on the draft branch only - main is unchanged.
        assert (
            git_repo.read_file(
                "prompts/assistant_chat.md", branch="main"
            )
            == SEED_BODY
        )
        assert (
            git_repo.read_file(
                "prompts/assistant_chat.md", branch=body["branch"]
            )
            == new_body
        )

        # Audit row recorded.
        assert len(sink.events) == 1
        evt = sink.events[0]
        assert evt.action == "prompt_draft_created"
        assert evt.actor_id == "alice"
        assert evt.actor_role == "admin"
        assert evt.result == "ok"
        assert evt.payload["branch"] == body["branch"]

    def test_template_format_failure_emits_render_failed(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        # Single ``{`` is invalid (must be escaped as ``{{``).
        bad_body = "# Bad prompt\nthis is { unbalanced\n"

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/draft",
            json={"body": bad_body},
        )

        assert response.status_code == 422, response.text
        # No git mutation was performed - main is intact and no
        # ``draft/`` branch was created.
        assert git_repo.read_file(
            "prompts/assistant_chat.md", branch="main"
        ) == SEED_BODY
        assert not any(
            head.name.startswith("draft/")
            for head in git_repo._repo.heads  # type: ignore[attr-defined]
        )

        # ``prompt_render_failed`` audit row was emitted.
        assert len(sink.events) == 1
        evt = sink.events[0]
        assert evt.action == "prompt_render_failed"
        assert evt.result == "error"

    def test_unknown_placeholder_rejected(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        # ``{unknown_var}`` is rejected by validate_template_format.
        bad_body = "# Bad prompt\n{unknown_var} should not pass.\n"

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/draft",
            json={"body": bad_body},
        )

        assert response.status_code == 422
        assert sink.events[0].action == "prompt_render_failed"

    def test_missing_path_returns_404(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.post(
            "/admin/prompts/prompts/new.md/draft",
            json={"body": SEED_BODY},
        )

        assert response.status_code == 404
        assert sink.events == []

    def test_oversized_body_rejected(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        big = "x" * (64 * 1024 + 1)
        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/draft",
            json={"body": big},
        )

        assert response.status_code == 413


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path}/pr
# ---------------------------------------------------------------------------


class TestOpenPr:
    def _make_draft(
        self,
        git_repo: GitRepo,
        client: TestClient,
        body: str,
    ) -> str:
        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/draft",
            json={"body": body},
        )
        assert response.status_code == 201, response.text
        return response.json()["branch"]

    def test_happy_path(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        new_body = SEED_BODY + "extra\n"
        branch = self._make_draft(git_repo, client, new_body)

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/pr",
            json={"branch": branch},
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["pr_id"] == "42"
        assert body["source_branch"] == branch
        assert body["target_branch"] == "main"
        assert body["pr_url"].startswith("https://")

        # Audit row recorded for the PR open.
        assert any(e.action == "prompt_pr_opened" for e in sink.events)

        # PR opener received the title / description we expected.
        assert len(pr_opener.calls) == 1
        call = pr_opener.calls[0]
        assert call["source_branch"] == branch
        assert call["target_branch"] == "main"
        assert "Prompt change" in call["title"]
        assert "diff vs `main`" in call["description"].lower()

    def test_invalid_branch_shape_rejected(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/pr",
            json={"branch": "main"},  # not a draft/ branch
        )

        assert response.status_code == 400
        assert pr_opener.calls == []

    def test_missing_branch_returns_404(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/pr",
            json={"branch": "draft/alice-1700000000"},
        )

        assert response.status_code == 404
        assert pr_opener.calls == []

    def test_upstream_pull_request_error_returns_502(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        new_body = SEED_BODY + "extra\n"
        branch = self._make_draft(git_repo, client, new_body)

        pr_opener.set_next_error(PullRequestError("upstream down"))
        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/pr",
            json={"branch": branch},
        )

        assert response.status_code == 502

    def test_upstream_merge_conflict_returns_409(
        self,
        git_repo: GitRepo,
    ) -> None:
        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()
        client = TestClient(_build_app(git_repo, sink, pr_opener))

        new_body = SEED_BODY + "extra\n"
        branch = self._make_draft(git_repo, client, new_body)

        pr_opener.set_next_error(MergeConflictError("upstream conflict"))
        response = client.post(
            "/admin/prompts/prompts/assistant_chat.md/pr",
            json={"branch": branch},
        )

        assert response.status_code == 409
        # ``prompt_pr_conflict`` audit row was emitted.
        assert any(
            e.action == "prompt_pr_conflict" for e in sink.events
        )


# ---------------------------------------------------------------------------
# Auth gating
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    def test_router_requires_admin_dependency(
        self,
        git_repo: GitRepo,
    ) -> None:
        # The router declares ``Depends(require_admin)`` at construction
        # time. With no override the default validator runs and rejects
        # the missing bearer token with 401.
        from src.auth.dependencies import get_validator

        sink = _StubAuditSink()
        pr_opener = _StubPrOpener()

        app = FastAPI()
        app.include_router(prompts_git_router)
        app.state.prompts_dir_prefix = "prompts/"
        app.dependency_overrides[get_prompts_git_repo] = lambda: git_repo
        app.dependency_overrides[get_prompts_audit_sink] = lambda: sink
        app.dependency_overrides[get_prompts_pr_opener] = lambda: pr_opener
        app.dependency_overrides[get_clock] = lambda: (lambda: 1.0)

        # Inject a stub validator that always rejects so we exercise
        # the auth path without standing up a real OIDC validator.
        from auth_shared import InvalidTokenError

        class _RejectingValidator:
            def validate(self, token: str) -> dict:
                raise InvalidTokenError("stub: rejected")

        app.dependency_overrides[get_validator] = _RejectingValidator

        client = TestClient(app)

        # No Authorization header  401 from require_admin's bearer
        # check (before even reaching the validator).
        response = client.get("/admin/prompts")
        assert response.status_code == 401
