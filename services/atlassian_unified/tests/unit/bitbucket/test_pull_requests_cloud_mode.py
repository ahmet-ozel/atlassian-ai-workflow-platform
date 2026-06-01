"""Unit tests for the pull-requests Cloud branch (Task 8.2).

These tests cover Requirements 9.1 - 9.8 and 19.1 / 19.2 from the
``bitbucket-cloud-dc-parity`` spec. They exercise :class:`PullRequestsMixin`
in **Cloud mode** and assert that every mode-branching method issues its
outbound HTTP call against the expected Cloud 2.0 URL template:

    /2.0/repositories/{workspace}/{slug}/pullrequests[/{id}][/...]

One happy-path test per routed method is included. DC-path tests live in
the existing ``test_pull_requests*`` modules and are intentionally not
touched by this file (Requirements 19.2 / 23.2).

Test pattern (shared across every test here):

* :class:`PullRequestsMixin` is instantiated via ``__new__`` so the
  :class:`BitbucketClient.__init__` (which builds a real
  ``atlassian.Bitbucket`` session and validates credentials) is bypassed.
* ``mixin.bitbucket`` is a :class:`unittest.mock.MagicMock` whose HTTP
  primitives (``get``, ``post``, ``put``, ``delete``) return minimal
  Cloud-shaped dicts so the mixin's normalizer path is exercised end-to-end.
* ``mixin.config`` is a :class:`types.SimpleNamespace` carrying the
  ``is_cloud=True`` flag and a default Cloud ``workspace`` so
  :func:`_resolve_workspace` returns the documented default when
  ``project_key`` is empty.
* Where the mixin bypasses the Bitbucket wrapper and hits
  ``self.bitbucket._session.get`` directly (``get_pull_request_diff`` and
  ``get_pr_file_diff``), the ``_session`` attribute is given its own
  :class:`MagicMock` whose ``get`` method returns a fake response object
  with ``.text`` and a no-op ``.raise_for_status()``.

Each test asserts the **URL** that reached the Bitbucket HTTP layer
matches the Cloud template for that method. Many tests additionally
verify the dispatcher used the right HTTP verb (e.g. ``approve`` is POST,
``unapprove`` is DELETE), which is the other half of "routes to the
correct Cloud endpoint" for Req 20.1 / 20.2.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.pull_requests import PullRequestsMixin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


WORKSPACE = "my-team"
REPO_SLUG = "my-repo"
PR_ID = 42
CLOUD_PR_BASE = f"/2.0/repositories/{WORKSPACE}/{REPO_SLUG}/pullrequests"


@pytest.fixture
def pr_mixin() -> PullRequestsMixin:
    """Return a :class:`PullRequestsMixin` wired for Cloud-mode unit tests.

    :class:`BitbucketClient.__init__` is skipped by constructing via
    ``__new__``; ``mixin.bitbucket`` is a bare :class:`MagicMock` so the
    HTTP primitives return ``MagicMock`` by default, and ``mixin.config``
    is a :class:`SimpleNamespace` stamped with the attributes the mixin
    touches in Cloud mode.
    """
    mixin = PullRequestsMixin.__new__(PullRequestsMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace=WORKSPACE,
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _minimal_cloud_pr(pr_id: int = PR_ID, state: str = "OPEN") -> dict[str, Any]:
    """Return a minimal Cloud 2.0 pull-request payload.

    The shape carries just enough structure to exercise
    :func:`normalize_pull_request` without tripping its defensive guards:
    the Cloud-side ``source`` / ``destination`` blocks, a Cloud-shaped
    ``author`` with ``account_id`` so ``normalize_user`` fires, and ISO
    8601 timestamps so the epoch-millis conversion path is covered.
    """
    return {
        "id": pr_id,
        "state": state,
        "title": "example",
        "source": {"branch": {"name": "feature/x"}, "commit": {"hash": "a" * 40}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 40}},
        "author": {
            "account_id": "abc-123",
            "display_name": "Alice",
            "uuid": "{11111111-1111-1111-1111-111111111111}",
        },
        "reviewers": [],
        "participants": [],
        "created_on": "2024-01-02T03:04:05+00:00",
        "updated_on": "2024-01-02T03:04:06+00:00",
    }


def _paged(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a single-page Cloud pagination envelope with ``next=None``.

    This is the terminator shape :meth:`BitbucketClient._get_paged_results_cloud`
    stops on (Requirement 7.3), so one GET is enough to deliver the full
    result set.
    """
    return {"values": values, "next": None, "page": 1, "pagelen": 10, "size": len(values)}


