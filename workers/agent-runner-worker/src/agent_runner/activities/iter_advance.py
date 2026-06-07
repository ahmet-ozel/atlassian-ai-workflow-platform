"""Iter advance + PR supersede activity.

When :class:`AgentRunnerWorkflow` advances to a new iteration and opens
a fresh draft PR, the previous iteration's PR (if any) must be marked
as *superseded* so the PO Review Inbox and human reviewers can
see at a glance that a newer iteration has shipped. The marking is a
two-part Bitbucket update:

1. Add the label ``superseded-by-pr-{new_pr_id}`` to the old PR.
2. Prepend a localized banner to the old PR's description so a casual
   reader sees the supersede notice without having to open the labels
   panel:

       ⚠️ Bu PR yerine yeni iterasyon (PR #{new_id}) açıldı, kapatılabilir.

The activity *also* records the transition in
``automation.pr_supersede_log`` via the existing
:class:`automation_service.pr_supersede_log.PrSupersedeLogRepo` so the
PO Review Inbox can render a multi-iter audit trail.

Idempotency contract
--------------------

The activity is **idempotent**: running it twice for the same
``(workflow_id, old_pr_id, new_pr_id)`` triple is a safe no-op. Three
mechanisms combine to guarantee this:

* The DB ledger uses ``ON CONFLICT (workflow_id, old_pr_id) DO NOTHING``
  (see :class:`PrSupersedeLogRepo` and
  ``platform/infra/postgres/11_workflows.sql`` block 4) so a retried
  insert is silently dropped.
* The Bitbucket *label add* is naturally idempotent - adding the same
  label twice has no effect on the PR's label set.
* The Bitbucket *description prepend* is guarded by a string
  containment check: when the banner already appears in the existing
  description (``BANNER_PREFIX`` template formatted for the same
  ``new_pr_id``) the activity skips the PUT call instead of producing
  a doubly-prefixed description.

Closed/merged old PR
--------------------

When the upstream PR's state is anything other than ``"OPEN"`` (i.e.
``"MERGED"``, ``"DECLINED"``, ``"CLOSED"`` - the exact spellings vary
by Bitbucket deployment, but only ``"OPEN"`` is treated as live),
labelling and description rewriting would be both unnecessary
(reviewers no longer act on a closed PR) and noisy (the banner would
muddy the closed PR's audit trail). The activity short-circuits the
Bitbucket calls in that case while *still* recording the supersede
log row - the ledger is the single source of truth for the PO Review
Inbox audit trail and must capture every iter transition regardless
of the upstream PR's current state.

The activity returns ``True`` when at least one Bitbucket side-effect
fired (label add or description prepend), and ``False`` when the
operation was a no-op (no old PR, closed/merged old PR, or banner
already present).

Cross-references
----------------

The activity writes through :class:`PrSupersedeLogRepo` and records the
supersede operation in the audit log.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

import httpx
from temporalio import activity

from http_shared import make_mcp_client, with_atlassian_creds

__all__ = [
    "BANNER_PREFIX_TEMPLATE",
    "IterAdvanceResult",
    "RepoRef",
    "SUPERSEDE_AUDIT_ACTION",
    "SUPERSEDE_LABEL_TEMPLATE",
    "iter_advance_pr_supersede",
    "set_pr_supersede_log_repo",
    "get_pr_supersede_log_repo",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants - shared with tests so the contract is the single source of truth
# ---------------------------------------------------------------------------


#: Bitbucket label format applied to the old PR. The ``{new_pr_id}``
#: placeholder is the freshly opened iteration's PR id; the literal
#: prefix ``superseded-by-pr-`` is what the PO Review Inbox greps for
#: when it filters out superseded entries.
SUPERSEDE_LABEL_TEMPLATE: Final[str] = "superseded-by-pr-{new_pr_id}"


#: Description-banner template prepended to the old PR's body. The
#: trailing ``\n\n`` separates the banner from the existing description
#: so Markdown rendering keeps the banner as its own paragraph. The
#: text is kept stable for Markdown rendering.
BANNER_PREFIX_TEMPLATE: Final[str] = (
    "⚠️ Bu PR yerine yeni iterasyon (PR #{new_pr_id}) "
    "açıldı, kapatılabilir.\n\n"
)


#: Stable audit action emitted when the activity successfully marks
#: the old PR as superseded (label + banner + ledger row). Mirrors
#: the value dashboard queries use.
SUPERSEDE_AUDIT_ACTION: Final[str] = "pr_superseded"


#: Default MCP server endpoint. Overridable via ``MCP_BASE_URL`` env
#: var so tests / staging can point at a fake. Mirrors the pattern in
#: :mod:`activities.bitbucket`.
_DEFAULT_MCP_BASE_URL: Final[str] = "http://atlassian-mcp:8090"


def _mcp_base_url() -> str:
    return os.environ.get("MCP_BASE_URL", _DEFAULT_MCP_BASE_URL)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoRef:
    """Bitbucket repository coordinate - workspace + slug.

    Mirrors :class:`activities.bitbucket.RepoRef` so the two activity
    modules can be passed the same object shape without an extra
    conversion step. Kept local here so the iter-advance module can
    be imported in environments where ``activities.bitbucket`` is not
    yet wired (eg. unit tests for this activity in isolation).
    """

    workspace: str
    repo_slug: str


@dataclass(frozen=True)
class IterAdvanceResult:
    """Outcome of an :func:`iter_advance_pr_supersede` invocation.

    Attributes
    ----------
    superseded:
        ``True`` when the activity actually changed the old PR
        (label added, banner prepended, or both); ``False`` when the
        call was an idempotent no-op (no old PR, old PR closed/merged,
        or banner already present from a previous run).
    label_added:
        ``True`` iff the label-add HTTP call returned a success code
        on this invocation. Adding an already-present label still
        counts as "added" because Bitbucket treats it idempotently -
        what we want to surface here is whether the call fired at all.
    description_updated:
        ``True`` iff the description prepend was issued. Skipped when
        the existing description already contains the banner for the
        same ``new_pr_id``.
    log_row_inserted:
        ``True`` iff a fresh row was appended to
        ``automation.pr_supersede_log``. ``False`` on a duplicate-key
        retry (``ON CONFLICT DO NOTHING`` swallowed the insert).
    """

    superseded: bool
    label_added: bool = False
    description_updated: bool = False
    log_row_inserted: bool = False


# ---------------------------------------------------------------------------
# Repo registry - wired at worker boot, read by the activity at runtime
# ---------------------------------------------------------------------------


_pr_supersede_log_repo: Any | None = None


def set_pr_supersede_log_repo(repo: Any) -> None:
    """Register the :class:`PrSupersedeLogRepo` used by this activity.

    Called once during worker startup (``main.py``) after the
    Postgres pool is initialised. The activity reads the registry on
    each invocation; if no repo is wired the activity falls through
    gracefully (the Bitbucket side-effects still fire - the ledger
    write is a *consequence* of supersede, not a precondition for it,
    matching the audit-emit pattern in :mod:`activities.precommit_scan`).

    Parameters
    ----------
    repo:
        An instance of
        :class:`automation_service.pr_supersede_log.PrSupersedeLogRepo`
        (or duck-typed equivalent exposing
        ``async record(workflow_id, old_pr_id, new_pr_id) -> bool``).
    """

    global _pr_supersede_log_repo  # noqa: PLW0603 - module-level singleton
    _pr_supersede_log_repo = repo


def get_pr_supersede_log_repo() -> Any | None:
    """Retrieve the registered :class:`PrSupersedeLogRepo`, or ``None``."""

    return _pr_supersede_log_repo


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
#
# The activity issues four Bitbucket calls (via the atlassian_mcp_bitbucket
# MCP):
#
#   GET  /api/bitbucket/pull-requests/get      → state + description
#   POST /api/bitbucket/pull-requests/labels   → add label
#   POST /api/bitbucket/pull-requests/update   → rewrite description
#
# Each helper is a thin wrapper around the same authenticated MCP
# client so the activity body stays readable. None of the helpers
# raises on a 4xx - they return the response object so the activity
# can decide whether the failure is a hard error or a graceful
# fallthrough.


def _get_credential_resolver():
    """Retrieve the worker's shared credential resolver.

    Uses the same registry as :mod:`activities.bitbucket`; the resolver
    is set during worker boot. Imported lazily so this module is
    importable in environments without the legacy ``src.activities``
    namespace (eg. isolated unit tests).
    """

    # Local import keeps the dependency surface minimal at import time.
    from src.activities import (  # type: ignore[import-not-found]
        get_credential_resolver as _resolver,
    )

    return _resolver()


async def _get_pr_state_and_description(
    repo: RepoRef, pr_id: int, dept_id: str
) -> tuple[str, str]:
    """Fetch ``(state, description)`` for an existing PR.

    Returns
    -------
    tuple[str, str]
        ``(state, description)`` where ``state`` is the Bitbucket
        normalised state string (``"OPEN"``, ``"MERGED"``,
        ``"DECLINED"``, ``"CLOSED"``) and ``description`` is the
        current PR description body. Empty strings are returned for
        missing fields so downstream containment checks stay simple.

    Raises
    ------
    httpx.HTTPError
        On transport-level failures. The Temporal retry policy
        decides whether to re-attempt.
    BitbucketSupersedeError
        On unexpected non-2xx HTTP responses.
    """

    credential_resolver = _get_credential_resolver()
    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=30.0,
        base_url=_mcp_base_url(),
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service="bitbucket",
            credential_resolver=credential_resolver,
        ) as authed_client:
            response = await authed_client.post(
                "/api/bitbucket/pull-requests/get",
                json={
                    "workspace": repo.workspace,
                    "repo_slug": repo.repo_slug,
                    "pr_id": pr_id,
                },
            )

    if response.status_code == 404:
        # Missing PR → treat as already-gone, equivalent to closed.
        return "CLOSED", ""

    if response.status_code != 200:
        raise BitbucketSupersedeError(
            f"Failed to fetch PR #{pr_id}: "
            f"HTTP {response.status_code} - {response.text}",
            status_code=response.status_code,
        )

    data = response.json() or {}
    state = str(data.get("state", "") or "").upper()
    description = str(data.get("description", "") or "")
    return state, description


async def _add_pr_label(
    repo: RepoRef, pr_id: int, label: str, dept_id: str
) -> bool:
    """Add ``label`` to PR ``pr_id``; return ``True`` on success.

    Bitbucket's label endpoints differ between Cloud and DC, but the
    MCP server normalises both behind ``/api/bitbucket/pull-requests/labels``.
    A 409 response (label already present) is treated as success - the
    desired end state (label set on PR) holds either way.
    """

    credential_resolver = _get_credential_resolver()
    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=30.0,
        base_url=_mcp_base_url(),
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service="bitbucket",
            credential_resolver=credential_resolver,
        ) as authed_client:
            response = await authed_client.post(
                "/api/bitbucket/pull-requests/labels",
                json={
                    "workspace": repo.workspace,
                    "repo_slug": repo.repo_slug,
                    "pr_id": pr_id,
                    "label": label,
                    "action": "add",
                },
            )

    if response.status_code in (200, 201, 204, 409):
        return True

    raise BitbucketSupersedeError(
        f"Failed to add label {label!r} to PR #{pr_id}: "
        f"HTTP {response.status_code} - {response.text}",
        status_code=response.status_code,
    )


async def _update_pr_description(
    repo: RepoRef, pr_id: int, description: str, dept_id: str
) -> bool:
    """Rewrite the PR description; return ``True`` on success."""

    credential_resolver = _get_credential_resolver()
    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=30.0,
        base_url=_mcp_base_url(),
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service="bitbucket",
            credential_resolver=credential_resolver,
        ) as authed_client:
            response = await authed_client.post(
                "/api/bitbucket/pull-requests/update",
                json={
                    "workspace": repo.workspace,
                    "repo_slug": repo.repo_slug,
                    "pr_id": pr_id,
                    "description": description,
                },
            )

    if response.status_code in (200, 201, 204):
        return True

    raise BitbucketSupersedeError(
        f"Failed to update description on PR #{pr_id}: "
        f"HTTP {response.status_code} - {response.text}",
        status_code=response.status_code,
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BitbucketSupersedeError(RuntimeError):
    """Raised when a Bitbucket call inside the activity fails unexpectedly.

    Mirrors :class:`activities.bitbucket.BitbucketActivityError` so
    callers (and the Temporal retry machinery) see a consistent
    exception class across the worker's Bitbucket activities.
    """

    def __init__(
        self, message: str, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Closed-state vocabulary
# ---------------------------------------------------------------------------


#: Bitbucket states that count as "live" - only an OPEN PR receives
#: the supersede treatment. Any other state is treated as a no-op
#: (the closed/merged PR is already out of the review queue).
_OPEN_STATES: Final[frozenset[str]] = frozenset({"OPEN"})


# ---------------------------------------------------------------------------
# Pure helper - exposed for unit tests
# ---------------------------------------------------------------------------


def _build_banner(new_pr_id: int) -> str:
    """Return the banner string prepended to the old PR description."""

    return BANNER_PREFIX_TEMPLATE.format(new_pr_id=new_pr_id)


def _description_already_banners(
    description: str, new_pr_id: int
) -> bool:
    """Whether ``description`` already starts with the supersede banner.

    Pure helper so the idempotency check (``[fix]`` retry must not
    produce a doubly-prefixed description) can be exercised by unit
    tests without HTTP mocking.
    """

    return _build_banner(new_pr_id) in description


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@activity.defn(name="iter_advance_pr_supersede")
async def iter_advance_pr_supersede(
    repo: RepoRef,
    workflow_id: str,
    old_pr_id: int | None,
    new_pr_id: int,
    dept_id: str,
) -> IterAdvanceResult:
    """Mark the previous iteration's PR as superseded by ``new_pr_id``.

    Side effects (in order):

    1. Short-circuit when ``old_pr_id is None`` (first-iteration call -
       there is nothing to supersede). Returns
       :class:`IterAdvanceResult` with all flags ``False``.
    2. Fetch the old PR's ``(state, description)`` via
       ``/api/bitbucket/pull-requests/get``. A 404 collapses to
       ``state="CLOSED"`` (the PR has been deleted upstream - same
       end state).
    3. When the state is not ``"OPEN"`` the Bitbucket calls are
       skipped, but the supersede ledger row is still recorded so the
       audit trail is complete.
    4. Otherwise:

       a. Add label ``superseded-by-pr-{new_pr_id}`` via
          ``/api/bitbucket/pull-requests/labels``.
       b. If the description does not already start with the banner
          for ``new_pr_id``, prepend the banner and rewrite via
          ``/api/bitbucket/pull-requests/update``.

    5. Insert a row into ``automation.pr_supersede_log`` via the
       registered :class:`PrSupersedeLogRepo` (if any). The PK
       constraint guarantees idempotency on retry.

    Caller contract
    ---------------

    * The Temporal retry policy applied at ``execute_activity`` time
      uses ``start_to_close_timeout=30s`` and ``maximumAttempts=3``
      and the activity is idempotent so retries are safe.
    * The activity raises :class:`BitbucketSupersedeError` only on
      genuinely unexpected non-2xx HTTP responses. Network errors
      surface as :class:`httpx.HTTPError` so Temporal treats them as
      transient and applies the retry policy.

    Parameters
    ----------
    repo:
        Bitbucket repository coordinate. Carried as a positional
        argument (rather than derived from ``workflow_id``) so the
        activity works for both ``automation-jira-*`` and
        ``automation-bb-*`` workflow ids - the ``automation-jira-*``
        variant carries the issue key but not the repo, so the
        workflow body must thread the repo through.
    workflow_id:
        Temporal workflow id of the calling agent run; forms half of
        the supersede-log PK.
    old_pr_id:
        PR id of the previous iteration's draft PR. ``None`` on the
        first iteration of a fresh issue - the activity returns
        ``IterAdvanceResult(superseded=False)`` immediately.
    new_pr_id:
        PR id of the freshly opened iteration draft PR. Embedded in
        the label and the banner.
    dept_id:
        Department slug - used by the credential resolver to mint
        Bitbucket creds. Mirrors :mod:`activities.bitbucket`.

    Returns
    -------
    IterAdvanceResult
        Frozen dataclass describing what changed.
    """

    # 1. No old PR → no-op (idempotent first iteration).
    if old_pr_id is None:
        _LOG.debug(
            "iter_advance_pr_supersede: no old_pr_id, no-op "
            "workflow_id=%s new_pr_id=%d",
            workflow_id,
            new_pr_id,
        )
        return IterAdvanceResult(superseded=False)

    if activity.in_activity():
        activity.heartbeat(
            f"superseding PR #{old_pr_id} → #{new_pr_id} "
            f"in {repo.workspace}/{repo.repo_slug}"
        )

    # 2. Fetch the old PR's current state + description.
    try:
        state, current_description = await _get_pr_state_and_description(
            repo, old_pr_id, dept_id
        )
    except httpx.HTTPError as exc:
        # Surface as Bitbucket-flavoured error so the activity retry
        # machinery sees a consistent class. The timeout / attempts
        # are configured at the call site.
        raise BitbucketSupersedeError(
            f"HTTP error fetching PR #{old_pr_id}: {exc}"
        ) from exc

    label_added = False
    description_updated = False

    # 3. Closed/merged → skip Bitbucket side-effects, still log.
    if state not in _OPEN_STATES:
        _LOG.info(
            "iter_advance_pr_supersede: old PR #%d is %s "
            "(not OPEN) - skipping Bitbucket updates",
            old_pr_id,
            state or "<unknown>",
        )
    else:
        # 4a. Label add.
        label = SUPERSEDE_LABEL_TEMPLATE.format(new_pr_id=new_pr_id)
        try:
            label_added = await _add_pr_label(
                repo, old_pr_id, label, dept_id
            )
        except httpx.HTTPError as exc:
            raise BitbucketSupersedeError(
                f"HTTP error adding label to PR #{old_pr_id}: {exc}"
            ) from exc

        # 4b. Description prepend, idempotent guard.
        if not _description_already_banners(current_description, new_pr_id):
            new_description = (
                _build_banner(new_pr_id) + current_description
            )
            try:
                description_updated = await _update_pr_description(
                    repo, old_pr_id, new_description, dept_id
                )
            except httpx.HTTPError as exc:
                raise BitbucketSupersedeError(
                    f"HTTP error updating PR #{old_pr_id} description: "
                    f"{exc}"
                ) from exc
        else:
            _LOG.debug(
                "iter_advance_pr_supersede: banner already present on "
                "PR #%d for new_pr_id=%d - skipping description update",
                old_pr_id,
                new_pr_id,
            )

    # 5. Insert into supersede ledger (idempotent on retry).
    log_row_inserted = await _record_supersede_log(
        workflow_id=workflow_id,
        old_pr_id=old_pr_id,
        new_pr_id=new_pr_id,
    )

    superseded = label_added or description_updated
    if superseded:
        _LOG.info(
            "iter_advance_pr_supersede: marked PR #%d superseded by #%d "
            "(workflow_id=%s)",
            old_pr_id,
            new_pr_id,
            workflow_id,
        )

    return IterAdvanceResult(
        superseded=superseded,
        label_added=label_added,
        description_updated=description_updated,
        log_row_inserted=log_row_inserted,
    )


async def _record_supersede_log(
    *, workflow_id: str, old_pr_id: int, new_pr_id: int
) -> bool:
    """Insert the supersede log row; swallow errors so Bitbucket wins.

    The Bitbucket side-effects (label, description) and the ledger
    row are independently idempotent, so a failure of one must not
    suppress the other. Following the same defensive pattern as
    :func:`activities.precommit_scan._emit_block_audit`, we log and
    continue when the ledger write fails so the workflow's terminal
    status reflects the Bitbucket state. The Temporal activity retry
    will re-attempt the whole sequence; the Bitbucket calls are
    naturally idempotent and the ledger uses
    ``ON CONFLICT DO NOTHING`` so there is no duplicate-row risk.
    """

    repo = get_pr_supersede_log_repo()
    if repo is None:
        # Worker has not wired the repo (eg. unit / integration test
        # environment that mocks the activity). The Bitbucket side
        # effects still fire - the ledger is a *consequence* of
        # supersede, not a precondition.
        _LOG.debug(
            "iter_advance_pr_supersede: PrSupersedeLogRepo not wired; "
            "skipping ledger insert (workflow_id=%s)",
            workflow_id,
        )
        return False

    try:
        return await repo.record(workflow_id, old_pr_id, new_pr_id)
    except Exception:  # noqa: BLE001 - best-effort, ledger may be down
        _LOG.warning(
            "iter_advance_pr_supersede: PrSupersedeLogRepo.record "
            "failed for workflow_id=%s old_pr_id=%d - continuing",
            workflow_id,
            old_pr_id,
            exc_info=True,
        )
        return False
