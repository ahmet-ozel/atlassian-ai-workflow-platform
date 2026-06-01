"""Unit tests for ``temporal_shared.identifiers`` — workflow ID, branch, and
artifact-key formatters.

Validated invariants (task 2.2 in ``.kiro/specs/p0-critical-path/tasks.md``):

1. Each formatter produces the exact string template documented in design.md
   §Workflow ID ve Idempotency Şeması.
2. ``issue_key`` validation rejects strings not matching
   ``^[A-Z][A-Z0-9_]+-[1-9][0-9]*$``.
3. ``workspace``/``repo`` validation rejects strings not matching
   ``^[a-z0-9][a-z0-9-]*$``.
4. No formatter imports or calls ``datetime``, ``random``, or ``uuid``.
"""

from __future__ import annotations

import inspect

import pytest

from temporal_shared.identifiers import (
    InvalidIssueKeyError,
    InvalidSlugError,
    agent_artifact_key,
    agent_workflow_id,
    automation_workflow_id_bb,
    automation_workflow_id_jira,
    branch_name,
    execution_artifact_key,
    execution_workflow_id,
)


# ---------------------------------------------------------------------------
# automation_workflow_id_jira
# ---------------------------------------------------------------------------


class TestAutomationWorkflowIdJira:
    def test_basic_format(self) -> None:
        assert automation_workflow_id_jira("PAY-4211") == "automation-jira-PAY-4211"

    def test_single_digit_issue(self) -> None:
        assert automation_workflow_id_jira("HR-1") == "automation-jira-HR-1"

    def test_underscore_in_project(self) -> None:
        assert automation_workflow_id_jira("ABC_DEF-123") == "automation-jira-ABC_DEF-123"

    def test_numeric_in_project(self) -> None:
        assert automation_workflow_id_jira("A2B-99") == "automation-jira-A2B-99"

    @pytest.mark.parametrize(
        "bad_key",
        [
            "pay-1",        # lowercase
            "PAY-0",        # leading zero
            "PAY-01",       # leading zero
            "-PAY-1",       # starts with dash
            "1PAY-1",       # starts with digit
            "PAY",          # no dash+number
            "PAY-",         # no number after dash
            "",             # empty
            "PAY-1 ",       # trailing space
            " PAY-1",       # leading space
        ],
    )
    def test_invalid_issue_key_raises(self, bad_key: str) -> None:
        with pytest.raises(InvalidIssueKeyError):
            automation_workflow_id_jira(bad_key)


# ---------------------------------------------------------------------------
# automation_workflow_id_bb
# ---------------------------------------------------------------------------


class TestAutomationWorkflowIdBb:
    def test_basic_format(self) -> None:
        assert (
            automation_workflow_id_bb("example-co", "payment-service", 42)
            == "automation-bb-example-co-payment-service-42"
        )

    def test_single_char_slug(self) -> None:
        assert automation_workflow_id_bb("a", "b", 1) == "automation-bb-a-b-1"

    def test_numeric_start(self) -> None:
        assert automation_workflow_id_bb("1org", "2repo", 99) == "automation-bb-1org-2repo-99"

    @pytest.mark.parametrize(
        "workspace,repo",
        [
            ("Invalid", "repo"),       # uppercase
            ("valid", "-invalid"),      # starts with dash
            ("", "repo"),              # empty workspace
            ("valid", ""),             # empty repo
            ("valid!", "repo"),        # special char
            ("valid", "repo.name"),    # dot
        ],
    )
    def test_invalid_slug_raises(self, workspace: str, repo: str) -> None:
        with pytest.raises(InvalidSlugError):
            automation_workflow_id_bb(workspace, repo, 1)


# ---------------------------------------------------------------------------
# agent_workflow_id
# ---------------------------------------------------------------------------


class TestAgentWorkflowId:
    def test_basic_format(self) -> None:
        assert (
            agent_workflow_id("automation-jira-PAY-4211", 1)
            == "agent-automation-jira-PAY-4211-iter-1"
        )

    def test_nested_parent(self) -> None:
        assert (
            agent_workflow_id("automation-bb-org-repo-5", 3)
            == "agent-automation-bb-org-repo-5-iter-3"
        )

    def test_idempotent(self) -> None:
        """Same inputs always produce same output (determinism)."""
        result1 = agent_workflow_id("parent-id", 2)
        result2 = agent_workflow_id("parent-id", 2)
        assert result1 == result2


# ---------------------------------------------------------------------------
# execution_workflow_id
# ---------------------------------------------------------------------------