def _diff_session(mixin: PullRequestsMixin, text: str = "diff --git a/x b/x\n") -> MagicMock:
    """Wire ``mixin.bitbucket._session`` for raw-text diff endpoints.

    ``get_pull_request_diff`` and ``get_pr_file_diff`` reach under the
    atlassian-python wrapper to issue a raw ``requests.Session.get`` call
    because the response body is a text diff, not JSON. We replace the
    session with a :class:`MagicMock` that returns a fake ``Response``
    carrying the expected ``.text`` and a no-op ``.raise_for_status()``.

    Returns the session mock so tests can assert on the captured
    ``.get(url, ...)`` call directly.
    """
    fake_response = MagicMock()
    fake_response.text = text
    fake_response.raise_for_status = MagicMock(return_value=None)
    session = MagicMock()
    session.get.return_value = fake_response
    mixin.bitbucket._session = session
    return session


# ===========================================================================
# List / Get / Create / Update
# ===========================================================================


class TestGetPullRequests:
    """Req 9.1, 9.2 — ``GET /2.0/repositories/{ws}/{slug}/pullrequests``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.get.return_value = _paged([_minimal_cloud_pr(1)])

        result = pr_mixin.get_pull_requests(WORKSPACE, REPO_SLUG, limit=10)

        # One page => one GET; the URL is the bare ``/pullrequests`` collection.
        pr_mixin.bitbucket.get.assert_called_once()
        url = pr_mixin.bitbucket.get.call_args[0][0]
        assert url == CLOUD_PR_BASE
        # Happy-path: the mixin returns a list of normalized PRs.
        assert isinstance(result, list)
        assert result and result[0]["id"] == 1


class TestGetPullRequest:
    """Req 9.2 — ``GET /2.0/repositories/{ws}/{slug}/pullrequests/{id}``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.get.return_value = _minimal_cloud_pr()

        result = pr_mixin.get_pull_request(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.get.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}")
        # Normalized payload exposes the DC-shaped ``fromRef``/``toRef`` keys
        # synthesized from the Cloud source/destination blocks.
        assert result["id"] == PR_ID
        assert "fromRef" in result and "toRef" in result


class TestCreatePullRequest:
    """Req 9.2 — ``POST /2.0/repositories/{ws}/{slug}/pullrequests``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = _minimal_cloud_pr()

        result = pr_mixin.create_pull_request(
            WORKSPACE,
            REPO_SLUG,
            title="A title",
            from_branch="feature/x",
            to_branch="main",
            description="body",
            reviewers=["acct-9"],
        )

        pr_mixin.bitbucket.post.assert_called_once()
        url = pr_mixin.bitbucket.post.call_args[0][0]
        data = pr_mixin.bitbucket.post.call_args.kwargs["data"]
        assert url == CLOUD_PR_BASE
        # Cloud body uses source/destination; DC-style refs/heads/ stripped.
        assert data["source"]["branch"]["name"] == "feature/x"
        assert data["destination"]["branch"]["name"] == "main"
        assert result["id"] == PR_ID


class TestUpdatePullRequest:
    """Req 9.2 — ``PUT /2.0/repositories/{ws}/{slug}/pullrequests/{id}``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.put.return_value = _minimal_cloud_pr()

        result = pr_mixin.update_pull_request(
            WORKSPACE,
            REPO_SLUG,
            PR_ID,
            version=1,  # ignored on Cloud
            title="new title",
            description="new body",
            reviewers=["acct-9"],
        )

        pr_mixin.bitbucket.put.assert_called_once()
        url = pr_mixin.bitbucket.put.call_args[0][0]
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}"
        assert result["id"] == PR_ID


