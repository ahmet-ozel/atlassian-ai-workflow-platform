"""Unit tests for ``automation_service.probe``.

Covers the probe acceptance criteria:

* Read probe runs for each surface (Jira ``/myself``,
  Bitbucket ``/2.0/user``, Confluence current user). Failure aborts
  before the write probe.
* Write probe round-trips a sentinel artifact for each
  surface (Confluence draft create+delete, Bitbucket branch
  create+delete, Jira self-comment).
* Every probe call first searches the target system for
  ``_AI_PROBE_*`` artifacts and deletes them so repeated calls leave
  no extra residue (idempotency).
* Probe artifacts never carry plain-text credentials.

The Atlassian client is replaced by an in-memory fake satisfying the
:class:`AtlassianProbeClient` protocol so the tests stay hermetic.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup - make the in-tree ``src`` directory importable
# ---------------------------------------------------------------------------
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))

from automation_service.probe import (  # noqa: E402
    PROBE_ARTIFACT_PREFIX,
    PROBE_ARTIFACT_SUFFIX,
    ProbeArtifact,
    ProbeResult,
    ProbeRunner,
    ProbeTargets,
    ResolvedCredential,
    is_probe_artifact_title,
    make_probe_title,
)


# ---------------------------------------------------------------------------
# Sentinel format helpers
# ---------------------------------------------------------------------------


class TestSentinelFormat:
    """Probe artifact title format."""

    def test_make_probe_title_uses_canonical_format(self) -> None:
        title = make_probe_title(now_unix_ts=1_700_000_000)
        assert title == f"{PROBE_ARTIFACT_PREFIX}1700000000{PROBE_ARTIFACT_SUFFIX}"
        assert title.startswith(PROBE_ARTIFACT_PREFIX)
        assert title.endswith(PROBE_ARTIFACT_SUFFIX)

    def test_make_probe_title_default_clock_returns_int_seconds(self) -> None:
        title = make_probe_title()
        # Strip the prefix / suffix and verify the middle is an integer.
        ts_part = title[len(PROBE_ARTIFACT_PREFIX) : -len(PROBE_ARTIFACT_SUFFIX)]
        assert ts_part.isdigit()

    def test_is_probe_artifact_title_accepts_canonical(self) -> None:
        assert is_probe_artifact_title("_AI_PROBE_1700000000_DELETE_ME")

    def test_is_probe_artifact_title_accepts_prefix_only(self) -> None:
        # Robust against historical / human-edited variants.
        assert is_probe_artifact_title("_AI_PROBE_legacy_artifact")

    @pytest.mark.parametrize(
        "title",
        ["", "regular page title", "AI_PROBE_no_underscore", "release-1.2.3"],
    )
    def test_is_probe_artifact_title_rejects_non_probe(self, title: str) -> None:
        assert not is_probe_artifact_title(title)

    def test_is_probe_artifact_title_handles_non_string(self) -> None:
        assert not is_probe_artifact_title(123)  # type: ignore[arg-type]
        assert not is_probe_artifact_title(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fake AtlassianProbeClient
# ---------------------------------------------------------------------------


@dataclass
class _FakeAtlassianClient:
    """In-memory ``AtlassianProbeClient`` for unit tests.

    Implements the bare minimum surface needed by ``ProbeRunner`` -
    every method records its inputs in ``calls`` so assertions can
    drive off the call log instead of poking at private attributes.
    """

    # ---- Configurable behaviour ----------------------------------------
    jira_myself_payload: dict[str, Any] = field(
        default_factory=lambda: {"accountId": "jira-bot-001"}
    )
    bitbucket_user_payload: dict[str, Any] = field(
        default_factory=lambda: {"account_id": "bb-bot-001"}
    )
    confluence_user_payload: dict[str, Any] = field(
        default_factory=lambda: {"accountId": "conf-bot-001"}
    )

    jira_self_comments: list[dict[str, Any]] = field(default_factory=list)
    bitbucket_probe_branches: list[str] = field(default_factory=list)
    confluence_probe_pages: list[dict[str, Any]] = field(default_factory=list)

    # ---- Failure injection --------------------------------------------
    fail_read: str | None = None  # one of {"jira", "bitbucket", "confluence"}
    fail_write_create: str | None = None
    fail_write_delete: str | None = None

    # ---- Generated state ----------------------------------------------
    next_jira_comment_id: int = 1
    next_confluence_page_id: int = 1
    target_issue_key: str = "BOT-1"

    # ---- Call log ------------------------------------------------------
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    # ----- Jira ---------------------------------------------------------

    async def jira_myself(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("jira_myself", (cred.username,)))
        if self.fail_read == "jira":
            raise RuntimeError("auth failed")
        return self.jira_myself_payload

    async def jira_search_self_comments(
        self,
        cred: ResolvedCredential,
        author_account_id: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("jira_search_self_comments", (author_account_id,)))
        return list(self.jira_self_comments)

    async def jira_create_self_comment(
        self,
        cred: ResolvedCredential,
        body: str,
    ) -> dict[str, Any]:
        self.calls.append(("jira_create_self_comment", (body,)))
        if self.fail_write_create == "jira":
            raise RuntimeError("create failed")
        comment_id = str(self.next_jira_comment_id)
        self.next_jira_comment_id += 1
        comment = {
            "id": comment_id,
            "issue_key": self.target_issue_key,
            "body_marker": body,
        }
        # Track the live comment so cleanup tests can find it.
        self.jira_self_comments.append(comment)
        return comment

    async def jira_delete_comment(
        self,
        cred: ResolvedCredential,
        issue_key: str,
        comment_id: str,
    ) -> None:
        self.calls.append(("jira_delete_comment", (issue_key, comment_id)))
        if self.fail_write_delete == "jira":
            raise RuntimeError("delete failed")
        self.jira_self_comments = [
            c for c in self.jira_self_comments if str(c.get("id")) != comment_id
        ]

    # ----- Bitbucket ----------------------------------------------------

    async def bitbucket_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("bitbucket_user", (cred.username,)))
        if self.fail_read == "bitbucket":
            raise RuntimeError("auth failed")
        return self.bitbucket_user_payload

    async def bitbucket_list_probe_branches(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
    ) -> list[str]:
        self.calls.append(
            ("bitbucket_list_probe_branches", (workspace, repo))
        )
        return list(self.bitbucket_probe_branches)

    async def bitbucket_create_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> str:
        self.calls.append(
            ("bitbucket_create_branch", (workspace, repo, branch_name))
        )
        if self.fail_write_create == "bitbucket":
            raise RuntimeError("create failed")
        self.bitbucket_probe_branches.append(branch_name)
        return f"sha-{branch_name}"

    async def bitbucket_delete_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> None:
        self.calls.append(
            ("bitbucket_delete_branch", (workspace, repo, branch_name))
        )
        if self.fail_write_delete == "bitbucket":
            raise RuntimeError("delete failed")
        self.bitbucket_probe_branches = [
            b for b in self.bitbucket_probe_branches if b != branch_name
        ]

    # ----- Confluence ---------------------------------------------------

    async def confluence_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("confluence_user", (cred.username,)))
        if self.fail_read == "confluence":
            raise RuntimeError("auth failed")
        return self.confluence_user_payload

    async def confluence_list_probe_pages(
        self,
        cred: ResolvedCredential,
        space_key: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("confluence_list_probe_pages", (space_key,)))
        return list(self.confluence_probe_pages)

    async def confluence_create_draft_page(
        self,
        cred: ResolvedCredential,
        space_key: str,
        title: str,
    ) -> dict[str, Any]:
        self.calls.append(("confluence_create_draft_page", (space_key, title)))
        if self.fail_write_create == "confluence":
            raise RuntimeError("create failed")
        page_id = str(self.next_confluence_page_id)
        self.next_confluence_page_id += 1
        page = {"id": page_id, "title": title, "space_key": space_key}
        self.confluence_probe_pages.append(page)
        return page

    async def confluence_delete_page(
        self,
        cred: ResolvedCredential,
        page_id: str,
    ) -> None:
        self.calls.append(("confluence_delete_page", (page_id,)))
        if self.fail_write_delete == "confluence":
            raise RuntimeError("delete failed")
        self.confluence_probe_pages = [
            p for p in self.confluence_probe_pages if str(p.get("id")) != page_id
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_TOKEN_MARKER = "PLAINTEXT_TOKEN_DO_NOT_LEAK_42"


def _cred() -> ResolvedCredential:
    return ResolvedCredential(
        url="https://acme.atlassian.net",
        username="bot@acme.com",
        personal_token=_TOKEN_MARKER,
    )


def _runner(client: _FakeAtlassianClient, ts: int = 1_700_000_000) -> ProbeRunner:
    return ProbeRunner(client, clock=lambda: ts)


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


class TestJiraProbe:
    """Jira read and write probe round-trip."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_ok_state(self) -> None:
        client = _FakeAtlassianClient()
        runner = _runner(client)

        result = await runner.run("payment", "jira", _cred())

        assert result.state == "ok"
        assert result.read_ok is True
        assert result.write_ok is True
        assert result.auto_fetched_account_id == "jira-bot-001"
        assert result.artifact is None

        # Read probe must run before the write probe.
        method_calls = [name for name, _ in client.calls]
        assert method_calls.index("jira_myself") < method_calls.index(
            "jira_create_self_comment"
        )

    @pytest.mark.asyncio
    async def test_create_uses_canonical_sentinel_body(self) -> None:
        client = _FakeAtlassianClient()
        ts = 1_725_000_000
        runner = _runner(client, ts=ts)

        await runner.run("payment", "jira", _cred())

        create_call = next(
            args for name, args in client.calls if name == "jira_create_self_comment"
        )
        body = create_call[0]
        assert body == make_probe_title(ts)
        assert body == f"_AI_PROBE_{ts}_DELETE_ME"

    @pytest.mark.asyncio
    async def test_idempotent_cleanup_removes_stale_comments(self) -> None:
        """Pre-existing ``_AI_PROBE_*`` comments are deleted first."""
        # Seed two stale probe comments + one unrelated comment.
        stale = [
            {
                "id": "100",
                "issue_key": "BOT-1",
                "body_marker": "_AI_PROBE_1600000000_DELETE_ME",
            },
            {
                "id": "101",
                "issue_key": "BOT-1",
                "body_marker": "_AI_PROBE_1650000000_DELETE_ME",
            },
            {
                "id": "200",
                "issue_key": "BOT-1",
                "body_marker": "ordinary user comment",
            },
        ]
        client = _FakeAtlassianClient(jira_self_comments=list(stale))
        runner = _runner(client)

        result = await runner.run("payment", "jira", _cred())
        assert result.state == "ok"

        deleted_ids = [
            args[1]
            for name, args in client.calls
            if name == "jira_delete_comment"
        ]
        # Both stale probe comments deleted; the new probe comment also
        # deleted in the round-trip; the unrelated "200" left alone.
        assert "100" in deleted_ids
        assert "101" in deleted_ids
        assert "200" not in deleted_ids

    @pytest.mark.asyncio
    async def test_repeated_runs_leave_no_extra_residue(self) -> None:
        """Running the probe twice in a row is idempotent."""
        client = _FakeAtlassianClient()
        runner = _runner(client)

        await runner.run("payment", "jira", _cred())
        await runner.run("payment", "jira", _cred())

        # No leftover probe comments after two runs.
        leftovers = [
            c
            for c in client.jira_self_comments
            if is_probe_artifact_title(str(c.get("body_marker") or ""))
        ]
        assert leftovers == []

    @pytest.mark.asyncio
    async def test_read_failure_skips_write_probe(self) -> None:
        """Read probe failure aborts before any write activity."""
        client = _FakeAtlassianClient(fail_read="jira")
        runner = _runner(client)

        result = await runner.run("payment", "jira", _cred())

        assert result.state == "read_failed"
        assert result.read_ok is False
        assert result.write_ok is False
        # Sanitised error: only the exception class name, never the
        # raw exception message.
        assert result.error_message is not None
        assert "auth failed" not in result.error_message
        assert "RuntimeError" in result.error_message

        write_calls = [
            name
            for name, _ in client.calls
            if name in {"jira_create_self_comment", "jira_delete_comment"}
        ]
        assert write_calls == []

    @pytest.mark.asyncio
    async def test_delete_failure_yields_partial_orphan(self) -> None:
        """Write delete failure returns ``partial_orphan`` + artifact."""
        client = _FakeAtlassianClient(fail_write_delete="jira")
        runner = _runner(client, ts=1_711_111_111)

        result = await runner.run("payment", "jira", _cred())

        assert result.state == "partial_orphan"
        assert result.read_ok is True
        assert result.write_ok is False
        assert result.artifact is not None
        assert isinstance(result.artifact, ProbeArtifact)
        assert result.artifact.dept_id == "payment"
        assert result.artifact.service == "jira"
        assert result.artifact.artifact_type == "jira_comment"
        assert result.artifact.title_or_name == "_AI_PROBE_1711111111_DELETE_ME"


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------


