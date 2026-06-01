"""``BotBranchRetention`` — daily Temporal cron for ``ai/*`` branch cleanup.

Validates Requirements: **R10.2** (workflows spec) — *"THE Platform SHALL
``bot_branch_retention`` adında bir Temporal cron workflow'u sağlar; bu
workflow her gün 30 günden eski ``ai/{issue_key}`` branch'lerini tarar,
ilgili Jira issue'su ``Done`` veya ``Closed`` durumundaysa branch'i
siler (MIMARI §16.16 N5)."*

Lifecycle (one cron tick)::

    1. ``list_bot_departments()``                       → tuple of dept ids
    2. for each dept:
         a. ``list_ai_branches(dept_id)``               → tuple of BotBranch
         b. for each branch (filtered by age):
              i.  ``get_jira_issue_status(dept_id, key)`` → str
              ii. if ``should_delete_branch(age, status)`` is True:
                    ``delete_ai_branch(dept_id, branch)``  (idempotent)
                    ``post_branch_retention_jira_comment(dept_id, key, branch)``
    3. return ``BotBranchRetentionReport(scanned, deleted, skipped, …)``

The workflow body is **pure orchestration** — every side effect goes
through a Temporal activity referenced by string name so the workflow
sandbox never imports network-side machinery (asyncpg / httpx / atlassian
clients).  The :func:`should_delete_branch` predicate is a pure function
(no time / random / I/O) which makes it trivially unit-testable as a
truth table and replay-safe inside the workflow.

Cron schedule registration
--------------------------

The workflow runs daily at **02:30 UTC**, half an hour before
``audit-prune-cron`` (03:00 UTC) so the two cron jobs do not contend
for the worker's activity slots.  Constants
:data:`BOT_BRANCH_RETENTION_WORKFLOW_ID`, :data:`AUTOMATION_TASK_QUEUE`
and :data:`BOT_BRANCH_RETENTION_CRON_SCHEDULE` expose the exact strings
the boot script needs (mirroring the ``audit_prune`` module's pattern).

Determinism contract (Spec 2 Property 2 / Property 11 parity)
-------------------------------------------------------------

Inside the workflow body we use **only** Temporal-deterministic
primitives:

* ``workflow.now()`` for the wallclock cutoff (never ``datetime.now``,
  ``time.time``, ``datetime.utcnow``).
* ``workflow.execute_activity(...)`` for every side-effecting step
  (no direct httpx / asyncpg calls).
* No ``random.*``, no ``uuid.uuid4()``, no ``os.environ`` reads.
* Activities are referenced **by string name** so the workflow module
  loads cleanly inside the Temporal sandbox even before the activity
  modules are wired (Spec 2 task 2.5+).

Idempotent run semantics (Property 10 / Property 8 parity)
----------------------------------------------------------

Running the workflow twice on the same day is a *safe* no-op:

* :func:`should_delete_branch` is a pure predicate — same inputs
  always yield the same decision.
* ``delete_ai_branch`` is idempotent at the activity layer: a second
  attempt against an already-deleted branch returns success without
  side effect (mirroring Bitbucket's ``DELETE`` semantics).
* The Jira "branch retention" comment activity de-dupes on the
  branch name so the same retention notice is not posted twice.

Therefore the workflow body itself does **not** carry a "did we run
today already?" guard — idempotence is delegated to the activities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy


# ---------------------------------------------------------------------------
# Public constants (re-exported via ``__init__``)
# ---------------------------------------------------------------------------

#: Temporal task queue on which ``automation-worker`` polls.  Re-exported
#: from this module so a future split between the gateway workflow and
#: the cron workflow does not require boot-script churn.
AUTOMATION_TASK_QUEUE: Final[str] = "automation-tq"

#: Stable workflow id used when scheduling the daily cron.  Reusing the
#: same id across worker restarts keeps a single in-flight cron lineage
#: rather than one per process restart.
BOT_BRANCH_RETENTION_WORKFLOW_ID: Final[str] = "bot-branch-retention-cron"

#: Cron schedule expression — daily at **02:30 UTC**.  Picked half an
#: hour before ``audit-prune-cron`` (03:00 UTC) so the two daily jobs
#: do not contend for the worker's activity slots.  5-field POSIX cron
#: syntax (minute, hour, day-of-month, month, day-of-week).
BOT_BRANCH_RETENTION_CRON_SCHEDULE: Final[str] = "30 2 * * *"

#: Hard retention cutoff for ``ai/*`` branches — branches older than
#: this AND linked to a closed Jira issue are eligible for deletion
#: (R10.2 / MIMARI §16.16 N5).
BRANCH_RETENTION_DAYS: Final[int] = 30

#: Closed Jira statuses for which the bot is allowed to clean up the
#: associated ``ai/*`` branch.  Both upper- and mixed-case are accepted
#: by the predicate via case-insensitive comparison so JIRA hosts that
#: localise the workflow status names (eg. "DONE", "Closed") still
#: match.  The set is defined here in canonical (capitalised) form to
#: mirror the spec language exactly.
CLOSED_JIRA_STATUSES: Final[frozenset[str]] = frozenset({"Done", "Closed"})


# ---------------------------------------------------------------------------
# Activity name constants (referenced by string only)
# ---------------------------------------------------------------------------

#: List the bot-managed department ids (one cron tick fans out across
#: all departments). Returns ``tuple[str, ...]``.
_ACT_LIST_BOT_DEPARTMENTS: Final[str] = "list_bot_departments"

#: List ``ai/*`` branches for one department. Returns ``tuple[BotBranch, ...]``.
_ACT_LIST_AI_BRANCHES: Final[str] = "list_ai_branches"

#: Fetch the Jira issue status for one ``(dept_id, issue_key)`` pair.
_ACT_GET_JIRA_ISSUE_STATUS: Final[str] = "get_jira_issue_status"

#: Delete one ``ai/*`` branch on Bitbucket.  Idempotent — repeated
#: calls against an already-deleted branch return success.
_ACT_DELETE_AI_BRANCH: Final[str] = "delete_ai_branch"

#: Post the "branch retained / branch removed" notification comment on
#: the linked Jira issue.  Idempotent: the activity de-dupes on
#: ``(issue_key, branch)`` so a re-run does not double-post.
_ACT_POST_RETENTION_COMMENT: Final[str] = "post_branch_retention_jira_comment"


# ---------------------------------------------------------------------------
# Activity options
# ---------------------------------------------------------------------------

#: ``list_bot_departments`` is a fast Postgres lookup against the
#: foundation ``departments`` table.
_LIST_DEPTS_TIMEOUT: Final[timedelta] = timedelta(seconds=15)

#: ``list_ai_branches`` walks Bitbucket's branch listing API for one
#: dept; capped at 5 minutes to absorb a large repo with many ``ai/``
#: branches (paginated).
_LIST_BRANCHES_TIMEOUT: Final[timedelta] = timedelta(minutes=5)

#: ``get_jira_issue_status`` is a single REST call.
_JIRA_STATUS_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

#: ``delete_ai_branch`` is a single Bitbucket ``DELETE``.
_DELETE_BRANCH_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

#: ``post_branch_retention_jira_comment`` is a single REST call.
_POST_COMMENT_TIMEOUT: Final[timedelta] = timedelta(seconds=30)

#: Default retry policy for idempotent side-effecting activities.
#: Three quick attempts cover transient outages without blocking the
#: next day's cron tick.
_DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotBranch:
    """A single ``ai/*`` branch surfaced by the listing activity.

    Attributes
    ----------
    department_id:
        Department slug owning the repo / branch.
    repo_slug:
        Bitbucket repo slug.
    branch_name:
        Full branch name (eg. ``"ai/PAY-4211"`` or
        ``"ai/PAY-4211-iter2"``).
    issue_key:
        Linked Jira issue key derived from the branch name.  ``None``
        when the branch does not follow the ``ai/{ISSUE_KEY}[-iter*]``
        convention; such branches are skipped (the workflow refuses to
        delete branches it cannot trace back to a Jira issue).
    last_commit_at:
        Wall-clock timestamp of the most recent commit on the branch
        (UTC).  Drives the age comparison inside the predicate.
    """

    department_id: str
    repo_slug: str
    branch_name: str
    issue_key: str | None
    last_commit_at: datetime


@dataclass(frozen=True, slots=True)
class BranchRetentionDecision:
    """Outcome for one inspected branch.

    Surfaces every consideration the workflow made so callers (admin
    UI, audit log) can reconstruct *why* a branch was kept or removed
    without re-deriving the rule.

    Attributes
    ----------
    branch:
        The :class:`BotBranch` the decision applies to.
    branch_age:
        Computed branch age (``workflow.now() - last_commit_at``).
    issue_status:
        Jira status string fetched for ``branch.issue_key``;
        ``None`` when the issue could not be resolved (eg. Jira API
        error or missing ``issue_key``).
    deleted:
        ``True`` when the branch was successfully deleted on this run.
    skip_reason:
        Stable category for non-deleted decisions:
        ``"too_young"``,
        ``"issue_not_closed"``,
        ``"issue_status_unknown"``,
        ``"missing_issue_key"``,
        ``"delete_failed"``,
        ``"jira_lookup_failed"``,
        or ``None`` when ``deleted is True``.
    """

    branch: BotBranch
    branch_age: timedelta
    issue_status: str | None
    deleted: bool
    skip_reason: str | None


@dataclass(frozen=True, slots=True)
class BotBranchRetentionReport:
    """Final result of a single :class:`BotBranchRetention` cron run.

    Attributes
    ----------
    scanned_branches:
        Number of branches inspected across all departments.
    deleted_branches:
        Number of branches removed on this run.
    skipped_too_young:
        Branches younger than :data:`BRANCH_RETENTION_DAYS`.
    skipped_issue_open:
        Branches whose linked issue is still open (status not in
        :data:`CLOSED_JIRA_STATUSES`).
    skipped_issue_unknown:
        Branches whose Jira status could not be resolved (lookup
        failed or ``issue_key`` could not be parsed).
    cutoff:
        ``workflow.now() - timedelta(days=BRANCH_RETENTION_DAYS)`` —
        echoed back so observers can reconstruct the slice without
        re-deriving it from the schedule.
    departments_scanned:
        Number of departments the cron tick fanned out across.
    """

    scanned_branches: int
    deleted_branches: int
    skipped_too_young: int
    skipped_issue_open: int
    skipped_issue_unknown: int
    cutoff: datetime
    departments_scanned: int


# ---------------------------------------------------------------------------
# Pure predicate (Property 8 parity)
# ---------------------------------------------------------------------------


def should_delete_branch(branch_age: timedelta, issue_status: str) -> bool:
    """Decide whether an ``ai/*`` branch is eligible for deletion.

    The predicate is the **single source of truth** for the deletion
    rule documented in design.md §"Property 8: Multi-iter / PO Review
    invariants" and in MIMARI §16.16 N5:

        ``True`` ⇔ ``branch_age > 30 days AND issue_status ∈ {Done, Closed}``

    Both conditions must hold — a young branch on a closed issue is
    kept (so a contributor can still rebase against it for a few days
    after merge), and an aged branch on an open issue is kept (so the
    bot does not accidentally remove ongoing work).

    Comparison rules:

    * ``branch_age`` uses *strict* inequality (``> 30 days``) so a
      branch that has not yet crossed the boundary survives the
      current cron tick and is reconsidered tomorrow.  This mirrors
      the requirement language ("30 günden eski") which excludes
      the boundary itself.
    * ``issue_status`` is compared **case-insensitively** against
      :data:`CLOSED_JIRA_STATUSES` so JIRA instances that surface
      ``"DONE"``, ``"closed"``, or other casings still match.  Empty
      / ``None`` / non-string inputs return ``False``.
    * ``branch_age`` must be non-negative.  A negative duration
      (eg. clock skew producing ``last_commit_at`` in the future)
      returns ``False`` — the branch is treated as "too young".

    Parameters
    ----------
    branch_age:
        How long ago the most recent commit landed on the branch.
    issue_status:
        Jira issue status string (canonical form is capitalised:
        ``"Done"``, ``"Closed"``).

    Returns
    -------
    bool
        ``True`` when both conditions hold; ``False`` otherwise.

    Examples
    --------
    >>> from datetime import timedelta
    >>> should_delete_branch(timedelta(days=31), "Done")
    True
    >>> should_delete_branch(timedelta(days=31), "Closed")
    True
    >>> should_delete_branch(timedelta(days=31), "DONE")  # case-insensitive
    True
    >>> should_delete_branch(timedelta(days=30), "Done")  # boundary kept
    False
    >>> should_delete_branch(timedelta(days=31), "In Progress")
    False
    >>> should_delete_branch(timedelta(days=29), "Done")
    False
    >>> should_delete_branch(timedelta(days=31), "")
    False
    """

    if not isinstance(issue_status, str):
        return False
    # Strict inequality — boundary value (exactly 30 days) is *kept*.
    if branch_age <= timedelta(days=BRANCH_RETENTION_DAYS):
        return False
    return issue_status.casefold() in {
        s.casefold() for s in CLOSED_JIRA_STATUSES
    }


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


@workflow.defn(name="BotBranchRetention")
class BotBranchRetention:
    """Daily Temporal cron — clean up stale bot ``ai/*`` branches.

    The workflow takes no input — every per-run parameter (cutoff,
    department list) is derived inside :meth:`run` so the cron schedule
    needs no per-tick payload.  Output is a typed
    :class:`BotBranchRetentionReport` summarising the slice the cron
    processed; details for individual branches stay in the audit log
    (emitted by the activity layer).
    """

    @workflow.run
    async def run(self) -> BotBranchRetentionReport:
        # 1. Compute the cutoff using the deterministic Temporal clock.
        #    ``workflow.now()`` is the only legal time source in a
        #    workflow body — replay must produce the identical value.
        now: datetime = workflow.now()
        cutoff: datetime = now - timedelta(days=BRANCH_RETENTION_DAYS)

        # 2. Resolve the department list.  An empty list is a valid
        #    outcome (eg. fresh deployment with no bot dept yet) — we
        #    still emit a clean report so the cron tick is observable.
        departments: tuple[str, ...] = await self._list_departments()

        scanned = 0
        deleted = 0
        skipped_too_young = 0
        skipped_issue_open = 0
        skipped_issue_unknown = 0

        for dept_id in departments:
            # 2a. List ``ai/*`` branches for this department.  A
            #     listing failure for one dept must not abort the
            #     other departments' cleanup — the activity handles
            #     its own retry budget; if it still fails after that
            #     we log and skip to the next dept.
            try:
                branches = await self._list_branches(dept_id)
            except Exception as exc:  # noqa: BLE001
                workflow.logger.warning(
                    "bot_branch_retention: list_ai_branches failed "
                    "for dept=%s: %s",
                    dept_id,
                    exc,
                )
                continue

            for branch in branches:
                scanned += 1
                age = now - branch.last_commit_at

                # Guard against negative age from clock skew.
                if age <= timedelta(days=BRANCH_RETENTION_DAYS):
                    skipped_too_young += 1
                    continue

                # Without an issue key we cannot ask Jira about the
                # status — refuse to delete (defensive).  This also
                # protects against branches that follow a future
                # naming convention the parser does not yet recognise.
                if not branch.issue_key:
                    skipped_issue_unknown += 1
                    continue

                # 2b.i — fetch Jira status; on lookup failure we keep
                # the branch (skip) rather than delete blindly.
                try:
                    issue_status = await self._get_issue_status(
                        dept_id, branch.issue_key
                    )
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "bot_branch_retention: jira status lookup "
                        "failed dept=%s issue=%s: %s",
                        dept_id,
                        branch.issue_key,
                        exc,
                    )
                    skipped_issue_unknown += 1
                    continue

                if not should_delete_branch(age, issue_status or ""):
                    skipped_issue_open += 1
                    continue

                # 2b.ii — delete + comment.  Both activities are
                # idempotent (Bitbucket DELETE returns 404 cleanly,
                # Jira comment activity de-dupes on (issue, branch)).
                try:
                    await self._delete_branch(dept_id, branch)
                except Exception as exc:  # noqa: BLE001
                    workflow.logger.warning(
                        "bot_branch_retention: delete_ai_branch "
                        "failed dept=%s branch=%s: %s",
                        dept_id,
                        branch.branch_name,
                        exc,
                    )
                    # Counted as "issue open" only when the issue is
                    # actually open; when delete itself failed we
                    # treat it as "skipped due to delete failure",
                    # which surfaces under ``skipped_issue_unknown``
                    # in the report (the operator sees the warning
                    # and can correlate via the audit log).
                    skipped_issue_unknown += 1
                    continue

                # Best-effort retention notice — a comment failure
                # must not roll back the (already successful) delete.
                await self._best_effort_post_comment(
                    dept_id, branch
                )
                deleted += 1

        return BotBranchRetentionReport(
            scanned_branches=scanned,
            deleted_branches=deleted,
            skipped_too_young=skipped_too_young,
            skipped_issue_open=skipped_issue_open,
            skipped_issue_unknown=skipped_issue_unknown,
            cutoff=cutoff,
            departments_scanned=len(departments),
        )

    # -- Internal activity wrappers --------------------------------------

    async def _list_departments(self) -> tuple[str, ...]:
        """Resolve the bot-managed department ids.

        Wrapped in a helper so a transient lookup failure can be
        caught at the workflow boundary; the activity itself already
        retries up to 3 times via ``_DEFAULT_RETRY``.  When the
        retries are exhausted we re-raise so Temporal records the
        cron run as failed and the next tick fires the day after —
        the alternative (silently skipping the entire run) would
        mask a misconfiguration.
        """

        result = await workflow.execute_activity(
            _ACT_LIST_BOT_DEPARTMENTS,
            start_to_close_timeout=_LIST_DEPTS_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        # Defensive coercion — accept tuples / lists / single strings.
        if result is None:
            return ()
        if isinstance(result, str):
            return (result,)
        try:
            return tuple(str(item) for item in result)
        except TypeError:
            workflow.logger.warning(
                "bot_branch_retention: list_bot_departments returned "
                "non-iterable %r; treating as empty list",
                type(result).__name__,
            )
            return ()

    async def _list_branches(self, dept_id: str) -> tuple[BotBranch, ...]:
        """List ``ai/*`` branches for one department."""

        result = await workflow.execute_activity(
            _ACT_LIST_AI_BRANCHES,
            args=[dept_id],
            start_to_close_timeout=_LIST_BRANCHES_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        if result is None:
            return ()
        coerced: list[BotBranch] = []
        for item in result:
            if isinstance(item, BotBranch):
                coerced.append(item)
                continue
            # Accept dict-shaped activity returns for tests / future
            # activity stubs that have not yet been migrated to the
            # frozen dataclass shape.
            if isinstance(item, dict):
                try:
                    coerced.append(
                        BotBranch(
                            department_id=str(
                                item.get("department_id") or dept_id
                            ),
                            repo_slug=str(item.get("repo_slug", "")),
                            branch_name=str(item.get("branch_name", "")),
                            issue_key=(
                                str(item["issue_key"])
                                if item.get("issue_key")
                                else None
                            ),
                            last_commit_at=item["last_commit_at"],
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    workflow.logger.warning(
                        "bot_branch_retention: skipping malformed "
                        "branch entry from list_ai_branches: %r",
                        item,
                    )
                continue
            workflow.logger.warning(
                "bot_branch_retention: ignoring unexpected "
                "list_ai_branches item shape %r",
                type(item).__name__,
            )
        return tuple(coerced)

    async def _get_issue_status(
        self, dept_id: str, issue_key: str
    ) -> str | None:
        """Fetch the Jira issue status string for one issue."""

        result = await workflow.execute_activity(
            _ACT_GET_JIRA_ISSUE_STATUS,
            args=[dept_id, issue_key],
            start_to_close_timeout=_JIRA_STATUS_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        if result is None:
            return None
        return str(result)

    async def _delete_branch(
        self, dept_id: str, branch: BotBranch
    ) -> None:
        """Delete one ``ai/*`` branch via the activity layer."""

        await workflow.execute_activity(
            _ACT_DELETE_AI_BRANCH,
            args=[dept_id, branch.repo_slug, branch.branch_name],
            start_to_close_timeout=_DELETE_BRANCH_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )

    async def _best_effort_post_comment(
        self, dept_id: str, branch: BotBranch
    ) -> None:
        """Post the retention notification comment, swallowing failures.

        The branch deletion has already succeeded by the time we
        reach this helper.  A comment failure is non-fatal: the
        operator can reconcile by reading the audit log emitted at
        the activity layer.
        """

        if branch.issue_key is None:
            # Should not happen — the workflow body filters on
            # ``branch.issue_key`` before reaching delete — but the
            # guard keeps the helper safe to call standalone.
            return
        try:
            await workflow.execute_activity(
                _ACT_POST_RETENTION_COMMENT,
                args=[dept_id, branch.issue_key, branch.branch_name],
                start_to_close_timeout=_POST_COMMENT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "bot_branch_retention: post_branch_retention_jira_"
                "comment failed dept=%s issue=%s branch=%s: %s",
                dept_id,
                branch.issue_key,
                branch.branch_name,
                exc,
            )


__all__: tuple[str, ...] = (
    "AUTOMATION_TASK_QUEUE",
    "BOT_BRANCH_RETENTION_CRON_SCHEDULE",
    "BOT_BRANCH_RETENTION_WORKFLOW_ID",
    "BRANCH_RETENTION_DAYS",
    "CLOSED_JIRA_STATUSES",
    "BotBranch",
    "BotBranchRetention",
    "BotBranchRetentionReport",
    "BranchRetentionDecision",
    "should_delete_branch",
)