# ===========================================================================
# State transitions — merge / approve / decline / reopen
# ===========================================================================


class TestMergePullRequest:
    """Req 9.5 — ``POST .../pullrequests/{id}/merge``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = _minimal_cloud_pr(state="MERGED")

        pr_mixin.merge_pull_request(
            WORKSPACE,
            REPO_SLUG,
            PR_ID,
            message="merging",
            delete_source_branch=True,
        )

        pr_mixin.bitbucket.post.assert_called_once()
        url = pr_mixin.bitbucket.post.call_args[0][0]
        body = pr_mixin.bitbucket.post.call_args.kwargs["data"]
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}/merge"
        assert body["close_source_branch"] is True


class TestApprovePullRequest:
    """Req 9.3 — ``POST .../pullrequests/{id}/approve``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = {"approved": True, "state": "approved"}

        pr_mixin.approve_pull_request(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.post.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}/approve")


class TestUnapprovePullRequest:
    """Req 9.3 — ``DELETE .../pullrequests/{id}/approve``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.delete.return_value = None

        pr_mixin.unapprove_pull_request(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.delete.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}/approve")


class TestDeclinePullRequest:
    """Req 9.4 — ``POST .../pullrequests/{id}/decline``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = _minimal_cloud_pr(state="DECLINED")

        pr_mixin.decline_pull_request(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.post.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}/decline")


class TestReopenPullRequest:
    """Req 9.2 — ``POST .../pullrequests/{id}/reopen``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = _minimal_cloud_pr()

        pr_mixin.reopen_pull_request(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.post.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}/reopen")


# ===========================================================================
# Activities / diff / changes
# ===========================================================================


class TestGetPullRequestActivities:
    """Req 9.7 — ``GET .../pullrequests/{id}/activity`` (singular on Cloud)."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.get.return_value = _paged([{"id": 1, "action": "APPROVED"}])

        pr_mixin.get_pull_request_activities(WORKSPACE, REPO_SLUG, PR_ID, limit=10)

        pr_mixin.bitbucket.get.assert_called_once()
        url = pr_mixin.bitbucket.get.call_args[0][0]
        # Cloud spells it ``activity`` (singular), not DC's ``activities``.
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}/activity"


class TestGetPullRequestDiff:
    """Req 9.8 — ``GET .../pullrequests/{id}/diff`` (raw diff)."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        session = _diff_session(pr_mixin, text="diff --git a/x b/x\n")

        result = pr_mixin.get_pull_request_diff(
            WORKSPACE, REPO_SLUG, PR_ID, context_lines=5
        )

        # The diff endpoint is reached via the raw session; the absolute URL
        # combines the Cloud base URL and the PR-scoped diff path.
        session.get.assert_called_once()
        full_url = session.get.call_args[0][0]
        assert full_url == f"https://api.bitbucket.org{CLOUD_PR_BASE}/{PR_ID}/diff"
        assert result == "diff --git a/x b/x\n"


class TestGetPullRequestChanges:
    """Req 9.2 — ``GET .../pullrequests/{id}/diffstat`` (per-file changes)."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.get.return_value = _paged(
            [{"status": "modified", "new": {"path": "x.py"}}]
        )

        pr_mixin.get_pull_request_changes(WORKSPACE, REPO_SLUG, PR_ID, limit=10)

        pr_mixin.bitbucket.get.assert_called_once()
        url = pr_mixin.bitbucket.get.call_args[0][0]
        # Cloud spells the changed-files endpoint as ``diffstat``.
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}/diffstat"