class TestBitbucketProbe:
    """Bitbucket read and write probe round-trip."""

    @pytest.mark.asyncio
    async def test_happy_path_round_trips_branch(self) -> None:
        client = _FakeAtlassianClient()
        runner = _runner(client)
        targets = ProbeTargets(
            bitbucket_workspace="acme",
            bitbucket_repo="payment-service",
        )

        result = await runner.run(
            "payment", "bitbucket", _cred(), targets=targets
        )

        assert result.state == "ok"
        assert result.write_ok is True
        assert client.bitbucket_probe_branches == []
        assert result.auto_fetched_account_id == "bb-bot-001"

    @pytest.mark.asyncio
    async def test_branch_name_uses_canonical_sentinel(self) -> None:
        client = _FakeAtlassianClient()
        ts = 1_730_000_000
        runner = _runner(client, ts=ts)
        targets = ProbeTargets(
            bitbucket_workspace="acme",
            bitbucket_repo="payment-service",
        )

        await runner.run("payment", "bitbucket", _cred(), targets=targets)

        create_call = next(
            args
            for name, args in client.calls
            if name == "bitbucket_create_branch"
        )
        # args = (workspace, repo, branch_name)
        assert create_call[2] == make_probe_title(ts)

    @pytest.mark.asyncio
    async def test_idempotent_cleanup_deletes_stale_branches(self) -> None:
        """Orphan ``_AI_PROBE_*`` branches are deleted up-front."""
        client = _FakeAtlassianClient(
            bitbucket_probe_branches=[
                "_AI_PROBE_1600000000_DELETE_ME",
                "feature/regular-branch",  # unrelated - must NOT be deleted
                "_AI_PROBE_1650000000_DELETE_ME",
            ]
        )
        runner = _runner(client)
        targets = ProbeTargets(
            bitbucket_workspace="acme", bitbucket_repo="payment-service"
        )

        result = await runner.run("payment", "bitbucket", _cred(), targets=targets)
        assert result.state == "ok"

        deleted = [
            args[2]
            for name, args in client.calls
            if name == "bitbucket_delete_branch"
        ]
        assert "_AI_PROBE_1600000000_DELETE_ME" in deleted
        assert "_AI_PROBE_1650000000_DELETE_ME" in deleted
        assert "feature/regular-branch" not in deleted

    @pytest.mark.asyncio
    async def test_missing_targets_returns_read_failed(self) -> None:
        """Bitbucket probes require workspace+repo targets."""
        client = _FakeAtlassianClient()
        runner = _runner(client)

        # No targets at all.
        result = await runner.run("payment", "bitbucket", _cred())
        assert result.state == "read_failed"
        assert client.calls == []  # never even called the read probe

        # Partial targets - workspace only.
        result2 = await runner.run(
            "payment",
            "bitbucket",
            _cred(),
            targets=ProbeTargets(bitbucket_workspace="acme"),
        )
        assert result2.state == "read_failed"

    @pytest.mark.asyncio
    async def test_delete_failure_yields_partial_orphan(self) -> None:
        client = _FakeAtlassianClient(fail_write_delete="bitbucket")
        ts = 1_722_222_222
        runner = _runner(client, ts=ts)
        targets = ProbeTargets(
            bitbucket_workspace="acme", bitbucket_repo="payment-service"
        )

        result = await runner.run(
            "payment", "bitbucket", _cred(), targets=targets
        )

        assert result.state == "partial_orphan"
        assert result.artifact is not None
        assert result.artifact.service == "bitbucket"
        assert result.artifact.artifact_type == "bitbucket_branch"
        assert result.artifact.title_or_name == f"_AI_PROBE_{ts}_DELETE_ME"
        assert "acme/payment-service" in result.artifact.external_id


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------


