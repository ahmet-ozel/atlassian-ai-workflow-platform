"""Workflow ID, branch-name, and artifact-key formatters.

All functions in this module are **pure** — they perform only string
concatenation and regex validation.  None of them call ``datetime``,
``random``, ``uuid``, or any I/O.  This guarantees Temporal workflow
determinism when these helpers are invoked inside workflow code.

Workflow ID schema (design.md §Workflow ID ve Idempotency Şeması):

| Workflow              | ID Template                                      |
|-----------------------|--------------------------------------------------|
| AutomationWorkflow    | automation-jira-{ISSUE_KEY}                      |
| AutomationWorkflow PR | automation-bb-{workspace}-{repo}-{pr_id}         |
| AgentRunnerWorkflow   | agent-{parent_id}-iter-{N}                       |
| ExecutionRunWorkflow  | exec-{parent_id}-{ts}                            |

Branch naming: ``ai/{issue_key}/iter-{N}``
Artifact keys: ``artifacts/{issue_key}/iter-{N}/{filename}``
                ``executions/{workflow_id}/{name}``

In addition to the foundation helpers above, the
``platform-mimari-workflows`` spec (task 1.2, Requirement 2.1) defines a
**round-trippable** ``WorkflowIdRef`` dataclass and three companion
helpers:

* :func:`jira_workflow_id` — ``automation-jira-{PROJECT_KEY}-{ISSUE_NUM}``
* :func:`bitbucket_pr_workflow_id` — ``automation-bb-{REPO_SLUG}-pr-{PR_ID}``
* :func:`parse_workflow_id` — inverse of the two formatters above.

The format regexes are pinned by design:

* ``^automation-jira-[A-Z][A-Z0-9_]{1,9}-\\d+$``
* ``^automation-bb-[a-z0-9-]+-pr-\\d+$``

The round-trip invariant ``parse_workflow_id(format(x)) == x`` holds for
every valid input pair (Property 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "InvalidIssueKeyError",
    "InvalidSlugError",
    "InvalidWorkflowIdError",
    "WorkflowIdRef",
    "automation_workflow_id_jira",
    "automation_workflow_id_bb",
    "agent_workflow_id",
    "execution_workflow_id",
    "branch_name",
    "agent_artifact_key",
    "execution_artifact_key",
    "jira_workflow_id",
    "bitbucket_pr_workflow_id",
    "parse_workflow_id",
]

# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class InvalidIssueKeyError(ValueError):
    """Raised when an issue_key does not match the expected format."""

    def __init__(self, issue_key: str) -> None:
        super().__init__(
            f"Invalid issue_key {issue_key!r}: "
            f"must match {_ISSUE_KEY_RE.pattern}"
        )
        self.issue_key = issue_key


class InvalidSlugError(ValueError):
    """Raised when a workspace or repo slug is invalid."""

    def __init__(self, value: str, field: str) -> None:
        super().__init__(
            f"Invalid {field} {value!r}: must match {_SLUG_RE.pattern}"
        )
        self.value = value
        self.field = field


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _validate_issue_key(issue_key: str) -> None:
    if not _ISSUE_KEY_RE.match(issue_key):
        raise InvalidIssueKeyError(issue_key)


def _validate_slug(value: str, field: str) -> None:
    if not _SLUG_RE.match(value):
        raise InvalidSlugError(value, field)


# ---------------------------------------------------------------------------
# Workflow ID formatters
# ---------------------------------------------------------------------------


def automation_workflow_id_jira(issue_key: str) -> str:
    """Return the Temporal workflow ID for a Jira-triggered automation.

    Format: ``automation-jira-{ISSUE_KEY}``

    Example::

        >>> automation_workflow_id_jira("PAY-4211")
        'automation-jira-PAY-4211'
    """
    _validate_issue_key(issue_key)
    return f"automation-jira-{issue_key}"


def automation_workflow_id_bb(workspace: str, repo: str, pr_id: int) -> str:
    """Return the Temporal workflow ID for a Bitbucket PR automation.

    Format: ``automation-bb-{workspace}-{repo}-{pr_id}``

    Example::

        >>> automation_workflow_id_bb("example-co", "payment-service", 42)
        'automation-bb-example-co-payment-service-42'
    """
    _validate_slug(workspace, "workspace")
    _validate_slug(repo, "repo")
    return f"automation-bb-{workspace}-{repo}-{pr_id}"


def agent_workflow_id(parent_id: str, iteration: int) -> str:
    """Return the Temporal workflow ID for an AgentRunner child workflow.

    Format: ``agent-{parent_id}-iter-{N}``

    Example::

        >>> agent_workflow_id("automation-jira-PAY-4211", 1)
        'agent-automation-jira-PAY-4211-iter-1'
    """
    return f"agent-{parent_id}-iter-{iteration}"


def execution_workflow_id(parent_id: str, ts: int) -> str:
    """Return the Temporal workflow ID for an ExecutionRun child workflow.

    Format: ``exec-{parent_id}-{ts}``

    The *ts* parameter is an integer timestamp (e.g. Unix epoch seconds)
    produced by an activity or ``workflow.now()`` — never by the caller
    importing ``datetime`` directly.

    Example::

        >>> execution_workflow_id("agent-automation-jira-PAY-4211-iter-1", 1700000000)
        'exec-agent-automation-jira-PAY-4211-iter-1-1700000000'
    """
    return f"exec-{parent_id}-{ts}"


# ---------------------------------------------------------------------------
# Branch name formatter
# ---------------------------------------------------------------------------


def branch_name(issue_key: str, iteration: int) -> str:
    """Return the Git branch name for an AI-generated code change.

    Format: ``ai/{issue_key}/iter-{N}``

    Example::

        >>> branch_name("PAY-4211", 1)
        'ai/PAY-4211/iter-1'
    """
    _validate_issue_key(issue_key)
    return f"ai/{issue_key}/iter-{iteration}"


# ---------------------------------------------------------------------------
# Artifact key formatters
# ---------------------------------------------------------------------------


def agent_artifact_key(issue_key: str, iteration: int, filename: str) -> str:
    """Return the MinIO object key for an agent-produced artifact.

    Format: ``artifacts/{issue_key}/iter-{N}/{filename}``

    Example::

        >>> agent_artifact_key("PAY-4211", 1, "diff.patch")
        'artifacts/PAY-4211/iter-1/diff.patch'
    """
    _validate_issue_key(issue_key)
    return f"artifacts/{issue_key}/iter-{iteration}/{filename}"


def execution_artifact_key(workflow_id: str, name: str) -> str:
    """Return the MinIO object key for an execution-produced artifact.

    Format: ``executions/{workflow_id}/{name}``

    Example::

        >>> execution_artifact_key("exec-agent-automation-jira-PAY-4211-iter-1-1700000000", "stdout.log")
        'executions/exec-agent-automation-jira-PAY-4211-iter-1-1700000000/stdout.log'
    """
    return f"executions/{workflow_id}/{name}"


# ---------------------------------------------------------------------------
# Round-trippable workflow_id (platform-mimari-workflows task 1.2, R2.1)
# ---------------------------------------------------------------------------
#
# These pinned regexes come straight from design.md (Property 1):
#
#   Jira:      ^automation-jira-[A-Z][A-Z0-9_]{1,9}-\d+$
#   Bitbucket: ^automation-bb-[a-z0-9-]+-pr-\d+$
#
# The Jira project key body is bounded ([1, 10] alphanumeric/underscore
# characters following the leading uppercase letter), giving a 2..10 char
# project key — matching standard Jira conventions.
#
# The Bitbucket variant takes a single repo_slug (no workspace component)
# and uses the literal ``-pr-`` infix to disambiguate the pr_id from any
# trailing digits in the slug; the parser greedily anchors on
# ``-pr-{digits}$`` to recover (repo_slug, pr_id).

_JIRA_WF_ID_RE = re.compile(r"^automation-jira-([A-Z][A-Z0-9_]{1,9})-(\d+)$")
_BB_WF_ID_RE = re.compile(r"^automation-bb-([a-z0-9-]+)-pr-(\d+)$")

_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
# Repo slugs are non-empty, lowercase alnum + dashes.  We additionally
# forbid leading/trailing dashes and double dashes so the parser is
# unambiguous and so the regex above never produces the empty group on
# pathological inputs.
_REPO_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class InvalidWorkflowIdError(ValueError):
    """Raised when a workflow_id string cannot be parsed.

    Used by :func:`parse_workflow_id` when the input matches neither the
    Jira nor the Bitbucket pattern, or when individual components fail
    additional structural validation (for example, double dashes or
    leading/trailing dashes in a repo slug).
    """

    def __init__(self, value: str, *, reason: str = "unknown format") -> None:
        super().__init__(
            f"Invalid workflow_id {value!r}: {reason}; "
            f"expected one of "
            f"{_JIRA_WF_ID_RE.pattern!r} or {_BB_WF_ID_RE.pattern!r}"
        )
        self.value = value
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WorkflowIdRef:
    """Structured reference produced by :func:`parse_workflow_id`.

    Exactly one of the two source-specific tuples is populated, controlled
    by :attr:`provider`:

    * ``provider == "jira"`` →
      :attr:`project_key` and :attr:`issue_num` are set;
      :attr:`repo_slug` and :attr:`pr_id` are ``None``.
    * ``provider == "bitbucket"`` →
      :attr:`repo_slug` and :attr:`pr_id` are set;
      :attr:`project_key` and :attr:`issue_num` are ``None``.

    The dataclass is frozen so it is hashable and safe to use as a
    Hypothesis strategy output, dictionary key, or set member in
    deterministic workflow code.
    """

    provider: Literal["jira", "bitbucket"]
    project_key: str | None = None
    issue_num: int | None = None
    repo_slug: str | None = None
    pr_id: int | None = None


def jira_workflow_id(project_key: str, issue_num: int) -> str:
    """Format a Jira-triggered Temporal workflow ID.

    Format: ``automation-jira-{PROJECT_KEY}-{ISSUE_NUM}``

    Constraints (design.md Property 1):

    * ``project_key`` must match ``^[A-Z][A-Z0-9_]{1,9}$`` (2..10 chars,
      first uppercase letter, remainder uppercase / digit / underscore).
    * ``issue_num`` must be a positive integer.

    Example::

        >>> jira_workflow_id("PAY", 4211)
        'automation-jira-PAY-4211'
    """
    if not isinstance(project_key, str) or not _JIRA_PROJECT_KEY_RE.match(
        project_key
    ):
        raise InvalidIssueKeyError(
            f"{project_key}-{issue_num}" if isinstance(project_key, str) else "<non-str>"
        )
    if not isinstance(issue_num, int) or isinstance(issue_num, bool) or issue_num < 1:
        raise InvalidIssueKeyError(f"{project_key}-{issue_num}")
    return f"automation-jira-{project_key}-{issue_num}"


def bitbucket_pr_workflow_id(repo_slug: str, pr_id: int) -> str:
    """Format a Bitbucket-PR-triggered Temporal workflow ID.

    Format: ``automation-bb-{REPO_SLUG}-pr-{PR_ID}``

    Constraints (design.md Property 1):

    * ``repo_slug`` must be a non-empty lowercase alphanumeric string,
      possibly containing dashes — but no leading/trailing dash and no
      consecutive dashes (so the parser remains unambiguous).
    * ``pr_id`` must be a positive integer.

    Example::

        >>> bitbucket_pr_workflow_id("payment-callbacks", 127)
        'automation-bb-payment-callbacks-pr-127'
    """
    if not isinstance(repo_slug, str) or not _REPO_SLUG_RE.match(repo_slug):
        raise InvalidSlugError(
            repo_slug if isinstance(repo_slug, str) else "<non-str>",
            "repo_slug",
        )
    if "--" in repo_slug:
        raise InvalidSlugError(repo_slug, "repo_slug")
    if not isinstance(pr_id, int) or isinstance(pr_id, bool) or pr_id < 1:
        raise InvalidSlugError(f"pr_id={pr_id!r}", "pr_id")
    return f"automation-bb-{repo_slug}-pr-{pr_id}"


def parse_workflow_id(s: str) -> WorkflowIdRef:
    """Parse a formatted workflow_id back into its structured components.

    The function is the inverse of :func:`jira_workflow_id` and
    :func:`bitbucket_pr_workflow_id`.  For any valid input pair the
    round-trip property holds::

        parse_workflow_id(jira_workflow_id(pk, n))
            == WorkflowIdRef(provider="jira", project_key=pk, issue_num=n)

        parse_workflow_id(bitbucket_pr_workflow_id(slug, pr))
            == WorkflowIdRef(provider="bitbucket", repo_slug=slug, pr_id=pr)

    Raises:
        InvalidWorkflowIdError: when ``s`` matches neither pattern, or
            when the recovered components fail their structural checks
            (e.g. a Bitbucket slug with leading/trailing or doubled
            dashes — see :func:`bitbucket_pr_workflow_id`).
    """
    if not isinstance(s, str):
        raise InvalidWorkflowIdError(
            repr(s), reason="value is not a string"
        )

    jira_m = _JIRA_WF_ID_RE.match(s)
    if jira_m is not None:
        project_key, issue_num_str = jira_m.group(1), jira_m.group(2)
        # Reject leading-zero issue numbers ("PAY-007") to keep the
        # round-trip invariant: format(parse(s)) == s.
        if issue_num_str != "0" and issue_num_str.startswith("0"):
            raise InvalidWorkflowIdError(
                s, reason="issue_num must not have leading zeros"
            )
        issue_num = int(issue_num_str)
        if issue_num < 1:
            raise InvalidWorkflowIdError(
                s, reason="issue_num must be positive"
            )
        return WorkflowIdRef(
            provider="jira",
            project_key=project_key,
            issue_num=issue_num,
        )

    bb_m = _BB_WF_ID_RE.match(s)
    if bb_m is not None:
        repo_slug, pr_id_str = bb_m.group(1), bb_m.group(2)
        # The greedy ``[a-z0-9-]+`` group might match a slug ending in a
        # dash or containing ``--``; reject those so the round-trip
        # invariant holds (``format`` would refuse to emit them).
        if (
            not _REPO_SLUG_RE.match(repo_slug)
            or "--" in repo_slug
        ):
            raise InvalidWorkflowIdError(
                s, reason=f"invalid repo_slug {repo_slug!r}"
            )
        if pr_id_str != "0" and pr_id_str.startswith("0"):
            raise InvalidWorkflowIdError(
                s, reason="pr_id must not have leading zeros"
            )
        pr_id = int(pr_id_str)
        if pr_id < 1:
            raise InvalidWorkflowIdError(
                s, reason="pr_id must be positive"
            )
        return WorkflowIdRef(
            provider="bitbucket",
            repo_slug=repo_slug,
            pr_id=pr_id,
        )

    raise InvalidWorkflowIdError(s, reason="unknown format")
