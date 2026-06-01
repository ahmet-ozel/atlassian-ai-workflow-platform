"""Property test P8 — Mode-based URL routing.

Validates Requirements 8.1 through 8.6, 9.1 through 9.8, 9.11, 9.12,
10.1 through 10.5, 11.1 through 11.11, 12.1 through 12.3, 13.1, 13.2
and 20.1 through 20.5 of the ``bitbucket-cloud-dc-parity`` spec /
design Property 8.

Property
--------

For every mode-branching Bitbucket_Tool ``t`` and for every effective
mode ``m ∈ {"cloud", "dc"}``, invoking the mixin method that backs
``t`` with a monkeypatched :class:`atlassian.Bitbucket` transport
SHALL issue at least one outbound request whose URL path prefix
matches the mode-specific template declared in design Sections 4, 9,
10, 11, 12, and 13:

* DC targets:
    * ``/rest/api/latest/...``
    * ``/rest/branch-utils/latest/...`` (branch-utils delete)
    * ``/rest/git/latest/...`` (tag create / delete)
    * ``/rest/insights/1.0/...`` (code insights reports / annotations)
    * ``/rest/build-status/latest/...`` (commit build status)
    * ``/rest/search/latest/...`` (code search)

* Cloud targets:
    * ``/2.0/repositories/{workspace}/{slug}/...``
    * ``/2.0/workspaces/...`` (project list / code search)
    * ``/2.0/users/...`` (get_user)

The property is realized by the function
:func:`test_bitbucket_tool_routes_to_correct_client_by_is_cloud`
(mandated by Requirement 20.1) which iterates the routing matrix
defined at module scope and, for each row, constructs a bypassed
mixin instance with ``is_cloud`` toggled on and off, captures every
outbound URL through a recording :class:`MagicMock`, and asserts the
URL prefix matches the mode-appropriate template.

Hypothesis is used to fuzz the bound variables (workspace slug, repo
slug, commit SHA, PR id, webhook id, user account_id, branch / tag
name) across a wide range of legal values so that the routing
property is not accidentally passing on a single hard-coded value.

Why a property test
-------------------

The sibling ``test_*_cloud_mode`` unit tests (tasks 7.2, 8.2, 9.2,
10.3, 11.2, 12.2, 13.3, 14.2, 15.2) already pin one URL per method in
Cloud mode against a mocked transport. This property test is the
*cross-cutting* regression guard that Requirement 20 mandates — it
asserts that *every* mode-branching tool correctly dispatches by
``is_cloud`` under randomly-generated inputs, so a future refactor
that silently drops an ``if self.is_cloud:`` branch on one method
fails this test even when the per-method unit tests happen to skip
the affected code path.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.1, 9.2,
9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.11, 9.12, 10.1, 10.2, 10.3, 10.4,
10.5, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10,
11.11, 12.1, 12.2, 12.3, 13.1, 13.2, 20.1, 20.2, 20.3, 20.4, 20.5**
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.branches import BranchesMixin
from mcp_atlassian.bitbucket.code_insights import CodeInsightsMixin
from mcp_atlassian.bitbucket.commit_comments import CommitCommentsMixin
from mcp_atlassian.bitbucket.commits import CommitsMixin
from mcp_atlassian.bitbucket.pull_requests import PullRequestsMixin
from mcp_atlassian.bitbucket.repositories import RepositoriesMixin
from mcp_atlassian.bitbucket.users import UsersMixin
from mcp_atlassian.bitbucket.watching import WatchingMixin
from mcp_atlassian.bitbucket.webhooks import WebhooksMixin


# ---------------------------------------------------------------------------
# Hypothesis strategies for the bound variables in the routing matrix
# ---------------------------------------------------------------------------


# Bitbucket slugs and identifiers follow a conservative ASCII alphabet.
# The URL templates never percent-encode these values, so the generated
# strings must avoid ``/``, ``?``, ``#``, and whitespace (those would
# split or terminate the URL path segment the template embeds them in).
_SLUG_ALPHABET = string.ascii_lowercase + string.digits + "-._"

slugs: st.SearchStrategy[str] = st.text(
    alphabet=_SLUG_ALPHABET, min_size=1, max_size=16
)

# DC project keys follow Bitbucket's convention of 2–10 uppercase
# ASCII letters. Cloud workspace slugs use ``slugs`` above; the two
# are intentionally different so the test distinguishes the DC
# ``{key}`` and Cloud ``{workspace}`` path segments.
project_keys: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("Z")),
    min_size=2,
    max_size=10,
)

# Commit SHAs — any 40-character hex string. Accept both lower and
# upper case because Bitbucket is case-insensitive on SHA lookups.
commit_shas: st.SearchStrategy[str] = st.text(
    alphabet="0123456789abcdef", min_size=40, max_size=40
)

# PR / webhook numeric ids — Bitbucket-internal positive integers.
pr_ids: st.SearchStrategy[int] = st.integers(min_value=1, max_value=10_000)
webhook_ids: st.SearchStrategy[int] = st.integers(
    min_value=1, max_value=100_000
)

# Cloud user identifiers — either a modern ``account_id`` shape
# (``^[A-Za-z0-9_:\-]+$``) or a brace-wrapped UUID. The routing
# property only cares that ``GET /2.0/users/{id}`` is hit, so any
# valid Cloud account identifier works. A curated pool keeps the
# generator cheap while exercising both shapes.
cloud_account_ids: st.SearchStrategy[str] = st.sampled_from(
    (
        "557058:abc-123",
        "557058:def_456",
        "abc123",
        "{01234567-89ab-cdef-0123-456789abcdef}",
        "{aabbccdd-eeff-0011-2233-445566778899}",
    )
)


# ---------------------------------------------------------------------------
# Fake transport — records every outbound HTTP URL without issuing HTTP
# ---------------------------------------------------------------------------


@dataclass
class _FakeSessionResponse:
    """Minimal stand-in for a :class:`requests.Response`.

    Exposes ``.status_code``, ``.text``, ``.content``, a no-op
    ``.raise_for_status()``, and a ``.json()`` that returns an empty
    Cloud pagination envelope so the mixin's direct-session methods
    (``get_file_content``, ``get_raw_file_content``, ``get_diff``,
    ``compare_commits``, ``get_pr_file_diff``, ``get_pull_request_diff``,
    ``browse_directory`` Cloud helper, and the Cloud watcher PUT/DELETE
    helpers) can decode a response without raising.
    """

    status_code: int = 200
    text: str = ""
    content: bytes = b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        """Return a minimal Cloud pagination envelope (empty values)."""
        if self.text:
            import json as _json
            return _json.loads(self.text)
        return {"values": [], "page": 1, "pagelen": 10, "size": 0}


@dataclass
class _RecordingTransport:
    """Record-only stand-in for :class:`atlassian.Bitbucket`.

    Every URL passed to ``get`` / ``post`` / ``put`` / ``delete`` (and
    to the low-level ``_session.get`` / ``_session.put`` /
    ``_session.delete``) is appended to :attr:`urls` so the test can
    assert the mode-appropriate prefix is present among the recorded
    URLs. The per-method :attr:`responses` mapping lets rows that
    need a specific response shape (for example, ``list_my_pull_requests``
    which fetches ``/2.0/user`` first) override the default.

    The high-level HTTP methods (``get``/``post``/``put``/``delete``)
    return a pagination-shaped dict by default so mixin code that
    passes payloads through ``_get_paged_results`` or a normalizer
    completes without raising.
    """

    urls: list[str] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)

    # Populated on construction so mixin code that reaches into
    # ``bitbucket._session.*`` sees the same recorder the high-level
    # methods use.
    _session: "_RecordingSession" = field(init=False)

    def __post_init__(self) -> None:
        self._session = _RecordingSession(parent=self)

    # A DC pagination envelope (``{values, isLastPage, ...}``)
    # satisfies both the DC and Cloud branches because it is also a
    # valid one-page Cloud envelope: no ``next`` key → terminates
    # after one page per Requirement 7.3; ``isLastPage=True``
    # terminates DC per Requirement 7.2.

    def _response_for(self, url: str) -> Any:
        # Most rows don't care about the exact payload — the mixin's
        # normalizers all accept partial dicts. Rows that DO care
        # (``list_my_pull_requests`` which calls ``/2.0/user`` first)
        # register explicit overrides through :meth:`set_response`.
        if url in self.responses:
            return self.responses[url]
        # Default: a grab-bag dict that satisfies every mixin method
        # in the routing matrix. The DC pagination envelope shape
        # (``{values, isLastPage, ...}``) doubles as a valid one-page
        # Cloud envelope because Cloud terminates on a missing
        # ``next`` key (Requirement 7.3) and DC terminates on
        # ``isLastPage=True`` (Requirement 7.2). The extra keys
        # (``mainbranch``, ``slug``, ``workspace``, ``state``) exist
        # to satisfy methods that read non-pagination fields from the
        # response — ``get_default_branch`` reads ``mainbranch``,
        # ``get_pr_merge_status`` reads ``state``, and ``get_repository``
        # readers pass the payload through ``normalize_repository`` which
        # inspects ``workspace``.
        return {
            "values": [],
            "size": 0,
            "isLastPage": True,
            "pagelen": 10,
            "page": 1,
            "mainbranch": {"name": "main", "type": "branch"},
            "slug": "repo",
            "name": "repo",
            "state": "OPEN",
            "workspace": {"slug": "my-team"},
        }

    def set_response(self, url: str, payload: Any) -> None:
        """Register a non-default response for a specific URL."""
        self.responses[url] = payload

    # -- high-level atlassian.Bitbucket primitives ---------------------

    def get(self, url: str, params: Any = None, **_: Any) -> Any:
        self.urls.append(url)
        return self._response_for(url)

    def post(self, url: str, data: Any = None, params: Any = None, **_: Any) -> Any:
        self.urls.append(url)
        return self._response_for(url)

    def put(self, url: str, data: Any = None, params: Any = None, **_: Any) -> Any:
        self.urls.append(url)
        return self._response_for(url)

    def delete(
        self, url: str, data: Any = None, params: Any = None, **_: Any
    ) -> Any:
        self.urls.append(url)
        return self._response_for(url)


@dataclass
class _RecordingSession:
    """Record-only stand-in for ``bitbucket._session``.

    The mixin reaches past the high-level transport for endpoints that
    return unified-diff text or raw file bytes
    (``get_file_content`` / ``get_raw_file_content`` / ``get_diff`` /
    ``compare_commits`` / ``get_pr_file_diff`` / ``get_pull_request_diff``)
    and for the Cloud watcher idempotence helpers
    (``_cloud_watch`` / ``_cloud_unwatch``). This class records the
    URL passed to ``get``/``put``/``delete`` on the session — stripping
    the ``http(s)://host`` prefix so the recorded string aligns with
    what the high-level transport records — and returns a minimal
    :class:`_FakeSessionResponse` so the caller can ``raise_for_status``
    and ``.text``/``.content``-decode without real HTTP.
    """

    parent: "_RecordingTransport"
    # Optional hard-coded status code for the next call. Used by the
    # watch/unwatch tests to simulate 200 (fresh) vs 409 (already)
    # vs 404 (not watching). Defaults to 200.
    next_status: int = 200

    @staticmethod
    def _strip_base(url: str) -> str:
        # The mixin calls ``self.bitbucket._session.get(
        # f"{self.config.url}{url}", ...)``, so ``url`` here is the
        # fully-qualified absolute URL. Split on the host to recover
        # the path so the caller's routing assertions operate on
        # the same shape as the high-level transport's recorded URLs.
        for scheme in ("https://", "http://"):
            if url.startswith(scheme):
                tail = url[len(scheme):]
                slash = tail.find("/")
                if slash == -1:
                    return "/"
                return tail[slash:]
        return url

    def get(self, url: str, params: Any = None, **_: Any) -> _FakeSessionResponse:
        self.parent.urls.append(self._strip_base(url))
        return _FakeSessionResponse(status_code=self.next_status, text="", content=b"")

    def put(self, url: str, data: Any = None, **_: Any) -> _FakeSessionResponse:
        self.parent.urls.append(self._strip_base(url))
        return _FakeSessionResponse(status_code=self.next_status)

    def delete(self, url: str, data: Any = None, **_: Any) -> _FakeSessionResponse:
        self.parent.urls.append(self._strip_base(url))
        return _FakeSessionResponse(status_code=self.next_status)


# ---------------------------------------------------------------------------
# Helpers — build a bypassed mixin wired for Cloud or DC mode
# ---------------------------------------------------------------------------


def _make_mixin(
    mixin_cls: type,
    *,
    is_cloud: bool,
    workspace: str | None,
) -> Any:
    """Build an instance of ``mixin_cls`` without running its ``__init__``.

    Bypasses :class:`BitbucketClient.__init__` (which builds a real
    ``atlassian.Bitbucket`` transport and validates credentials) via
    :meth:`type.__new__`, then stamps the minimal attribute surface the
    mode-branching branches read at runtime:

    * ``mixin.config`` — a :class:`SimpleNamespace` carrying
      ``is_cloud``, ``workspace``, ``url``, ``ssl_verify`` and
      ``projects_filter`` (everything the Cloud / DC branches touch).
    * ``mixin.bitbucket`` — a :class:`_RecordingTransport` that
      records every outbound URL without issuing HTTP.

    The mixin's ``is_cloud`` property is inherited from
    :class:`BitbucketClient` and delegates to ``self.config.is_cloud``,
    so toggling the namespace flag is enough to select the branch.
    """
    mixin = mixin_cls.__new__(mixin_cls)
    mixin.config = SimpleNamespace(
        is_cloud=is_cloud,
        workspace=workspace,
        # A Cloud-shaped base URL for Cloud mode lets the direct-session
        # helpers build absolute URLs that our ``_RecordingSession``
        # strips cleanly. DC mode uses a representative on-prem host.
        url=(
            "https://api.bitbucket.org"
            if is_cloud
            else "https://stash.example.com"
        ),
        ssl_verify=True,
        projects_filter=None,
        timeout=75,
    )
    mixin.bitbucket = _RecordingTransport()
    return mixin


# ---------------------------------------------------------------------------
# Routing matrix — one row per mode-branching mixin method
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RoutingRow:
    """One mode-branching mixin method and its per-mode URL templates.

    Attributes
    ----------
    name:
        Short human-readable label used in parametrization ids and
        assertion messages.
    mixin_cls:
        The :class:`BitbucketClient` subclass that owns the method.
    invoke:
        Callable taking ``(mixin, workspace, repo_slug, cloud_pk,
        dc_pk, pr_id, commit_sha, webhook_id, account_id,
        branch_name, tag_name, report_key)`` that calls the method
        with the appropriate arguments. Every row uses the same
        invocation signature so the property test can drive every
        row uniformly.
    dc_prefix_fn / cloud_prefix_fn:
        Functions that build the expected URL prefix for DC / Cloud
        mode from the same bound variables the invoke callable uses.
        The property asserts that at least one recorded URL starts
        with the mode-appropriate prefix.
    """

    name: str
    mixin_cls: type
    invoke: Callable[..., Any]
    dc_prefix_fn: Callable[..., str]
    cloud_prefix_fn: Callable[..., str]
    # When True, the Cloud branch of this method does NOT issue any
    # HTTP — it raises ``ValueError("not_supported_on_cloud: ...")``
    # before reaching the wire. Used for tools whose Cloud equivalent
    # was removed by Atlassian (see CHANGE-2770 for
    # ``GET /2.0/workspaces`` / ``get_projects``). The test body
    # handles this row specially: Cloud branch asserts the exception,
    # DC branch still asserts the URL prefix as normal.
    cloud_raises_not_supported: bool = False


# Convenience aliases — every row's invoke and prefix functions take
# the same keyword arguments; defining them at module scope keeps the
# matrix readable.
def _cloud_repo_base(ws: str, repo: str) -> str:
    return f"/2.0/repositories/{ws}/{repo}"


def _dc_repo_base(pk: str, repo: str) -> str:
    return f"/rest/api/latest/projects/{pk}/repos/{repo}"


# The matrix is deliberately broad: every mixin method that carries an
# ``if self.is_cloud:`` branch appears here once. Each row's ``invoke``
# callable uses only the bound variables it cares about; unused kwargs
# are accepted via ``**_``.
ROUTING_MATRIX: tuple[_RoutingRow, ...] = (
    # ------------------------------------------------------------------
    # Repositories & projects (Req 8.1–8.6)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="get_projects",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, **_: m.get_projects(limit=5),
        dc_prefix_fn=lambda **_: "/rest/api/latest/projects",
        # Cloud branch no longer issues HTTP — it raises
        # ``ValueError("not_supported_on_cloud: ...")`` because
        # Atlassian removed ``GET /2.0/workspaces`` in CHANGE-2770.
        # The cloud_prefix_fn value below is vestigial and unused when
        # ``cloud_raises_not_supported=True``; we retain a sensible
        # placeholder so the matrix definition stays uniform.
        cloud_prefix_fn=lambda **_: "/2.0/workspaces",
        cloud_raises_not_supported=True,
    ),
    _RoutingRow(
        name="get_project",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, dc_pk, ws, **_: m.get_project(
            ws if m.is_cloud else dc_pk
        ),
        dc_prefix_fn=lambda *, dc_pk, **_: (
            f"/rest/api/latest/projects/{dc_pk}"
        ),
        cloud_prefix_fn=lambda *, ws, **_: f"/2.0/workspaces/{ws}",
    ),
    _RoutingRow(
        name="get_repositories",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, **_: m.get_repositories(
            ws if m.is_cloud else dc_pk, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, **_: (
            f"/rest/api/latest/projects/{dc_pk}/repos"
        ),
        cloud_prefix_fn=lambda *, ws, **_: f"/2.0/repositories/{ws}",
    ),
    _RoutingRow(
        name="get_repository",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_repository(
            ws if m.is_cloud else dc_pk, repo
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: _dc_repo_base(dc_pk, repo),
        cloud_prefix_fn=lambda *, ws, repo, **_: _cloud_repo_base(ws, repo),
    ),
    _RoutingRow(
        name="search_repositories",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, **_: m.search_repositories("query", limit=5),
        dc_prefix_fn=lambda **_: "/rest/api/latest/repos",
        cloud_prefix_fn=lambda *, ws, **_: f"/2.0/repositories/{ws}",
    ),
    _RoutingRow(
        name="get_file_content",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_file_content(
            ws if m.is_cloud else dc_pk, repo, "README.md"
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/browse/README.md"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/src/HEAD/README.md"
        ),
    ),
    _RoutingRow(
        name="get_raw_file_content",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_raw_file_content(
            ws if m.is_cloud else dc_pk, repo, "README.md"
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/raw/README.md"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/src/HEAD/README.md"
        ),
    ),
    _RoutingRow(
        name="browse_directory",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.browse_directory(
            ws if m.is_cloud else dc_pk, repo, path="src", at="main", limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/files/src"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/src/main/src"
        ),
    ),
    _RoutingRow(
        name="get_default_branch",
        mixin_cls=RepositoriesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_default_branch(
            ws if m.is_cloud else dc_pk, repo
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/default-branch"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: _cloud_repo_base(ws, repo),
    ),
    # ------------------------------------------------------------------
    # Pull requests (Req 9.1–9.8, 9.11, 9.12)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="get_pull_requests",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_pull_requests(
            ws if m.is_cloud else dc_pk, repo, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests"
        ),
    ),
    _RoutingRow(
        name="get_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.get_pull_request(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}"
        ),
    ),
    _RoutingRow(
        name="approve_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.approve_pull_request(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/approve"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/approve"
        ),
    ),
    _RoutingRow(
        name="decline_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.decline_pull_request(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/decline"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/decline"
        ),
    ),
    _RoutingRow(
        name="merge_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.merge_pull_request(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/merge"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/merge"
        ),
    ),
    _RoutingRow(
        name="get_pull_request_activities",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: (
            m.get_pull_request_activities(
                ws if m.is_cloud else dc_pk, repo, pr_id, limit=5
            )
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/activities"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/activity"
        ),
    ),
    _RoutingRow(
        name="get_pull_request_diff",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.get_pull_request_diff(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}.diff"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/diff"
        ),
    ),
    _RoutingRow(
        name="reopen_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.reopen_pull_request(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/reopen"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/reopen"
        ),
    ),
    _RoutingRow(
        name="request_changes_pull_request",
        mixin_cls=PullRequestsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: (
            m.request_changes_pull_request(
                ws if m.is_cloud else dc_pk, repo, pr_id, "some-user"
            )
        ),
        # DC routes this through ``set_pr_participant_status`` which
        # hits ``/participants/{username}`` — any recorded URL under the
        # DC PR path is acceptable.
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/participants"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/request-changes"
        ),
    ),
    # ------------------------------------------------------------------
    # Branches & tags (Req 10.1–10.5)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="get_branches",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_branches(
            ws if m.is_cloud else dc_pk, repo, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/branches"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/branches"
        ),
    ),
    _RoutingRow(
        name="create_branch",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, branch, **_: m.create_branch(
            ws if m.is_cloud else dc_pk, repo, branch, "deadbeef"
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/branches"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/branches"
        ),
    ),
    _RoutingRow(
        name="delete_branch",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, branch, **_: m.delete_branch(
            ws if m.is_cloud else dc_pk, repo, branch
        ),
        # DC uses the separate ``branch-utils`` plugin path for delete.
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"/rest/branch-utils/latest/projects/{dc_pk}/repos/{repo}/branches"
        ),
        cloud_prefix_fn=lambda *, ws, repo, branch, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/branches/{branch}"
        ),
    ),
    _RoutingRow(
        name="get_tags",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_tags(
            ws if m.is_cloud else dc_pk, repo, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/tags"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/tags"
        ),
    ),
    _RoutingRow(
        name="create_tag",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, tag, **_: m.create_tag(
            ws if m.is_cloud else dc_pk, repo, tag, "deadbeef"
        ),
        # DC tag create lives under /rest/git/latest per Req 10.1.
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"/rest/git/latest/projects/{dc_pk}/repos/{repo}/tags"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/tags"
        ),
    ),
    _RoutingRow(
        name="delete_tag",
        mixin_cls=BranchesMixin,
        invoke=lambda m, *, ws, dc_pk, repo, tag, **_: m.delete_tag(
            ws if m.is_cloud else dc_pk, repo, tag
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, tag, **_: (
            f"/rest/git/latest/projects/{dc_pk}/repos/{repo}/tags/{tag}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, tag, **_: (
            f"{_cloud_repo_base(ws, repo)}/refs/tags/{tag}"
        ),
    ),
    # ------------------------------------------------------------------
    # Commits (Req 11.1–11.11)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="get_commits",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.get_commits(
            ws if m.is_cloud else dc_pk, repo, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/commits"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/commits"
        ),
    ),
    _RoutingRow(
        name="get_commit",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: m.get_commit(
            ws if m.is_cloud else dc_pk, repo, sha
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/commits/{sha}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}"
        ),
    ),
    _RoutingRow(
        name="get_diff_commit",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: m.get_diff(
            ws if m.is_cloud else dc_pk, repo, commit_id=sha
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/commits/{sha}.diff"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/diff/{sha}"
        ),
    ),
    _RoutingRow(
        name="compare_commits",
        mixin_cls=CommitsMixin,
        # Use slugs that satisfy the Cloud pre-check (no ``/``/``?``/``#``/
        # whitespace) — the property only asserts routing, not the
        # compare-spec semantics.
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.compare_commits(
            ws if m.is_cloud else dc_pk, repo, "feature", "main", limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/compare/commits"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/diff/main..feature"
        ),
    ),
    _RoutingRow(
        name="get_commit_build_status",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: m.get_commit_build_status(
            sha,
            limit=5,
            project_key=(ws if m.is_cloud else dc_pk),
            repo_slug=repo,
        ),
        dc_prefix_fn=lambda *, sha, **_: (
            f"/rest/build-status/latest/commits/{sha}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}/statuses"
        ),
    ),
    _RoutingRow(
        name="post_commit_build_status",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: m.post_commit_build_status(
            sha,
            "SUCCESSFUL",
            "ci/build",
            project_key=(ws if m.is_cloud else dc_pk),
            repo_slug=repo,
        ),
        dc_prefix_fn=lambda *, sha, **_: (
            f"/rest/build-status/latest/commits/{sha}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}/statuses/build"
        ),
    ),
    _RoutingRow(
        name="search_code",
        mixin_cls=CommitsMixin,
        invoke=lambda m, *, ws, dc_pk, **_: m.search_code(
            "query",
            project_key=(ws if m.is_cloud else dc_pk),
            limit=5,
        ),
        dc_prefix_fn=lambda **_: "/rest/search/latest/search",
        cloud_prefix_fn=lambda *, ws, **_: (
            f"/2.0/workspaces/{ws}/search/code"
        ),
    ),
    # ------------------------------------------------------------------
    # Commit comments (Req 11.8, 11.9)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="list_commit_comments",
        mixin_cls=CommitCommentsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: m.list_commit_comments(
            ws if m.is_cloud else dc_pk, repo, sha
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/commits/{sha}/comments"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}/comments"
        ),
    ),
    # ------------------------------------------------------------------
    # Code insights (Req 12.1–12.3)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="list_code_insight_reports",
        mixin_cls=CodeInsightsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, **_: (
            m.list_code_insight_reports(
                ws if m.is_cloud else dc_pk, repo, sha, limit=5
            )
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, **_: (
            f"/rest/insights/1.0/projects/{dc_pk}/repos/{repo}"
            f"/commits/{sha}/reports"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}/reports"
        ),
    ),
    _RoutingRow(
        name="get_code_insight_report",
        mixin_cls=CodeInsightsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, report_key, **_: (
            m.get_code_insight_report(
                ws if m.is_cloud else dc_pk, repo, sha, report_key
            )
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, report_key, **_: (
            f"/rest/insights/1.0/projects/{dc_pk}/repos/{repo}"
            f"/commits/{sha}/reports/{report_key}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, report_key, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}/reports/{report_key}"
        ),
    ),
    _RoutingRow(
        name="list_code_insight_annotations",
        mixin_cls=CodeInsightsMixin,
        invoke=lambda m, *, ws, dc_pk, repo, sha, report_key, **_: (
            m.list_code_insight_annotations(
                ws if m.is_cloud else dc_pk, repo, sha, report_key, limit=5
            )
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, sha, report_key, **_: (
            f"/rest/insights/1.0/projects/{dc_pk}/repos/{repo}"
            f"/commits/{sha}/reports/{report_key}/annotations"
        ),
        cloud_prefix_fn=lambda *, ws, repo, sha, report_key, **_: (
            f"{_cloud_repo_base(ws, repo)}/commit/{sha}"
            f"/reports/{report_key}/annotations"
        ),
    ),
    # ------------------------------------------------------------------
    # Users (Req 13.1, 13.2)
    # ------------------------------------------------------------------
    _RoutingRow(
        name="get_user",
        mixin_cls=UsersMixin,
        # DC accepts any slug shape; Cloud requires an ``account_id``
        # so the two branches are fed different values.
        invoke=lambda m, *, account_id, **_: m.get_user(
            account_id if m.is_cloud else "jdoe"
        ),
        dc_prefix_fn=lambda **_: "/rest/api/latest/users/jdoe",
        cloud_prefix_fn=lambda *, account_id, **_: (
            f"/2.0/users/{account_id}"
        ),
    ),
    # ------------------------------------------------------------------
    # Webhooks (Req 16.4) — included because the property's Validates
    # list explicitly includes every mode-branching surface.
    # ------------------------------------------------------------------
    _RoutingRow(
        name="list_webhooks",
        mixin_cls=WebhooksMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.list_webhooks(
            ws if m.is_cloud else dc_pk, repo, limit=5
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/webhooks"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/hooks"
        ),
    ),
    _RoutingRow(
        name="get_webhook",
        mixin_cls=WebhooksMixin,
        invoke=lambda m, *, ws, dc_pk, repo, webhook_id, **_: m.get_webhook(
            ws if m.is_cloud else dc_pk, repo, webhook_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, webhook_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/webhooks/{webhook_id}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, webhook_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/hooks/{webhook_id}"
        ),
    ),
    _RoutingRow(
        name="create_webhook",
        mixin_cls=WebhooksMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.create_webhook(
            ws if m.is_cloud else dc_pk,
            repo,
            name="ci-webhook",
            url="https://hooks.example.com/x",
            events=["repo:push"],
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/webhooks"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/hooks"
        ),
    ),
    _RoutingRow(
        name="delete_webhook",
        mixin_cls=WebhooksMixin,
        invoke=lambda m, *, ws, dc_pk, repo, webhook_id, **_: m.delete_webhook(
            ws if m.is_cloud else dc_pk, repo, webhook_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, webhook_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/webhooks/{webhook_id}"
        ),
        cloud_prefix_fn=lambda *, ws, repo, webhook_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/hooks/{webhook_id}"
        ),
    ),
    # ------------------------------------------------------------------
    # Watching (Req 16.5) — DC uses ``/watch`` (singular); Cloud uses
    # ``/watchers`` (plural) via direct session PUT/DELETE.
    # ------------------------------------------------------------------
    _RoutingRow(
        name="watch_repo",
        mixin_cls=WatchingMixin,
        invoke=lambda m, *, ws, dc_pk, repo, **_: m.watch_repo(
            ws if m.is_cloud else dc_pk, repo
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/watch"
        ),
        cloud_prefix_fn=lambda *, ws, repo, **_: (
            f"{_cloud_repo_base(ws, repo)}/watchers"
        ),
    ),
    _RoutingRow(
        name="watch_pr",
        mixin_cls=WatchingMixin,
        invoke=lambda m, *, ws, dc_pk, repo, pr_id, **_: m.watch_pr(
            ws if m.is_cloud else dc_pk, repo, pr_id
        ),
        dc_prefix_fn=lambda *, dc_pk, repo, pr_id, **_: (
            f"{_dc_repo_base(dc_pk, repo)}/pull-requests/{pr_id}/watch"
        ),
        cloud_prefix_fn=lambda *, ws, repo, pr_id, **_: (
            f"{_cloud_repo_base(ws, repo)}/pullrequests/{pr_id}/watchers"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Invariant helpers
# ---------------------------------------------------------------------------


def _dc_prefix_is_dc_shaped(prefix: str) -> bool:
    """Return ``True`` iff ``prefix`` starts with a DC-shaped root.

    Requirement 20 allows the following DC roots:

    * ``/rest/api/latest/...``
    * ``/rest/branch-utils/latest/...``
    * ``/rest/git/latest/...``
    * ``/rest/insights/1.0/...``
    * ``/rest/build-status/latest/...``
    * ``/rest/search/latest/...``

    This invariant is checked at matrix-definition time (via the
    parametrization itself) and at runtime against every recorded URL
    to guard against a future refactor that accidentally routes a DC
    call to a Cloud-shaped prefix.
    """
    return prefix.startswith(
        (
            "/rest/api/latest/",
            "/rest/branch-utils/latest/",
            "/rest/git/latest/",
            "/rest/insights/1.0/",
            "/rest/build-status/latest/",
            "/rest/search/latest/",
        )
    )


def _cloud_prefix_is_cloud_shaped(prefix: str) -> bool:
    """Return ``True`` iff ``prefix`` starts with a Cloud 2.0 root.

    Requirement 20 allows exactly three Cloud roots (design Section 4,
    9, 10, 11, 12, 13):

    * ``/2.0/repositories/...``
    * ``/2.0/workspaces/...``
    * ``/2.0/users/...``
    """
    return prefix.startswith(
        (
            "/2.0/repositories/",
            "/2.0/workspaces",
            "/2.0/users/",
        )
    )


# ---------------------------------------------------------------------------
# Property — the mandated function name lives here
# ---------------------------------------------------------------------------


# The function name is mandated by Requirement 20.1: the test
# SHALL include a single property-based test named
# ``test_bitbucket_tool_routes_to_correct_client_by_is_cloud``.


@pytest.mark.parametrize(
    "row",
    ROUTING_MATRIX,
    ids=[row.name for row in ROUTING_MATRIX],
)
@given(
    ws=slugs,
    dc_pk=project_keys,
    repo=slugs,
    pr_id=pr_ids,
    sha=commit_shas,
    webhook_id=webhook_ids,
    account_id=cloud_account_ids,
    branch=slugs,
    tag=slugs,
    report_key=slugs,
)
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_bitbucket_tool_routes_to_correct_client_by_is_cloud(
    row: _RoutingRow,
    ws: str,
    dc_pk: str,
    repo: str,
    pr_id: int,
    sha: str,
    webhook_id: int,
    account_id: str,
    branch: str,
    tag: str,
    report_key: str,
) -> None:
    """Property 8 — every mode-branching tool routes to the mode-
    appropriate URL prefix.

    For each ``row`` in :data:`ROUTING_MATRIX` and each mode
    ``m ∈ {"cloud", "dc"}`` the property asserts:

    1. The mixin method, invoked with the bound variables drawn by
       Hypothesis and recorded through a :class:`_RecordingTransport`,
       issues at least one HTTP call.
    2. At least one recorded URL starts with the mode-appropriate
       prefix returned by ``row.dc_prefix_fn`` / ``row.cloud_prefix_fn``.
    3. No recorded URL starts with the *other* mode's prefix shape —
       a Cloud invocation never emits a ``/rest/...`` URL, and a DC
       invocation never emits a ``/2.0/...`` URL.

    The property holds independent of the exact values of ``ws``,
    ``dc_pk``, ``repo``, etc., so a random sample from the generators
    above exercises every mixin method against a broad distribution
    of inputs.

    Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.1, 9.2,
    9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.11, 9.12, 10.1, 10.2, 10.3, 10.4,
    10.5, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9,
    11.10, 11.11, 12.1, 12.2, 12.3, 13.1, 13.2, 20.1, 20.2, 20.3,
    20.4, 20.5.
    """
    bound = {
        "ws": ws,
        "dc_pk": dc_pk,
        "repo": repo,
        "pr_id": pr_id,
        "sha": sha,
        "webhook_id": webhook_id,
        "account_id": account_id,
        "branch": branch,
        "tag": tag,
        "report_key": report_key,
    }

    # -------- Cloud mode ---------------------------------------------
    cloud_mixin = _make_mixin(
        row.mixin_cls, is_cloud=True, workspace=ws
    )

    if row.cloud_raises_not_supported:
        # Tools whose Cloud equivalent was removed by Atlassian (e.g.
        # ``get_projects`` after CHANGE-2770 deleted
        # ``GET /2.0/workspaces``) must raise pre-HTTP with an
        # actionable ``not_supported_on_cloud:`` message and issue
        # zero outbound calls. Validates Requirement 14.10 and the
        # post-CHANGE-2770 contract.
        with pytest.raises(ValueError, match=r"^not_supported_on_cloud:"):
            row.invoke(cloud_mixin, **bound)
        cloud_urls = list(cloud_mixin.bitbucket.urls)
        assert cloud_urls == [], (
            f"[{row.name}] Cloud branch issued HTTP despite being "
            f"flagged not-supported-on-cloud. Recorded URLs: "
            f"{cloud_urls!r}"
        )
    else:
        row.invoke(cloud_mixin, **bound)
        cloud_urls = list(cloud_mixin.bitbucket.urls)

        assert cloud_urls, (
            f"[{row.name}] Cloud invocation issued zero HTTP calls; "
            "expected at least one outbound Cloud 2.0 URL."
        )

        expected_cloud_prefix = row.cloud_prefix_fn(**bound)
        assert _cloud_prefix_is_cloud_shaped(expected_cloud_prefix), (
            f"[{row.name}] Matrix definition error: "
            f"expected_cloud_prefix {expected_cloud_prefix!r} is not a "
            "Cloud 2.0 shape."
        )
        assert any(u.startswith(expected_cloud_prefix) for u in cloud_urls), (
            f"[{row.name}] Cloud invocation did not target "
            f"{expected_cloud_prefix!r}. Recorded URLs: {cloud_urls!r}"
        )
        # Req 20.2 — Cloud invocation SHALL NOT fall through to DC.
        for recorded_url in cloud_urls:
            assert not recorded_url.startswith("/rest/"), (
                f"[{row.name}] Cloud invocation leaked a DC URL "
                f"{recorded_url!r}; every outbound path must live "
                "under /2.0/."
            )

    # -------- DC mode ------------------------------------------------
    dc_mixin = _make_mixin(
        row.mixin_cls, is_cloud=False, workspace=None
    )
    row.invoke(dc_mixin, **bound)
    dc_urls = list(dc_mixin.bitbucket.urls)

    assert dc_urls, (
        f"[{row.name}] DC invocation issued zero HTTP calls; expected "
        "at least one outbound /rest/... URL."
    )

    expected_dc_prefix = row.dc_prefix_fn(**bound)
    assert _dc_prefix_is_dc_shaped(expected_dc_prefix), (
        f"[{row.name}] Matrix definition error: expected_dc_prefix "
        f"{expected_dc_prefix!r} is not a DC shape."
    )
    assert any(u.startswith(expected_dc_prefix) for u in dc_urls), (
        f"[{row.name}] DC invocation did not target "
        f"{expected_dc_prefix!r}. Recorded URLs: {dc_urls!r}"
    )
    # Req 20.3 — DC invocation SHALL NOT fall through to Cloud.
    for recorded_url in dc_urls:
        assert not recorded_url.startswith("/2.0/"), (
            f"[{row.name}] DC invocation leaked a Cloud URL "
            f"{recorded_url!r}; every outbound path must live under "
            "/rest/."
        )