class TestConfluenceProbe:
    """Confluence draft round-trip."""

    @pytest.mark.asyncio
    async def test_happy_path_creates_and_deletes_draft(self) -> None:
        client = _FakeAtlassianClient()
        runner = _runner(client)
        targets = ProbeTargets(confluence_space_key="PAYDOCS")

        result = await runner.run(
            "payment", "confluence", _cred(), targets=targets
        )

        assert result.state == "ok"
        assert result.write_ok is True
        assert client.confluence_probe_pages == []
        assert result.auto_fetched_account_id == "conf-bot-001"

    @pytest.mark.asyncio
    async def test_idempotent_cleanup_deletes_stale_pages(self) -> None:
        client = _FakeAtlassianClient(
            confluence_probe_pages=[
                {"id": "1", "title": "_AI_PROBE_1600000000_DELETE_ME"},
                {"id": "2", "title": "Quarterly review notes"},
                {"id": "3", "title": "_AI_PROBE_1650000000_DELETE_ME"},
            ]
        )
        runner = _runner(client)
        targets = ProbeTargets(confluence_space_key="PAYDOCS")

        result = await runner.run(
            "payment", "confluence", _cred(), targets=targets
        )
        assert result.state == "ok"

        deleted_ids = [
            args[0]
            for name, args in client.calls
            if name == "confluence_delete_page"
        ]
        assert "1" in deleted_ids
        assert "3" in deleted_ids
        assert "2" not in deleted_ids

    @pytest.mark.asyncio
    async def test_partial_orphan_when_delete_fails(self) -> None:
        """Confluence delete failure produces ``partial_orphan``."""
        client = _FakeAtlassianClient(fail_write_delete="confluence")
        ts = 1_733_333_333
        runner = _runner(client, ts=ts)
        targets = ProbeTargets(confluence_space_key="PAYDOCS")

        result = await runner.run(
            "payment", "confluence", _cred(), targets=targets
        )

        assert result.state == "partial_orphan"
        assert result.write_ok is False
        assert result.artifact is not None
        assert result.artifact.service == "confluence"
        assert result.artifact.artifact_type == "confluence_page"
        assert result.artifact.title_or_name == f"_AI_PROBE_{ts}_DELETE_ME"
        # External id is the draft page id assigned by the fake.
        assert result.artifact.external_id.isdigit()

    @pytest.mark.asyncio
    async def test_create_failure_returns_write_failed(self) -> None:
        client = _FakeAtlassianClient(fail_write_create="confluence")
        runner = _runner(client)
        targets = ProbeTargets(confluence_space_key="PAYDOCS")

        result = await runner.run(
            "payment", "confluence", _cred(), targets=targets
        )

        assert result.state == "write_failed"
        assert result.read_ok is True
        assert result.write_ok is False
        assert result.artifact is None  # nothing left behind to track

    @pytest.mark.asyncio
    async def test_missing_space_key_returns_read_failed(self) -> None:
        client = _FakeAtlassianClient()
        runner = _runner(client)

        result = await runner.run("payment", "confluence", _cred())

        assert result.state == "read_failed"
        assert "space_key" in (result.error_message or "")


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


class TestCredentialHygiene:
    """Plain-text credentials never appear in artifacts or logs."""

    @pytest.mark.asyncio
    async def test_artifact_body_does_not_contain_token(self) -> None:
        """The Confluence draft title (carried into the partial-orphan
        artifact) never carries the plain-text credential."""
        client = _FakeAtlassianClient(fail_write_delete="confluence")
        runner = _runner(client)
        targets = ProbeTargets(confluence_space_key="PAYDOCS")

        result = await runner.run(
            "payment", "confluence", _cred(), targets=targets
        )

        assert result.artifact is not None
        assert _TOKEN_MARKER not in result.artifact.title_or_name
        assert _TOKEN_MARKER not in result.artifact.external_id
        # The error message also stays free of plain-text credentials.
        assert _TOKEN_MARKER not in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_jira_create_call_body_contains_only_sentinel(self) -> None:
        client = _FakeAtlassianClient()
        ts = 1_700_000_000
        runner = _runner(client, ts=ts)

        await runner.run("payment", "jira", _cred())

        create_call = next(
            args for name, args in client.calls if name == "jira_create_self_comment"
        )
        body = create_call[0]
        assert body == make_probe_title(ts)
        assert _TOKEN_MARKER not in body
        assert "bot@acme.com" not in body
