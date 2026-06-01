"""Unit tests for the ``description_parser`` activity (task 2.2).

Validates Requirements: 5.3, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7,
11.8.

Strategy
--------

The parser is a pure function — no external collaborators, no async.
We exercise it with hand-crafted descriptions covering:

* No front-matter → ``None``.
* Well-formed block → all fields populated.
* Field-level validation: invalid values are dropped to ``None`` and
  recorded in ``parse_errors``.
* Edge cases: empty body, malformed YAML, missing ``ai-bot`` key,
  unrelated YAML at the top.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.description_parser import (  # noqa: E402
    ParsedFrontMatter,
    TIMEOUT_SECONDS_MAX,
    TIMEOUT_SECONDS_MIN,
    VALID_CLEANUP_POLICIES,
    VALID_WORKFLOW_TYPES,
    parse_description_frontmatter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_description(yaml_body: str, *, body: str = "## Amaç\nDo a thing.") -> str:
    """Build a description with the given YAML body sandwiched in --- markers."""
    return f"---\n{yaml_body}\n---\n\n{body}"


# ---------------------------------------------------------------------------
# 1. No / malformed front-matter → None
# ---------------------------------------------------------------------------


class TestNoFrontMatter:
    """Front-matter absent / malformed → fall through to LLM path."""

    def test_none_description(self) -> None:
        assert parse_description_frontmatter(None) is None

    def test_empty_description(self) -> None:
        assert parse_description_frontmatter("") is None

    def test_plain_markdown(self) -> None:
        desc = "## Amaç\nFix the retry bug.\n\n## Kabul\n- works"
        assert parse_description_frontmatter(desc) is None

    def test_horizontal_rule_only(self) -> None:
        # A bare ``---`` line later in the body is **not** front-matter.
        desc = "## Amaç\n\nDo a thing.\n\n---\n\nMore notes."
        assert parse_description_frontmatter(desc) is None

    def test_unclosed_front_matter(self) -> None:
        # Opening ``---`` but no closing delimiter → not parsed.
        desc = "---\nai-bot:\n  workflow_type: noop_test\n\n## Amaç"
        assert parse_description_frontmatter(desc) is None

    def test_malformed_yaml(self) -> None:
        # Closing delimiter present but body is invalid YAML.
        desc = _make_description(
            "ai-bot:\n  workflow_type: [unclosed list"
        )
        assert parse_description_frontmatter(desc) is None

    def test_yaml_without_ai_bot_key(self) -> None:
        # Front-matter without the ``ai-bot`` mapping is not our block.
        desc = _make_description("title: My Task\nauthor: ada")
        assert parse_description_frontmatter(desc) is None

    def test_ai_bot_not_mapping(self) -> None:
        # ``ai-bot`` must be a mapping; a scalar means we treat the
        # block as not-ours.
        desc = _make_description("ai-bot: some_string")
        assert parse_description_frontmatter(desc) is None


# ---------------------------------------------------------------------------
# 2. Well-formed front-matter → fields populated
# ---------------------------------------------------------------------------


class TestHappyPath:
    """All fields validated and surfaced."""

    def test_full_block(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  workflow_type: code_change_with_test\n"
            "  repo: payment-service\n"
            "  branch: develop\n"
            "  needs_ssh: true\n"
            "  needs_docker: false\n"
            "  test_command: pytest -q\n"
            "  cleanup: on_success\n"
            "  timeout_seconds: 1800\n"
            "  web_search: false\n"
            "  output:\n"
            "    - type: jira_comment\n"
            "      params:\n"
            "        body: Done\n"
            "    - type: bitbucket_pr\n"
            "      params:\n"
            "        draft: true\n"
            "    - type: bitbucket_commit\n"
            "      params:\n"
            "        path: reports/PAY-42.md\n"
            "        content: ok\n"
        )

        result = parse_description_frontmatter(desc)

        assert result is not None
        assert isinstance(result, ParsedFrontMatter)
        assert result.workflow_type == "code_change_with_test"
        assert result.repo == "payment-service"
        assert result.branch == "develop"
        assert result.needs_ssh is True
        assert result.needs_docker is False
        assert result.execution_command == "pytest -q"
        assert result.cleanup == "on_success"
        assert result.timeout_seconds == 1800
        assert result.web_search is False
        assert result.output == [
            {"type": "jira_comment", "params": {"body": "Done"}},
            {"type": "bitbucket_create_pr", "params": {"draft": True}},
            {
                "type": "bitbucket_commit",
                "params": {"path": "reports/PAY-42.md", "content": "ok"},
            },
        ]
        assert result.parse_errors == []

    def test_partial_block_unspecified_fields_are_none(self) -> None:
        desc = _make_description(
            "ai-bot:\n  workflow_type: noop_test\n"
        )
        result = parse_description_frontmatter(desc)

        assert result is not None
        assert result.workflow_type == "noop_test"
        assert result.repo is None
        assert result.branch is None
        assert result.needs_ssh is None
        assert result.needs_docker is None
        assert result.execution_command is None
        assert result.cleanup is None
        assert result.timeout_seconds is None
        assert result.web_search is None
        assert result.output is None
        assert result.parse_errors == []

    def test_empty_ai_bot_block(self) -> None:
        # ``ai-bot:`` with no children → empty mapping → all None.
        desc = _make_description("ai-bot: {}")
        result = parse_description_frontmatter(desc)

        assert result is not None
        assert result == ParsedFrontMatter()

    def test_leading_whitespace_tolerated(self) -> None:
        # BOM / whitespace before the front-matter still parses.
        desc = "\n  \n---\nai-bot:\n  workflow_type: pr_review\n---\n"
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.workflow_type == "pr_review"

    def test_jira_markup_mangled_identifiers_are_restored(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  workflow*type: remote*ssh*test*only\n"
            "  needs*ssh: true\n"
            "  needs*docker: false\n"
            "  test*command: printf ok\n"
            "  output:\n"
            "    - type: jira*comment\n"
            "      params:\n"
            "        file*path: strict-live-e2e/out.md\n"
            "        project*key: johni*test\n"
        )

        result = parse_description_frontmatter(desc)

        assert result is not None
        assert result.workflow_type == "remote_ssh_test_only"
        assert result.needs_ssh is True
        assert result.needs_docker is False
        assert result.execution_command == "printf ok"
        assert result.output == [
            {
                "type": "jira_comment",
                "params": {
                    "file_path": "strict-live-e2e/out.md",
                    "project_key": "example_workspace",
                },
            },
        ]
        assert result.parse_errors == []


# ---------------------------------------------------------------------------
# 3. Field-level validation — invalid values land in parse_errors
# ---------------------------------------------------------------------------


class TestInvalidWorkflowType:
    def test_unknown_workflow_type_is_dropped(self) -> None:
        desc = _make_description(
            "ai-bot:\n  workflow_type: code_change\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.workflow_type is None
        assert any(
            "workflow_type" in err and "code_change" in err
            for err in result.parse_errors
        )

    def test_non_string_workflow_type(self) -> None:
        desc = _make_description("ai-bot:\n  workflow_type: 42\n")
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.workflow_type is None
        assert any("workflow_type" in err for err in result.parse_errors)


class TestInvalidCleanup:
    @pytest.mark.parametrize("bad_value", ["yes", "no", "remove", "True"])
    def test_invalid_cleanup_dropped(self, bad_value: str) -> None:
        desc = _make_description(f"ai-bot:\n  cleanup: {bad_value}\n")
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.cleanup is None
        assert any("cleanup" in err for err in result.parse_errors)

    @pytest.mark.parametrize(
        "good_value", sorted(VALID_CLEANUP_POLICIES)
    )
    def test_valid_cleanup_preserved(self, good_value: str) -> None:
        desc = _make_description(f"ai-bot:\n  cleanup: {good_value}\n")
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.cleanup == good_value
        assert result.parse_errors == []


class TestInvalidTimeoutSeconds:
    @pytest.mark.parametrize("bad_value", [
        TIMEOUT_SECONDS_MIN - 1,
        TIMEOUT_SECONDS_MAX + 1,
        0,
        -100,
        10_000,
    ])
    def test_out_of_range(self, bad_value: int) -> None:
        desc = _make_description(
            f"ai-bot:\n  timeout_seconds: {bad_value}\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.timeout_seconds is None
        assert any(
            "timeout_seconds" in err for err in result.parse_errors
        )

    def test_non_integer(self) -> None:
        desc = _make_description(
            'ai-bot:\n  timeout_seconds: "not a number"\n'
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.timeout_seconds is None
        assert any(
            "timeout_seconds" in err for err in result.parse_errors
        )

    def test_bool_rejected_as_int(self) -> None:
        # Booleans subclass int in Python — explicitly rejected.
        desc = _make_description(
            "ai-bot:\n  timeout_seconds: true\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.timeout_seconds is None
        assert any(
            "timeout_seconds" in err for err in result.parse_errors
        )

    @pytest.mark.parametrize("good_value", [
        TIMEOUT_SECONDS_MIN,
        TIMEOUT_SECONDS_MAX,
        300,
        1800,
        3600,
    ])
    def test_in_range(self, good_value: int) -> None:
        desc = _make_description(
            f"ai-bot:\n  timeout_seconds: {good_value}\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.timeout_seconds == good_value
        assert result.parse_errors == []


class TestInvalidBooleans:
    @pytest.mark.parametrize(
        "field_name", ["needs_ssh", "needs_docker", "web_search"]
    )
    def test_string_rejected(self, field_name: str) -> None:
        # Quoted strings are not valid YAML booleans.
        desc = _make_description(
            f'ai-bot:\n  {field_name}: "true"\n'
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert getattr(result, field_name) is None
        assert any(field_name in err for err in result.parse_errors)


class TestInvalidOutput:
    def test_output_not_list(self) -> None:
        desc = _make_description(
            "ai-bot:\n  output: jira_comment\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.output is None
        assert any("output" in err for err in result.parse_errors)

    def test_output_entry_unknown_type(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  output:\n"
            "    - type: jira_comment\n"
            "      params: {body: ok}\n"
            "    - type: nonexistent_action\n"
            "      params: {}\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        # Valid entry preserved, invalid one dropped.
        assert result.output == [
            {"type": "jira_comment", "params": {"body": "ok"}},
        ]
        assert any(
            "nonexistent_action" in err for err in result.parse_errors
        )

    def test_output_entry_missing_type(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  output:\n"
            "    - params: {body: ok}\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.output == []
        assert any("output[0]" in err for err in result.parse_errors)

    def test_output_entry_not_mapping(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  output:\n"
            "    - just_a_string\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.output == []
        assert any("output[0]" in err for err in result.parse_errors)

    def test_output_entry_params_missing_defaults_to_empty(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  output:\n"
            "    - type: jira_transition\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.output == [
            {"type": "jira_transition", "params": {}},
        ]
        assert result.parse_errors == []


class TestExecutionCommand:
    def test_command_list_is_joined(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  workflow_type: script_execute\n"
            "  commands:\n"
            "    - npm ci\n"
            "    - npm test\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.execution_command == "npm ci && npm test"

    def test_empty_command_is_rejected(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  workflow_type: remote_ssh_test_only\n"
            "  test_command: \"\"\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.execution_command is None
        assert any("test_command" in err for err in result.parse_errors)


# ---------------------------------------------------------------------------
# 4. Mixed valid/invalid — partial success preserved
# ---------------------------------------------------------------------------


class TestMixedValidity:
    def test_invalid_field_does_not_poison_others(self) -> None:
        desc = _make_description(
            "ai-bot:\n"
            "  workflow_type: code_change_with_test\n"
            "  cleanup: yes\n"            # invalid
            "  timeout_seconds: 1800\n"
            "  needs_ssh: true\n"
        )
        result = parse_description_frontmatter(desc)
        assert result is not None
        assert result.workflow_type == "code_change_with_test"
        assert result.cleanup is None
        assert result.timeout_seconds == 1800
        assert result.needs_ssh is True
        assert len(result.parse_errors) == 1
        assert "cleanup" in result.parse_errors[0]


# ---------------------------------------------------------------------------
# 5. Workflow type vocabulary — sanity check on the closed set
# ---------------------------------------------------------------------------


class TestWorkflowTypeVocabulary:
    def test_all_valid_workflow_types_accepted(self) -> None:
        for wf_type in sorted(VALID_WORKFLOW_TYPES):
            desc = _make_description(
                f"ai-bot:\n  workflow_type: {wf_type}\n"
            )
            result = parse_description_frontmatter(desc)
            assert result is not None
            assert result.workflow_type == wf_type
            assert result.parse_errors == []
