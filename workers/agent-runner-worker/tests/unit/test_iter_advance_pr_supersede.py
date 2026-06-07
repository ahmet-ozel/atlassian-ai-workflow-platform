"""Unit tests for ``iter_advance_pr_supersede`` activity.

Validates the contract documented on
:func:`agent_runner.activities.iter_advance.iter_advance_pr_supersede`:

* No-op when ``old_pr_id is None`` (first iteration of a fresh
  issue) - neither Bitbucket nor the supersede ledger is touched.
* Open old PR → label add + banner prepend + ledger insert all fire.
* Closed / merged old PR → Bitbucket calls are skipped, ledger row
  is still recorded (audit trail invariant).
* Idempotent: a second call for the same
  ``(workflow_id, old_pr_id, new_pr_id)`` triple is a no-op:

  * the existing label is silently re-added (Bitbucket-side
    idempotency - 409 is treated as success);
  * the description-prepend is short-circuited because the banner
    is already present;
  * the ledger row insert returns ``False`` (PK conflict).

The HTTP layer is mocked at the ``httpx.AsyncClient.post`` boundary
so the activity body runs end-to-end with realistic response codes
and bodies.

"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap - mirror the existing unit-test pattern.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"
_HTTP_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "http_shared" / "src"

for _candidate in (
    _SRC_DIR,
    _TEMPORAL_SHARED_SRC,
    _MCP_CLIENT_SRC,
    _HTTP_SHARED_SRC,
):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

# noqa: E402 - imports after sys.path bootstrap.

from agent_runner.activities import iter_advance as iter_advance_mod  # noqa: E402
from agent_runner.activities.iter_advance import (  # noqa: E402
    BANNER_PREFIX_TEMPLATE,
    SUPERSEDE_LABEL_TEMPLATE,
    IterAdvanceResult,
    RepoRef,
    _build_banner,
    _description_already_banners,
    iter_advance_pr_supersede,
    set_pr_supersede_log_repo,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeAuthClient:
    """Minimal authenticated MCP client double.

    Records the ``(path, json_payload)`` pairs the activity sends and
    returns a configurable :class:`httpx.Response` for each path.
    """

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, json: dict[str, Any]):
        self.calls.append((path, dict(json)))
        try:
            return self._responses[path]
        except KeyError as exc:
            raise AssertionError(
                f"unexpected POST to {path!r}; "
                f"available routes={sorted(self._responses)}"
            ) from exc


def _make_response(
    status_code: int, body: dict[str, Any] | None = None
) -> httpx.Response:
    """Construct a fully-realised :class:`httpx.Response` for tests."""

    return httpx.Response(
        status_code,
        json=body if body is not None else {},
        request=httpx.Request("POST", "http://mcp/test"),
    )


@pytest.fixture
def patch_mcp(monkeypatch: pytest.MonkeyPatch):
    """Replace ``make_mcp_client`` + ``with_atlassian_creds`` with fakes.

    The fixture returns the :class:`_FakeAuthClient` so each test
    can pre-load its expected responses and assert on the recorded
    call log.
    """

    auth_client = _FakeAuthClient(responses={})

    @asynccontextmanager
    async def _fake_with_atlassian_creds(
        client, *, dept_id, service, credential_resolver
    ):
        # The activity uses ``async with with_atlassian_creds(...)``
        # to mint creds; we just yield the pre-built fake client.
        del client, dept_id, service, credential_resolver
        yield auth_client

    class _FakeMcpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def _fake_make_mcp_client(*args, **kwargs):
        del args, kwargs
        return _FakeMcpClient()

    monkeypatch.setattr(
        iter_advance_mod, "make_mcp_client", _fake_make_mcp_client
    )
    monkeypatch.setattr(
        iter_advance_mod,
        "with_atlassian_creds",
        _fake_with_atlassian_creds,
    )

    # The activity also looks up a credential resolver via the legacy
    # ``src.activities`` registry. Patch that lookup so the activity
    # body does not blow up trying to import it in the test env.
    monkeypatch.setattr(
        iter_advance_mod,
        "_get_credential_resolver",
        lambda: object(),
    )

    return auth_client


@pytest.fixture
def fake_repo() -> RepoRef:
    return RepoRef(workspace="example-co", repo_slug="payment-callbacks")


@pytest.fixture
def repo_record_log() -> list[tuple[str, int, int]]:
    """Capture every :meth:`PrSupersedeLogRepo.record` invocation.

    The tests configure :func:`set_pr_supersede_log_repo` with a
    duck-typed object whose ``record`` async method appends to this
    list. The first call returns ``True`` (fresh insert), every
    subsequent call returns ``False`` (idempotent PK conflict) so
    the ``log_row_inserted`` invariant can be asserted across
    repeated runs.
    """

    log: list[tuple[str, int, int]] = []

    class _FakeRepo:
        async def record(
            self,
            workflow_id: str,
            old_pr_id: int,
            new_pr_id: int,
        ) -> bool:
            # PK = (workflow_id, old_pr_id) - the new_pr_id is not part
            # of the key, so a retried iter_advance with the same
            # (workflow_id, old_pr_id) returns False even when
            # new_pr_id differs (which mirrors the real repo's
            # ``ON CONFLICT (workflow_id, old_pr_id) DO NOTHING``
            # behaviour).
            already_seen = any(
                entry[0] == workflow_id and entry[1] == old_pr_id
                for entry in log
            )
            log.append((workflow_id, old_pr_id, new_pr_id))
            return not already_seen

    set_pr_supersede_log_repo(_FakeRepo())
    yield log
    # Reset so other tests do not see this fixture's repo.
    set_pr_supersede_log_repo(None)


# ---------------------------------------------------------------------------
# 1. Pure helpers - banner build + idempotency guard
# ---------------------------------------------------------------------------


class TestPureHelpers:
    """Unit tests for the pure helpers exposed alongside the activity."""

    def test_banner_template_carries_new_pr_id(self) -> None:
        banner = _build_banner(127)
        assert "PR #127" in banner
        assert banner.endswith("\n\n")
        # Match the exact template wording from the design doc.
        assert "yeni iterasyon" in banner

    def test_banner_template_constants_match_format(self) -> None:
        # The constant exposes ``{new_pr_id}`` so external auditors
        # can grep the production source for the literal banner text.
        assert "{new_pr_id}" in BANNER_PREFIX_TEMPLATE
        assert "{new_pr_id}" in SUPERSEDE_LABEL_TEMPLATE

    def test_idempotency_guard_detects_existing_banner(self) -> None:
        existing = _build_banner(127) + "Original description body"
        assert _description_already_banners(existing, 127) is True

    def test_idempotency_guard_ignores_other_pr_ids(self) -> None:
        existing = _build_banner(99) + "Original description body"
        assert _description_already_banners(existing, 127) is False

    def test_idempotency_guard_handles_empty_description(self) -> None:
        assert _description_already_banners("", 127) is False


# ---------------------------------------------------------------------------
# 2. ``old_pr_id is None`` - first-iteration no-op
# ---------------------------------------------------------------------------


class TestNoOpWhenNoOldPr:
    """``old_pr_id=None`` → activity returns immediately without HTTP."""

    def test_returns_no_op_result(
        self, patch_mcp: _FakeAuthClient, fake_repo: RepoRef
    ) -> None:
        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="automation-bb-payment-callbacks-pr-127",
                old_pr_id=None,
                new_pr_id=127,
                dept_id="payments",
            )
        )

        assert isinstance(result, IterAdvanceResult)
        assert result.superseded is False
        assert result.label_added is False
        assert result.description_updated is False
        assert result.log_row_inserted is False

    def test_no_http_calls_issued(
        self, patch_mcp: _FakeAuthClient, fake_repo: RepoRef
    ) -> None:
        asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-x",
                old_pr_id=None,
                new_pr_id=42,
                dept_id="payments",
            )
        )
        assert patch_mcp.calls == []

    def test_no_ledger_write(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-y",
                old_pr_id=None,
                new_pr_id=42,
                dept_id="payments",
            )
        )
        assert repo_record_log == []


# ---------------------------------------------------------------------------
# 3. Closed / merged old PR - Bitbucket no-op, ledger still records
# ---------------------------------------------------------------------------


class TestClosedOldPrSkipsBitbucket:
    """Non-OPEN states skip label + banner; ledger still records."""

    @pytest.mark.parametrize("state", ["MERGED", "DECLINED", "CLOSED"])
    def test_no_label_or_description_calls(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
        state: str,
    ) -> None:
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200, {"state": state, "description": "old body"}
            ),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="automation-bb-x-pr-200",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )

        # Only the GET fired - no label add, no description update.
        called_paths = [p for p, _ in patch_mcp.calls]
        assert called_paths == ["/api/bitbucket/pull-requests/get"]
        assert result.label_added is False
        assert result.description_updated is False
        assert result.superseded is False

        # Ledger row still recorded - audit trail invariant.
        assert len(repo_record_log) == 1
        assert repo_record_log[0] == ("automation-bb-x-pr-200", 199, 200)
        assert result.log_row_inserted is True

    def test_404_on_get_collapses_to_closed(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        """Missing PR → treated as already-closed; activity continues."""

        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(404),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-404",
                old_pr_id=99,
                new_pr_id=100,
                dept_id="payments",
            )
        )

        # No label or description PUT.
        called_paths = [p for p, _ in patch_mcp.calls]
        assert "/api/bitbucket/pull-requests/labels" not in called_paths
        assert "/api/bitbucket/pull-requests/update" not in called_paths
        # Ledger insert still fires.
        assert result.log_row_inserted is True
        assert result.superseded is False


# ---------------------------------------------------------------------------
# 4. Open old PR happy path
# ---------------------------------------------------------------------------


class TestOpenOldPrHappyPath:
    """Open old PR → label + banner prepend + ledger insert all fire."""

    def test_label_added_and_description_prepended(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200,
                {
                    "state": "OPEN",
                    "description": "Original PR body explanation.",
                },
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(200),
            "/api/bitbucket/pull-requests/update": _make_response(200),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="automation-bb-payment-callbacks-pr-200",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )

        # All three Bitbucket endpoints invoked.
        called_paths = [p for p, _ in patch_mcp.calls]
        assert called_paths == [
            "/api/bitbucket/pull-requests/get",
            "/api/bitbucket/pull-requests/labels",
            "/api/bitbucket/pull-requests/update",
        ]
        # Label payload carries the spec-pinned format.
        label_call = patch_mcp.calls[1][1]
        assert label_call["label"] == "superseded-by-pr-200"
        assert label_call["pr_id"] == 199
        # Description payload prepends the banner verbatim.
        desc_call = patch_mcp.calls[2][1]
        assert desc_call["description"].startswith(
            _build_banner(200)
        )
        assert "Original PR body explanation." in desc_call["description"]

        assert result.label_added is True
        assert result.description_updated is True
        assert result.superseded is True
        assert result.log_row_inserted is True

        # Ledger row recorded.
        assert repo_record_log == [
            ("automation-bb-payment-callbacks-pr-200", 199, 200)
        ]

    def test_label_409_treated_as_idempotent_success(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        """409 on label add (label already present) → still counts."""

        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200, {"state": "OPEN", "description": "body"}
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(409),
            "/api/bitbucket/pull-requests/update": _make_response(200),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-409",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )

        assert result.label_added is True
        assert result.superseded is True


# ---------------------------------------------------------------------------
# 5. Idempotency - second call for the same triple is a no-op
# ---------------------------------------------------------------------------


class TestIdempotentSecondCall:
    """A retried call for the same triple performs no observable change."""

    def test_second_call_skips_description_when_banner_present(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        # Simulate the second call: the GET now returns a description
        # that already starts with the supersede banner (as written by
        # a previous successful run). The activity must NOT issue a
        # second description PUT - that would yield a doubly-prefixed
        # body.
        existing = _build_banner(200) + "Original description body"
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200, {"state": "OPEN", "description": existing}
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(200),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-idem",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )

        called_paths = [p for p, _ in patch_mcp.calls]
        # Only get + labels - NO update.
        assert called_paths == [
            "/api/bitbucket/pull-requests/get",
            "/api/bitbucket/pull-requests/labels",
        ]
        assert result.description_updated is False
        # Label add still fires (Bitbucket-side idempotent).
        assert result.label_added is True
        # Superseded reflects "something happened" - label add counts.
        assert result.superseded is True

    def test_two_consecutive_calls_record_one_ledger_row(
        self,
        patch_mcp: _FakeAuthClient,
        fake_repo: RepoRef,
        repo_record_log: list[tuple[str, int, int]],
    ) -> None:
        # First call: open PR with no banner yet.
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200, {"state": "OPEN", "description": "body"}
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(200),
            "/api/bitbucket/pull-requests/update": _make_response(200),
        }
        first = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-idem-2",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )
        assert first.log_row_inserted is True

        # Second call - same triple, banner now in place upstream.
        patch_mcp.calls = []  # reset call recorder
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200,
                {
                    "state": "OPEN",
                    "description": _build_banner(200) + "body",
                },
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(200),
        }
        second = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-idem-2",
                old_pr_id=199,
                new_pr_id=200,
                dept_id="payments",
            )
        )
        # Second ledger insert is a PK conflict → ``False``.
        assert second.log_row_inserted is False
        # Description update did NOT fire (banner already present).
        assert second.description_updated is False

        # Exactly one ledger row recorded across both calls.
        assert len(repo_record_log) == 2  # repo records both attempts
        # …but the ``insert`` outcome reflects that only the first
        # actually mutated the table:
        assert first.log_row_inserted is True
        assert second.log_row_inserted is False


# ---------------------------------------------------------------------------
# 6. Repo registry isolation
# ---------------------------------------------------------------------------


class TestRepoRegistryIsolation:
    """When no repo is wired the activity still completes (best-effort)."""

    def test_no_repo_registered_yields_log_row_inserted_false(
        self, patch_mcp: _FakeAuthClient, fake_repo: RepoRef
    ) -> None:
        # Explicitly clear the registry - no fixture configures it.
        set_pr_supersede_log_repo(None)
        patch_mcp._responses = {
            "/api/bitbucket/pull-requests/get": _make_response(
                200, {"state": "OPEN", "description": "body"}
            ),
            "/api/bitbucket/pull-requests/labels": _make_response(200),
            "/api/bitbucket/pull-requests/update": _make_response(200),
        }

        result = asyncio.run(
            iter_advance_pr_supersede(
                fake_repo,
                workflow_id="wf-no-repo",
                old_pr_id=10,
                new_pr_id=11,
                dept_id="payments",
            )
        )

        # Bitbucket side-effects still happened …
        assert result.label_added is True
        assert result.description_updated is True
        # … but the ledger insert reports False (no repo wired).
        assert result.log_row_inserted is False


# ---------------------------------------------------------------------------
# 7. Workflow wiring - `_handle_code_change_with_test` invokes the activity
# ---------------------------------------------------------------------------


class TestWorkflowWiring:
    """``AgentRunnerWorkflow._handle_code_change_with_test`` calls the activity.

    Drives the same body method exercised by
    ``test_agent_runner_code_change.TestCodeChangeWithTest`` but with
    ``_previous_pr_id`` pre-set, so the supersede activity dispatch
    path is exercised end-to-end.
    """

    def test_iter_advance_invoked_with_previous_pr_id(self) -> None:
        from datetime import datetime, timezone
        from unittest.mock import patch

        from temporalio import workflow as _wf

        from agent_runner.workflows.agent_runner_workflow import (
            AgentRunnerWorkflow,
        )
        from temporal_shared.messages import (
            AgentRunnerWorkflowInput,
            LlmAnalysisResult,
        )

        wf = AgentRunnerWorkflow()
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=2)
        wf._previous_pr_id = 199  # iter-(N-1) PR id

        inp = AgentRunnerWorkflowInput(
            parent_workflow_id="automation-jira-PAY-4211",
            issue_key="PAY-4211",
            department_id="payments",
            workflow_type="code_change_with_test",
            analysis=LlmAnalysisResult(
                workflow_type="code_change_with_test",
                confidence="high",
                target_repo="payment-callbacks",
                target_branch="ai/PAY-4211",
                title="Iter 2",
                rationale="follow-up",
                token_usage=120,
            ),
            target_repo="payment-callbacks",
            target_branch="ai/PAY-4211",
            iteration=2,
            max_iter=5,
            default_language="tr",
        )

        activity_calls: list[tuple[str, list[Any]]] = []

        async def _fake_execute_activity(*args, **kwargs):
            name = args[0]
            activity_calls.append((name, kwargs.get("args") or list(args[1:])))
            if name == "opencode_generate_code":
                return {
                    "files": [
                        {
                            "path": "src/payment_retry.py",
                            "content": "def retry_enabled():\n    return True\n",
                            "action": "update",
                        }
                    ]
                }
            if name == "precommit_scanner":
                return {"decision": "pass", "matched_patterns": []}
            if name == "bitbucket_create_branch":
                return {"name": "ai/PAY-4211/iter-2"}
            if name == "bitbucket_commit_via_git":
                return {"commit_hash": "abc123", "branch": "ai/PAY-4211", "message": "[bot]"}
            if name == "bitbucket_create_pull_request_cloud":
                return {
                    "id": 200,
                    "title": "[bot]",
                    "url": "https://example.com/pr/200",
                    "draft": True,
                }
            if name == "iter_advance_pr_supersede":
                return {
                    "superseded": True,
                    "label_added": True,
                    "description_updated": True,
                    "log_row_inserted": True,
                }
            return None

        async def _fake_child(*args, **kwargs):
            return type("X", (), {"status": "passed"})()

        info_stub = type(
            "I", (), {"workflow_id": "automation-jira-PAY-4211"}
        )()

        async def _drive() -> None:
            with patch.object(
                _wf, "execute_activity", _fake_execute_activity
            ), patch.object(
                _wf, "execute_child_workflow", _fake_child
            ), patch.object(
                _wf,
                "info",
                lambda: info_stub,
            ), patch.object(
                _wf,
                "now",
                lambda: datetime(2026, 5, 14, tzinfo=timezone.utc),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        # The supersede activity fired with the right args.
        supersede_calls = [
            args for name, args in activity_calls
            if name == "iter_advance_pr_supersede"
        ]
        assert len(supersede_calls) == 1
        # Args order: [repo_dict, workflow_id, old_pr_id, new_pr_id, dept_id]
        repo_dict, workflow_id, old_pr_id, new_pr_id, dept_id = (
            supersede_calls[0]
        )
        assert repo_dict["repo_slug"] == "payment-callbacks"
        assert workflow_id == "automation-jira-PAY-4211"
        assert old_pr_id == 199
        assert new_pr_id == 200
        assert dept_id == "payments"

        # Workflow state advanced to the new PR id.
        assert wf._previous_pr_id == 200

    def test_iter_advance_skipped_when_no_previous_pr(self) -> None:
        """First iteration → ``_previous_pr_id is None`` → no activity call."""

        from datetime import datetime, timezone
        from unittest.mock import patch

        from temporalio import workflow as _wf

        from agent_runner.workflows.agent_runner_workflow import (
            AgentRunnerWorkflow,
        )
        from temporal_shared.messages import (
            AgentRunnerWorkflowInput,
            LlmAnalysisResult,
        )

        wf = AgentRunnerWorkflow()
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        # ``_previous_pr_id`` left as ``None`` (default).

        inp = AgentRunnerWorkflowInput(
            parent_workflow_id="automation-jira-PAY-4211",
            issue_key="PAY-4211",
            department_id="payments",
            workflow_type="code_change_with_test",
            analysis=LlmAnalysisResult(
                workflow_type="code_change_with_test",
                confidence="high",
                target_repo="payment-callbacks",
                target_branch="ai/PAY-4211",
                title="Iter 1",
                rationale="initial",
                token_usage=120,
            ),
            target_repo="payment-callbacks",
            target_branch="ai/PAY-4211",
            iteration=1,
            max_iter=5,
            default_language="tr",
        )

        activity_calls: list[str] = []

        async def _fake_execute_activity(*args, **kwargs):
            name = args[0]
            activity_calls.append(name)
            if name == "opencode_generate_code":
                return {
                    "files": [
                        {
                            "path": "src/payment_retry.py",
                            "content": "def retry_enabled():\n    return True\n",
                            "action": "update",
                        }
                    ]
                }
            if name == "precommit_scanner":
                return {"decision": "pass", "matched_patterns": []}
            if name == "bitbucket_create_branch":
                return {"name": "ai/PAY-4211/iter-1"}
            if name == "bitbucket_commit_via_git":
                return {"commit_hash": "abc123", "branch": "ai/PAY-4211", "message": "[bot]"}
            if name == "bitbucket_create_pull_request_cloud":
                return {
                    "id": 100,
                    "title": "[bot]",
                    "url": "https://example.com/pr/100",
                    "draft": True,
                }
            return None

        async def _fake_child(*args, **kwargs):
            return type("X", (), {"status": "passed"})()

        info_stub = type(
            "I", (), {"workflow_id": "automation-jira-PAY-4211"}
        )()

        async def _drive() -> None:
            with patch.object(
                _wf, "execute_activity", _fake_execute_activity
            ), patch.object(
                _wf, "execute_child_workflow", _fake_child
            ), patch.object(
                _wf,
                "info",
                lambda: info_stub,
            ), patch.object(
                _wf,
                "now",
                lambda: datetime(2026, 5, 14, tzinfo=timezone.utc),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        # No supersede call was issued.
        assert "iter_advance_pr_supersede" not in activity_calls
        # But the new PR id is now tracked for the *next* iteration.
        assert wf._previous_pr_id == 100
