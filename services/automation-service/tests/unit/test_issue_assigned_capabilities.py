"""Unit tests for issue-assigned workflow capability shaping."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _SERVICE_ROOT.parents[1]
for _path in (
    _SERVICE_ROOT,
    _SERVICE_ROOT / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from automation_service.webhooks_issue_assigned import (  # noqa: E402
    _simple_capabilities,
)


@dataclass
class _Cred:
    present: bool = False

    def has_credential(self) -> bool:
        return self.present


@dataclass
class _Bot:
    jira: _Cred | None = None
    bitbucket: _Cred | None = None
    confluence: _Cred | None = None


@dataclass
class _Dept:
    bot: _Bot
    web_search_enabled: bool = False
    available_capabilities: tuple[str, ...] = ()


def test_db_runner_assignment_capability_is_preserved() -> None:
    dept = _Dept(
        bot=_Bot(jira=_Cred(True)),
        available_capabilities=("jira", "execution"),
    )

    assert _simple_capabilities(dept, {}) == ("execution", "jira")


def test_env_runner_flag_still_grants_execution() -> None:
    dept = _Dept(bot=_Bot(jira=_Cred(True)))

    assert _simple_capabilities(
        dept,
        {"EXECUTION_RUNNER_ASSIGNED": "true"},
    ) == ("execution", "jira")