class TestGetPrFileDiff:
    """Req 9.8 — ``GET .../pullrequests/{id}/diff`` with ``path`` query param."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        session = _diff_session(pr_mixin, text="+ new line\n")

        result = pr_mixin.get_pr_file_diff(
            WORKSPACE, REPO_SLUG, PR_ID, path="src/app.py", context_lines=3
        )

        session.get.assert_called_once()
        full_url = session.get.call_args[0][0]
        params = session.get.call_args.kwargs["params"]
        assert full_url == f"https://api.bitbucket.org{CLOUD_PR_BASE}/{PR_ID}/diff"
        # File scoping is done via the ``path`` query parameter on Cloud.
        assert params["path"] == "src/app.py"
        assert result == "+ new line\n"


# ===========================================================================
# Comments — add / update / delete
# ===========================================================================


class TestAddPullRequestComment:
    """Req 9.2 — ``POST .../pullrequests/{id}/comments``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = {
            "id": 7,
            "content": {"raw": "looks good"},
            "user": {"account_id": "abc-123", "display_name": "A"},
        }

        result = pr_mixin.add_pull_request_comment(
            WORKSPACE, REPO_SLUG, PR_ID, text="looks good"
        )

        pr_mixin.bitbucket.post.assert_called_once()
        url = pr_mixin.bitbucket.post.call_args[0][0]
        body = pr_mixin.bitbucket.post.call_args.kwargs["data"]
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}/comments"
        assert body["content"]["raw"] == "looks good"
        # Normalized comment exposes a DC-ish ``text`` alias for downstream.
        assert result["text"] == "looks good"


class TestUpdatePrComment:
    """Req 9.2 — ``PUT .../pullrequests/{id}/comments/{cid}``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.put.return_value = {
            "id": 7,
            "content": {"raw": "edited"},
        }

        pr_mixin.update_pr_comment(
            WORKSPACE, REPO_SLUG, PR_ID, comment_id=7, version=1, text="edited"
        )

        pr_mixin.bitbucket.put.assert_called_once()
        url = pr_mixin.bitbucket.put.call_args[0][0]
        assert url == f"{CLOUD_PR_BASE}/{PR_ID}/comments/7"


class TestDeletePrComment:
    """Req 9.2 — ``DELETE .../pullrequests/{id}/comments/{cid}``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.delete.return_value = None

        pr_mixin.delete_pr_comment(
            WORKSPACE, REPO_SLUG, PR_ID, comment_id=7, version=1
        )

        pr_mixin.bitbucket.delete.assert_called_once_with(
            f"{CLOUD_PR_BASE}/{PR_ID}/comments/7"
        )


# ===========================================================================
# Reviewers — add / remove (fetch-then-PUT pattern on Cloud)
# ===========================================================================


class TestAddPrReviewer:
    """Req 9.12 — reviewer add on Cloud = PUT on the PR itself.

    Cloud has no ``POST /participants`` endpoint; the mixin fetches the PR
    first to read the current reviewer list, appends the new entry, then
    PUTs the PR back. Both calls SHALL target ``.../pullrequests/{id}``.
    """

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_url = f"{CLOUD_PR_BASE}/{PR_ID}"
        pr_mixin.bitbucket.get.return_value = {**_minimal_cloud_pr(), "reviewers": []}
        pr_mixin.bitbucket.put.return_value = _minimal_cloud_pr()

        pr_mixin.add_pr_reviewer(WORKSPACE, REPO_SLUG, PR_ID, username="acct-9")

        # Both calls route to the bare PR URL (no sub-path).
        pr_mixin.bitbucket.get.assert_called_once_with(pr_url)
        pr_mixin.bitbucket.put.assert_called_once()
        put_url = pr_mixin.bitbucket.put.call_args[0][0]
        assert put_url == pr_url


class TestRemovePrReviewer:
    """Req 9.12 — reviewer removal on Cloud = PUT with filtered list."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_url = f"{CLOUD_PR_BASE}/{PR_ID}"
        pr_mixin.bitbucket.get.return_value = {
            **_minimal_cloud_pr(),
            "reviewers": [{"account_id": "acct-9"}, {"account_id": "acct-10"}],
        }
        pr_mixin.bitbucket.put.return_value = _minimal_cloud_pr()

        assert pr_mixin.remove_pr_reviewer(
            WORKSPACE, REPO_SLUG, PR_ID, username="acct-9"
        ) is True

        pr_mixin.bitbucket.get.assert_called_once_with(pr_url)
        pr_mixin.bitbucket.put.assert_called_once()
        put_url = pr_mixin.bitbucket.put.call_args[0][0]
        assert put_url == pr_url


# ===========================================================================
# Participant status — APPROVED routes to /approve on Cloud
# ===========================================================================


class TestSetPrParticipantStatus:
    """Req 9.3 — ``APPROVED`` routes to ``POST .../pullrequests/{id}/approve``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = {"approved": True, "state": "approved"}

        result = pr_mixin.set_pr_participant_status(
            WORKSPACE,
            REPO_SLUG,
            PR_ID,
            username="acct-9",  # ignored on Cloud; self-subject
            status="APPROVED",
            approved=True,
        )

        pr_mixin.bitbucket.post.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}/approve")
        assert result["approved"] is True


