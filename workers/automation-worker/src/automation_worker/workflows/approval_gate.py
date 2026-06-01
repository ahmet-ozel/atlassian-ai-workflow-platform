"""``ApprovalGateWorkflow`` — signal-based approval waiting mechanism.

Implements a Temporal workflow that blocks commit operations when
modified files match department-configured ``approval_required_paths``
regex patterns. The workflow waits for an authorized user to signal
``[approve]`` or ``[reject]`` via Jira comment, with a 4-hour timeout.

Responsibilities (design.md §8 "Approval Gate" and Requirements
11.1–11.8):

1. Match commit file paths against ``approval_required_paths`` regex
   patterns from department configuration.
2. If any file matches: block commit, post Jira comment with matched
   paths and approval instructions.
3. Wait for ``[approve]`` or ``[reject]`` signal from authorized user
   (case-insensitive).
4. Authorized ``[approve]``: continue workflow, allow commit.
5. Authorized ``[reject]``: cancel workflow, discard changes.
6. Unauthorized user signal: ignore, remain in waiting state.
7. 4-hour timeout: auto-cancel, post Jira comment.
8. Empty/undefined ``approval_required_paths``: skip approval check,
   continue directly.
9. Log all events to audit log (event type, matched paths, approver).

Determinism contract: The workflow body uses only Temporal-deterministic
primitives — ``workflow.now()``, ``workflow.execute_activity``,
signal handlers, and ``workflow.wait_condition``. No ``random`` /
``uuid.uuid4`` / ``os.environ`` / direct I/O.

Validates Requirements: **11.1** (regex path matching + block),
**11.2** (Jira comment with matched paths), **11.3** ([approve] signal),
**11.4** ([reject] signal), **11.5** (4-hour timeout), **11.6**
(unauthorized user ignored), **11.7** (empty paths → skip), **11.8**
(audit logging).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Timeout for the approval gate — 4 hours (Requirement 11.5).
APPROVAL_TIMEOUT: Final[timedelta] = timedelta(hours=4)

#: Activity name for posting Jira comments.
_ACT_JIRA_ADD_COMMENT: Final[str] = "jira_add_comment"

#: Activity name for writing audit log entries.
_ACT_AUDIT_WRITE: Final[str] = "audit_write"

#: Default activity timeout for short operations.
_SHORT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: Default retry policy for activities.
_DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalGateInput:
    """Input for the ApprovalGateWorkflow.

    Attributes:
        issue_key: The Jira issue key for context and comments.
        dept_id: Department identifier for configuration lookup.
        workflow_id: Parent workflow identifier for tracing.
        commit_files: List of file paths being committed.
        approval_required_paths: Regex patterns from department config
            that require approval when matched.
        approvers: List of authorized Jira account IDs who can
            approve or reject.
    """

    issue_key: str
    dept_id: str
    workflow_id: str
    commit_files: list[str] = field(default_factory=list)
    approval_required_paths: list[str] = field(default_factory=list)
    approvers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApprovalGateResult:
    """Result of the ApprovalGateWorkflow.

    Attributes:
        approved: Whether the commit was approved.
        timed_out: Whether the workflow timed out waiting for approval.
        approver_id: Jira account ID of the user who approved/rejected
            (None if timed out or skipped).
        matched_paths: List of file paths that matched approval patterns.
    """

    approved: bool
    timed_out: bool
    approver_id: str | None = None
    matched_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def match_approval_paths(
    commit_files: list[str],
    approval_required_paths: list[str],
) -> list[str]:
    """Match commit files against approval-required regex patterns.

    Returns the list of commit file paths that match at least one
    pattern. Uses ``re.search`` so patterns can match anywhere in
    the file path.

    This is a pure function — no side effects, deterministic output.

    Validates Requirement 11.1.
    """

    if not approval_required_paths or not commit_files:
        return []

    matched: list[str] = []
    compiled_patterns = []
    for pattern in approval_required_paths:
        try:
            compiled_patterns.append(re.compile(pattern))
        except re.error:
            # Skip invalid regex patterns gracefully
            continue

    for file_path in commit_files:
        for compiled in compiled_patterns:
            if compiled.search(file_path):
                matched.append(file_path)
                break  # One match is enough per file

    return matched


def is_authorized_approver(user_id: str, approvers: list[str]) -> bool:
    """Check if a user is in the authorized approvers list.

    Validates Requirement 11.6.
    """

    return user_id in approvers


def parse_approval_decision(decision: str) -> str | None:
    """Parse a decision string for [approve] or [reject] markers.

    Returns "approve", "reject", or None if neither marker is found.
    Case-insensitive matching (Requirements 11.3, 11.4).
    """

    lower = decision.lower()
    if "[approve]" in lower:
        return "approve"
    if "[reject]" in lower:
        return "reject"
    return None


# ---------------------------------------------------------------------------
# Formatting helpers (pure)
# ---------------------------------------------------------------------------


def _format_approval_request_comment(matched_paths: list[str]) -> str:
    """Format the Jira comment requesting approval.

    Includes matched file paths and approval/rejection instructions.
    Validates Requirement 11.2.
    """

    paths_list = "\n".join(f"  • `{p}`" for p in matched_paths)
    return (
        "🔒 **Onay Gerekli — Hassas Dosya Değişikliği**\n\n"
        "Aşağıdaki dosyalar onay gerektiren yollarda değişiklik "
        "içermektedir:\n\n"
        f"{paths_list}\n\n"
        "---\n"
        "• Onaylamak için: `[approve]` yazın\n"
        "• Reddetmek için: `[reject]` yazın\n\n"
        "⏱️ 4 saat içinde yanıt alınmazsa işlem otomatik iptal edilir."
    )


def _format_timeout_comment() -> str:
    """Format the Jira comment for approval timeout.

    Validates Requirement 11.5.
    """

    return (
        "⏱️ **Onay Zaman Aşımı**\n\n"
        "4 saat içinde yetkili kullanıcıdan onay alınamadı. "
        "İş akışı otomatik olarak iptal edildi.\n\n"
        "Değişiklikleri uygulamak için görevi yeniden atayın."
    )


def _format_rejection_comment(approver_id: str) -> str:
    """Format the Jira comment for rejection.

    Validates Requirement 11.4.
    """

    return (
        f"❌ **Değişiklik Reddedildi**\n\n"
        f"Yetkili kullanıcı ({approver_id}) tarafından reddedildi. "
        f"Kod değişiklikleri uygulanmayacak."
    )


def _format_approval_comment(approver_id: str) -> str:
    """Format the Jira comment for approval confirmation."""

    return (
        f"✅ **Değişiklik Onaylandı**\n\n"
        f"Yetkili kullanıcı ({approver_id}) tarafından onaylandı. "
        f"Commit işlemi devam ediyor."
    )


# ---------------------------------------------------------------------------
# ApprovalGateWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="ApprovalGateWorkflow")
class ApprovalGateWorkflow:
    """Signal-based approval gate workflow.

    Waits for [approve] or [reject] signal from an authorized Jira
    user. Blocks commit operations when files match department
    approval_required_paths patterns. Times out after 4 hours.

    Validates Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6,
    11.7, 11.8.
    """

    def __init__(self) -> None:
        """Initialize workflow state."""

        self._decision: str | None = None  # "approve" or "reject"
        self._decision_user_id: str | None = None

    @workflow.signal
    async def approval_received(self, user_id: str, decision: str) -> None:
        """Signal handler for approval/rejection comments.

        Called when a Jira comment containing [approve] or [reject]
        is detected. Only processes signals from authorized users;
        unauthorized signals are ignored (Requirement 11.6).

        Args:
            user_id: Jira account ID of the comment author.
            decision: The raw comment text containing [approve] or
                [reject] markers.
        """

        # Parse the decision from the comment text
        parsed = parse_approval_decision(decision)
        if parsed is None:
            # Comment doesn't contain [approve] or [reject] — ignore
            return

        # Check authorization — only process from authorized approvers
        # The approvers list is set during run() and accessed here.
        # Since signals can arrive before run() sets _approvers,
        # we store the signal data and let the wait_condition in run()
        # handle the authorization check.
        self._decision = parsed
        self._decision_user_id = user_id

    @workflow.run
    async def run(self, inp: ApprovalGateInput) -> ApprovalGateResult:
        """Execute the approval gate workflow.

        1. If approval_required_paths is empty/undefined, skip check
           (Requirement 11.7).
        2. Match commit files against patterns (Requirement 11.1).
        3. If no matches, skip check and continue.
        4. Post Jira comment with matched paths (Requirement 11.2).
        5. Log "requested" event to audit (Requirement 11.8).
        6. Wait for signal or timeout (Requirements 11.3–11.6).
        7. Process result and log event (Requirement 11.8).
        """

        # Requirement 11.7: Skip if approval_required_paths is empty
        if not inp.approval_required_paths:
            workflow.logger.info(
                "ApprovalGateWorkflow: No approval_required_paths "
                "configured for dept %s — skipping approval check.",
                inp.dept_id,
            )
            return ApprovalGateResult(
                approved=True,
                timed_out=False,
                approver_id=None,
                matched_paths=[],
            )

        # Requirement 11.1: Match commit files against patterns
        matched_paths = match_approval_paths(
            inp.commit_files, inp.approval_required_paths
        )

        # No matches — no approval needed, continue directly
        if not matched_paths:
            workflow.logger.info(
                "ApprovalGateWorkflow: No files matched "
                "approval_required_paths for %s — continuing.",
                inp.issue_key,
            )
            return ApprovalGateResult(
                approved=True,
                timed_out=False,
                approver_id=None,
                matched_paths=[],
            )

        # Requirement 11.2: Post Jira comment with matched paths
        await self._post_jira_comment(
            inp.issue_key,
            inp.dept_id,
            _format_approval_request_comment(matched_paths),
        )

        # Requirement 11.8: Log "requested" event to audit
        await self._write_audit_log(
            workflow_id=inp.workflow_id,
            issue_key=inp.issue_key,
            event_type="requested",
            matched_paths=matched_paths,
            approver_id=None,
        )

        # Requirement 11.3, 11.4, 11.5, 11.6: Wait for signal or timeout
        #
        # We use workflow.wait_condition with a timeout. The condition
        # checks that a decision has been made by an AUTHORIZED user.
        # Signals from unauthorized users set _decision but fail the
        # authorization check, so we reset and keep waiting.

        def _has_authorized_decision() -> bool:
            """Check if an authorized decision has been received."""

            if self._decision is None:
                return False
            # Requirement 11.6: Only authorized users can decide
            if not is_authorized_approver(
                self._decision_user_id or "", inp.approvers
            ):
                # Unauthorized — reset and keep waiting
                workflow.logger.info(
                    "ApprovalGateWorkflow: Ignoring signal from "
                    "unauthorized user %s for %s.",
                    self._decision_user_id,
                    inp.issue_key,
                )
                self._decision = None
                self._decision_user_id = None
                return False
            return True

        # Wait with 4-hour timeout (Requirement 11.5)
        timed_out = False
        try:
            await workflow.wait_condition(
                _has_authorized_decision,
                timeout=APPROVAL_TIMEOUT,
            )
        except TimeoutError:
            timed_out = True

        # Handle timeout (Requirement 11.5)
        if timed_out:
            workflow.logger.warning(
                "ApprovalGateWorkflow: Timed out waiting for approval "
                "on %s after 4 hours.",
                inp.issue_key,
            )

            # Post timeout comment to Jira
            await self._post_jira_comment(
                inp.issue_key,
                inp.dept_id,
                _format_timeout_comment(),
            )

            # Requirement 11.8: Log "timeout" event
            await self._write_audit_log(
                workflow_id=inp.workflow_id,
                issue_key=inp.issue_key,
                event_type="timeout",
                matched_paths=matched_paths,
                approver_id=None,
            )

            return ApprovalGateResult(
                approved=False,
                timed_out=True,
                approver_id=None,
                matched_paths=matched_paths,
            )

        # Process the authorized decision
        approver_id = self._decision_user_id or ""
        decision = self._decision

        if decision == "approve":
            # Requirement 11.3: Approved — continue workflow
            workflow.logger.info(
                "ApprovalGateWorkflow: Approved by %s for %s.",
                approver_id,
                inp.issue_key,
            )

            await self._post_jira_comment(
                inp.issue_key,
                inp.dept_id,
                _format_approval_comment(approver_id),
            )

            # Requirement 11.8: Log "approved" event
            await self._write_audit_log(
                workflow_id=inp.workflow_id,
                issue_key=inp.issue_key,
                event_type="approved",
                matched_paths=matched_paths,
                approver_id=approver_id,
            )

            return ApprovalGateResult(
                approved=True,
                timed_out=False,
                approver_id=approver_id,
                matched_paths=matched_paths,
            )

        else:
            # Requirement 11.4: Rejected — cancel workflow
            workflow.logger.info(
                "ApprovalGateWorkflow: Rejected by %s for %s.",
                approver_id,
                inp.issue_key,
            )

            await self._post_jira_comment(
                inp.issue_key,
                inp.dept_id,
                _format_rejection_comment(approver_id),
            )

            # Requirement 11.8: Log "rejected" event
            await self._write_audit_log(
                workflow_id=inp.workflow_id,
                issue_key=inp.issue_key,
                event_type="rejected",
                matched_paths=matched_paths,
                approver_id=approver_id,
            )

            return ApprovalGateResult(
                approved=False,
                timed_out=False,
                approver_id=approver_id,
                matched_paths=matched_paths,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post_jira_comment(
        self, issue_key: str, dept_id: str, body: str
    ) -> None:
        """Post a Jira comment, swallowing failures (best-effort)."""

        try:
            await workflow.execute_activity(
                _ACT_JIRA_ADD_COMMENT,
                args=[issue_key, body, dept_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "ApprovalGateWorkflow: jira_add_comment failed for %s "
                "— continuing",
                issue_key,
            )

    async def _write_audit_log(
        self,
        *,
        workflow_id: str,
        issue_key: str,
        event_type: str,
        matched_paths: list[str],
        approver_id: str | None,
    ) -> None:
        """Write an audit log entry for approval events.

        Requirement 11.8: Log event type, matched paths, and approver.
        """

        try:
            await workflow.execute_activity(
                _ACT_AUDIT_WRITE,
                args=[
                    {
                        "workflow_id": workflow_id,
                        "issue_key": issue_key,
                        "event_type": f"approval_{event_type}",
                        "matched_paths": matched_paths,
                        "approver_account_id": approver_id,
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "ApprovalGateWorkflow: audit_write failed for %s "
                "(event=%s) — continuing",
                issue_key,
                event_type,
            )