class TestExecutionWorkflowId:
    def test_basic_format(self) -> None:
        assert (
            execution_workflow_id("agent-automation-jira-PAY-4211-iter-1", 1700000000)
            == "exec-agent-automation-jira-PAY-4211-iter-1-1700000000"
        )

    def test_idempotent(self) -> None:
        result1 = execution_workflow_id("parent", 12345)
        result2 = execution_workflow_id("parent", 12345)
        assert result1 == result2


# ---------------------------------------------------------------------------
# branch_name
# ---------------------------------------------------------------------------


class TestBranchName:
    def test_basic_format(self) -> None:
        assert branch_name("PAY-4211", 1) == "ai/PAY-4211/iter-1"

    def test_higher_iteration(self) -> None:
        assert branch_name("HR-99", 3) == "ai/HR-99/iter-3"

    def test_invalid_issue_key_raises(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            branch_name("invalid", 1)


# ---------------------------------------------------------------------------
# agent_artifact_key
# ---------------------------------------------------------------------------


class TestAgentArtifactKey:
    def test_basic_format(self) -> None:
        assert (
            agent_artifact_key("PAY-4211", 1, "diff.patch")
            == "artifacts/PAY-4211/iter-1/diff.patch"
        )

    def test_nested_filename(self) -> None:
        assert (
            agent_artifact_key("HR-1", 2, "output.json")
            == "artifacts/HR-1/iter-2/output.json"
        )

    def test_invalid_issue_key_raises(self) -> None:
        with pytest.raises(InvalidIssueKeyError):
            agent_artifact_key("bad", 1, "file.txt")


# ---------------------------------------------------------------------------
# execution_artifact_key
# ---------------------------------------------------------------------------


class TestExecutionArtifactKey:
    def test_basic_format(self) -> None:
        wf_id = "exec-agent-automation-jira-PAY-4211-iter-1-1700000000"
        assert (
            execution_artifact_key(wf_id, "stdout.log")
            == f"executions/{wf_id}/stdout.log"
        )

    def test_different_names(self) -> None:
        assert (
            execution_artifact_key("wf-1", "stderr.log")
            == "executions/wf-1/stderr.log"
        )


# ---------------------------------------------------------------------------
# Purity invariant: no datetime/random/uuid
# ---------------------------------------------------------------------------


class TestPurityInvariant:
    """Verify the module does not import non-deterministic libraries."""

    def test_no_forbidden_imports(self) -> None:
        import temporal_shared.identifiers as mod

        source = inspect.getsource(mod)
        for forbidden in ("import datetime", "import random", "import uuid",
                          "from datetime", "from random", "from uuid"):
            assert forbidden not in source, (
                f"identifiers.py must not contain '{forbidden}' — "
                "all formatters must be pure string operations"
            )


# ---------------------------------------------------------------------------
# Round-trippable workflow_id (platform-mimari-workflows task 1.2, R2.1)
# ---------------------------------------------------------------------------
#
# These helpers add a structured ``WorkflowIdRef`` plus
# ``jira_workflow_id`` / ``bitbucket_pr_workflow_id`` / ``parse_workflow_id``
# alongside the existing foundation-spec formatters.  The unit tests below
# pin the documented format regexes from design.md Property 1:
#
#   ^automation-jira-[A-Z][A-Z0-9_]{1,9}-\d+$
#   ^automation-bb-[a-z0-9-]+-pr-\d+$
#
# A companion property-based test (task 1.4) exercises the round-trip
# invariant ``parse(format(x)) == x`` across a Hypothesis-generated input
# space.
import re

from temporal_shared.identifiers import (
    InvalidWorkflowIdError,
    WorkflowIdRef,
    bitbucket_pr_workflow_id,
    jira_workflow_id,
    parse_workflow_id,
)


_JIRA_WF_REGEX = re.compile(r"^automation-jira-[A-Z][A-Z0-9_]{1,9}-\d+$")
_BB_WF_REGEX = re.compile(r"^automation-bb-[a-z0-9-]+-pr-\d+$")


class TestJiraWorkflowId:
    def test_basic_format(self) -> None:
        assert jira_workflow_id("PAY", 4211) == "automation-jira-PAY-4211"

    def test_minimum_project_key_length(self) -> None:
        # Two characters (``[A-Z][A-Z0-9_]{1,9}`` requires ≥ 1 trailing char).
        assert jira_workflow_id("HR", 1) == "automation-jira-HR-1"

    def test_maximum_project_key_length(self) -> None:
        # Ten characters: the leading [A-Z] plus 9 trailing.
        assert (
            jira_workflow_id("ABCDEFGHIJ", 99)
            == "automation-jira-ABCDEFGHIJ-99"
        )

    def test_underscore_in_key(self) -> None:
        assert (
            jira_workflow_id("ABC_DEF", 123)
            == "automation-jira-ABC_DEF-123"
        )

    def test_digit_in_key(self) -> None:
        assert jira_workflow_id("A2B", 99) == "automation-jira-A2B-99"

    def test_matches_documented_regex(self) -> None:
        wf_id = jira_workflow_id("PAY", 4211)
        assert _JIRA_WF_REGEX.match(wf_id)

    @pytest.mark.parametrize(
        "project_key",
        [
            "P",                # only 1 char (regex requires ≥ 2)
            "ABCDEFGHIJK",      # 11 chars (max is 10)
            "pay",              # lowercase
            "1PAY",             # leading digit
            "_PAY",             # leading underscore
            "PAY!",             # special char
            "PAY-X",            # contains dash
            "",                 # empty
        ],
    )
    def test_invalid_project_key_raises(self, project_key: str) -> None:
        with pytest.raises(InvalidIssueKeyError):
            jira_workflow_id(project_key, 1)

    @pytest.mark.parametrize("issue_num", [0, -1, -42])
    def test_non_positive_issue_num_raises(self, issue_num: int) -> None:
        with pytest.raises(InvalidIssueKeyError):
            jira_workflow_id("PAY", issue_num)

    def test_bool_issue_num_raises(self) -> None:
        # ``bool`` is a subclass of ``int`` in Python; reject it explicitly so
        # callers cannot accidentally pass a flag in place of a number.
        with pytest.raises(InvalidIssueKeyError):
            jira_workflow_id("PAY", True)  # type: ignore[arg-type]


class TestBitbucketPrWorkflowId:
    def test_basic_format(self) -> None:
        assert (
            bitbucket_pr_workflow_id("payment-callbacks", 127)
            == "automation-bb-payment-callbacks-pr-127"
        )

    def test_single_char_slug(self) -> None:
        assert (
            bitbucket_pr_workflow_id("a", 1)
            == "automation-bb-a-pr-1"
        )

    def test_numeric_in_slug(self) -> None:
        assert (
            bitbucket_pr_workflow_id("repo123", 5)
            == "automation-bb-repo123-pr-5"
        )

    def test_matches_documented_regex(self) -> None:
        wf_id = bitbucket_pr_workflow_id("my-repo-42", 99)
        assert _BB_WF_REGEX.match(wf_id)

    @pytest.mark.parametrize(
        "repo_slug",
        [
            "",                # empty
            "-leading",        # leading dash
            "trailing-",       # trailing dash
            "double--dash",    # consecutive dashes
            "Upper",           # uppercase
            "with.dot",        # special char
            "with_under",      # underscore
            "has space",       # whitespace
        ],
    )
    def test_invalid_repo_slug_raises(self, repo_slug: str) -> None:
        with pytest.raises(InvalidSlugError):
            bitbucket_pr_workflow_id(repo_slug, 1)

    @pytest.mark.parametrize("pr_id", [0, -1, -99])
    def test_non_positive_pr_id_raises(self, pr_id: int) -> None:
        with pytest.raises(InvalidSlugError):
            bitbucket_pr_workflow_id("repo", pr_id)

    def test_bool_pr_id_raises(self) -> None:
        with pytest.raises(InvalidSlugError):
            bitbucket_pr_workflow_id("repo", True)  # type: ignore[arg-type]


class TestParseWorkflowId:
    def test_parses_jira_workflow_id(self) -> None:
        ref = parse_workflow_id("automation-jira-PAY-4211")
        assert ref == WorkflowIdRef(
            provider="jira",
            project_key="PAY",
            issue_num=4211,
        )

    def test_parses_bitbucket_workflow_id(self) -> None:
        ref = parse_workflow_id("automation-bb-payment-callbacks-pr-127")
        assert ref == WorkflowIdRef(
            provider="bitbucket",
            repo_slug="payment-callbacks",
            pr_id=127,
        )

    def test_parses_bitbucket_with_dashes_and_digits(self) -> None:
        ref = parse_workflow_id("automation-bb-my-repo-v2-pr-1")
        assert ref.provider == "bitbucket"
        assert ref.repo_slug == "my-repo-v2"
        assert ref.pr_id == 1

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",                                          # empty
            "automation-jira-",                          # truncated jira
            "automation-jira-PAY",                       # missing num
            "automation-jira-PAY-",                      # missing num after dash
            "automation-jira-pay-1",                     # lowercase project
            "automation-jira-PAY-01",                    # leading-zero issue num
            "automation-jira-PAY-0",                     # zero issue num
            "automation-jira-ABCDEFGHIJK-1",             # project key too long
            "automation-jira-A-1",                       # project key too short
            "automation-bb-",                            # truncated bb
            "automation-bb-repo-pr-",                    # missing pr_id
            "automation-bb-repo-pr-01",                  # leading-zero pr_id
            "automation-bb-repo-pr-0",                   # zero pr_id
            "automation-bb--repo-pr-1",                  # leading dash in slug
            "automation-bb-repo--name-pr-1",             # double dash in slug
            "automation-bb-repo--pr-1",                  # double dash before -pr-
            "automation-bb-Repo-pr-1",                   # uppercase slug
            "automation-other-foo-1",                    # unknown provider
            "automation-jira-PAY-4211-extra",            # trailing junk
        ],
    )
    def test_invalid_format_raises(self, bad_id: str) -> None:
        with pytest.raises(InvalidWorkflowIdError):
            parse_workflow_id(bad_id)

    def test_non_string_raises(self) -> None:
        with pytest.raises(InvalidWorkflowIdError):
            parse_workflow_id(123)  # type: ignore[arg-type]