# ===========================================================================
# Request-changes (NEEDS_WORK) — POST/DELETE on /request-changes
# ===========================================================================


class TestRequestChangesPullRequest:
    """Req 9.11 — ``POST .../pullrequests/{id}/request-changes``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.post.return_value = {"status": "NEEDS_WORK"}

        pr_mixin.request_changes_pull_request(
            WORKSPACE, REPO_SLUG, PR_ID, username="acct-9"
        )

        pr_mixin.bitbucket.post.assert_called_once_with(
            f"{CLOUD_PR_BASE}/{PR_ID}/request-changes"
        )


class TestUnrequestChangesPullRequest:
    """Req 9.11 — ``DELETE .../pullrequests/{id}/request-changes``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.delete.return_value = None

        pr_mixin.unrequest_changes_pull_request(
            WORKSPACE, REPO_SLUG, PR_ID, username="acct-9"
        )

        pr_mixin.bitbucket.delete.assert_called_once_with(
            f"{CLOUD_PR_BASE}/{PR_ID}/request-changes"
        )


# ===========================================================================
# Merge status — GET on the PR itself (Cloud has no dedicated endpoint)
# ===========================================================================


class TestGetPrMergeStatus:
    """Req 9.2 — Cloud synthesizes DC merge-status from ``GET .../pullrequests/{id}``."""

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        pr_mixin.bitbucket.get.return_value = _minimal_cloud_pr(state="OPEN")

        result = pr_mixin.get_pr_merge_status(WORKSPACE, REPO_SLUG, PR_ID)

        pr_mixin.bitbucket.get.assert_called_once_with(f"{CLOUD_PR_BASE}/{PR_ID}")
        # OPEN ⇒ canMerge=True; DC-shaped response synthesized from the PR.
        assert result["canMerge"] is True
        assert result["conflicted"] is False
        assert result["vetoes"] == []


# ===========================================================================
# Dashboard — list_my_pull_requests resolves /2.0/user then paginates
# ===========================================================================


class TestListMyPullRequests:
    """Req 9.2 — Cloud: ``GET /2.0/user`` then ``GET /2.0/pullrequests/{uuid}``.

    Cloud has no ``/dashboard/pull-requests`` endpoint. The mixin first
    resolves the authenticated user's UUID via ``GET /2.0/user`` and then
    pages the dashboard view at ``/2.0/pullrequests/{selector}``.
    """

    def test_outbound_url_matches_cloud_template(self, pr_mixin):
        user_uuid = "{22222222-2222-2222-2222-222222222222}"
        dashboard_url = f"/2.0/pullrequests/{user_uuid}"

        # Sequence: first call resolves /2.0/user, second call is the
        # first page of the dashboard endpoint.
        pr_mixin.bitbucket.get.side_effect = [
            {"uuid": user_uuid, "account_id": "abc-123"},
            _paged([_minimal_cloud_pr(1)]),
        ]

        result = pr_mixin.list_my_pull_requests(role="REVIEWER", state="OPEN", limit=10)

        assert pr_mixin.bitbucket.get.call_count == 2
        # First GET resolves the authenticated user.
        first_url = pr_mixin.bitbucket.get.call_args_list[0][0][0]
        assert first_url == "/2.0/user"
        # Second GET is the dashboard endpoint scoped to that user.
        second_url = pr_mixin.bitbucket.get.call_args_list[1][0][0]
        assert second_url == dashboard_url
        assert isinstance(result, list) and result[0]["id"] == 1
