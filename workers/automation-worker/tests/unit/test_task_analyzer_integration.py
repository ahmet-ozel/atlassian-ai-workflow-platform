"""Smoke test: ``analyze_task`` integrates with the *real* description_parser.

Most of the unit-test suite stubs ``description_parser`` so the analyzer
can be exercised in isolation.  This file replays the YAML happy-path
through the actual parser once, proving that:

* the import wiring works (``_try_parse_frontmatter`` finds the module);
* the dataclass attributes the analyzer reads (``workflow_type``,
  ``repo``, ``branch``, ``cleanup``, ``timeout_seconds``, ``web_search``,
  ``output``) are all present on the real :class:`ParsedFrontMatter`;
* the YAML branch wins over the LLM (LLM is never called).

Validates Requirements: 5.3, 5.4, 11.1, 20.6.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
for _candidate in (_SRC_DIR,):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities import task_analyzer  # noqa: E402
from automation_worker.activities.task_analyzer import (  # noqa: E402
    TaskAnalysisInput,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
)


@dataclass
class _RecordingLLM:
    """LLM that fails the test if it gets called - proves YAML wins."""

    calls: list[Any] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        return "{}"


@dataclass
class _RecordingCommenter:
    comments: list[tuple[str, str, str]] = field(default_factory=list)

    async def add_comment(
        self, issue_key: str, body: str, *, dept_id: str
    ) -> None:
        self.comments.append((issue_key, body, dept_id))


@pytest.fixture
def llm() -> _RecordingLLM:
    instance = _RecordingLLM()
    set_llm_caller(instance)
    return instance


@pytest.fixture
def commenter() -> _RecordingCommenter:
    instance = _RecordingCommenter()
    set_jira_commenter(instance)
    return instance


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    p = tmp_path / "task_analysis.md"
    p.write_text("# stub prompt\n", encoding="utf-8")
    set_prompt_path(p)
    yield p
    set_prompt_path(task_analyzer.DEFAULT_PROMPT_PATH)


_REAL_YAML_DESCRIPTION = """---
ai-bot:
  workflow_type: code_change_with_test
  repo: org/backend
  branch: develop
  test_command: pytest -q
  cleanup: always
  timeout_seconds: 600
  web_search: false
---

Add an exponential-backoff retry to the callback handler.
"""


def test_real_description_parser_drives_yaml_branch(
    prompt_path: Path,
    llm: _RecordingLLM,
    commenter: _RecordingCommenter,
) -> None:
    """End-to-end: real parser → analyzer → ready result without LLM."""
    inp = TaskAnalysisInput(
        issue_key="PAY-100",
        title="Add retry",
        description=_REAL_YAML_DESCRIPTION,
        labels=[],
        custom_fields={},
        dept_id="payments",
        dept_config={
            "available_repos": ["org/backend"],
            "web_search_enabled": True,
            "docker_defaults": {
                "cleanup_policy": "on_success",
                "default_timeout_seconds": 1800,
            },
        },
        trace_id="trace-int-001",
    )

    result = asyncio.run(analyze_task(inp))

    assert result.status == "ready"
    assert result.source == "yaml_frontmatter"
    assert result.workflow_type == "code_change_with_test"
    assert result.repo == "org/backend"
    assert result.branch == "develop"
    assert result.cleanup_policy == "always"
    assert result.timeout_seconds == 600
    assert result.confidence == 1.0
    # LLM is bypassed entirely.
    assert llm.calls == []
    # No comments posted on the happy path.
    assert commenter.comments == []