class TestRoundTripInvariants:
    """``parse(format(x)) == x`` round-trip checked on representative inputs.

    Property 1 in design.md mandates this invariant for any
    ``(project_key, issue_num)`` Jira tuple and any
    ``(repo_slug, pr_id)`` Bitbucket tuple.  Hypothesis-driven coverage
    lives in ``tests/property/test_workflow_id.py`` (task 1.4); the
    examples below pin a handful of explicit cases so a regression in
    either direction is caught at unit-test scope as well.
    """

    @pytest.mark.parametrize(
        "project_key,issue_num",
        [
            ("PAY", 1),
            ("PAY", 4211),
            ("HR", 99),
            ("ABC_DEF", 12345),
            ("A2B", 7),
            ("ABCDEFGHIJ", 1),       # max-length project key
        ],
    )
    def test_jira_round_trip(self, project_key: str, issue_num: int) -> None:
        wf_id = jira_workflow_id(project_key, issue_num)
        ref = parse_workflow_id(wf_id)
        assert ref == WorkflowIdRef(
            provider="jira",
            project_key=project_key,
            issue_num=issue_num,
        )
        # And the inverse: re-formatting from the parsed ref reproduces s.
        assert (
            jira_workflow_id(ref.project_key, ref.issue_num)  # type: ignore[arg-type]
            == wf_id
        )

    @pytest.mark.parametrize(
        "repo_slug,pr_id",
        [
            ("a", 1),
            ("payment-callbacks", 127),
            ("my-repo-v2", 99),
            ("repo123", 5),
            ("a1b2c3", 42),
        ],
    )
    def test_bitbucket_round_trip(
        self, repo_slug: str, pr_id: int
    ) -> None:
        wf_id = bitbucket_pr_workflow_id(repo_slug, pr_id)
        ref = parse_workflow_id(wf_id)
        assert ref == WorkflowIdRef(
            provider="bitbucket",
            repo_slug=repo_slug,
            pr_id=pr_id,
        )
        assert (
            bitbucket_pr_workflow_id(ref.repo_slug, ref.pr_id)  # type: ignore[arg-type]
            == wf_id
        )

    def test_jira_and_bitbucket_namespaces_are_disjoint(self) -> None:
        """No string is both a valid Jira and Bitbucket workflow_id."""
        # Sanity check: every Jira id starts ``automation-jira-`` and every
        # Bitbucket id starts ``automation-bb-``, so the two regexes are
        # mutually exclusive by prefix alone.
        jira_id = jira_workflow_id("PAY", 1)
        bb_id = bitbucket_pr_workflow_id("repo", 1)
        assert _JIRA_WF_REGEX.match(jira_id)
        assert not _BB_WF_REGEX.match(jira_id)
        assert _BB_WF_REGEX.match(bb_id)
        assert not _JIRA_WF_REGEX.match(bb_id)


class TestWorkflowIdRefDataclass:
    def test_is_frozen(self) -> None:
        ref = WorkflowIdRef(provider="jira", project_key="PAY", issue_num=1)
        with pytest.raises((AttributeError, TypeError)):
            ref.project_key = "OTHER"  # type: ignore[misc]

    def test_is_hashable(self) -> None:
        ref1 = WorkflowIdRef(provider="jira", project_key="PAY", issue_num=1)
        ref2 = WorkflowIdRef(provider="jira", project_key="PAY", issue_num=1)
        ref3 = WorkflowIdRef(provider="jira", project_key="PAY", issue_num=2)
        assert hash(ref1) == hash(ref2)
        assert {ref1, ref2, ref3} == {ref1, ref3}

    def test_equality_by_fields(self) -> None:
        a = WorkflowIdRef(provider="bitbucket", repo_slug="r", pr_id=1)
        b = WorkflowIdRef(provider="bitbucket", repo_slug="r", pr_id=1)
        c = WorkflowIdRef(provider="bitbucket", repo_slug="r", pr_id=2)
        assert a == b
        assert a != c
