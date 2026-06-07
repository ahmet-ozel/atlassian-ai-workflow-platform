"""Unit tests for ``temporal_shared.capabilities``.

Validates the :data:`WORKFLOW_TYPE_CAPABILITIES` mapping shape, the
:func:`derive_capabilities` rule table, and the :func:`gate` set-algebra
function.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

import pytest

from temporal_shared.capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    GateDecision,
    derive_capabilities,
    gate,
)


# ---------------------------------------------------------------------------
# Lightweight fakes that satisfy the structural protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BotEntry:
    """Bot entry test fake - the real one lives in a later task."""

    present: bool = True

    def has_credential(self) -> bool:
        return self.present


@dataclass
class _Bot:
    jira: _BotEntry | None = None
    bitbucket: _BotEntry | None = None
    confluence: _BotEntry | None = None


@dataclass
class _Dept:
    bot: _Bot = field(default_factory=_Bot)
    web_search_enabled: bool = False


def _empty_env() -> Mapping[str, str]:
    return {}


# ---------------------------------------------------------------------------
# WORKFLOW_TYPE_CAPABILITIES - structural shape
# ---------------------------------------------------------------------------


class TestMappingShape:
    """The mapping must match the design.md literal exactly."""

    EXPECTED: dict[str, frozenset[str]] = {
        "code_change_with_test": frozenset(
            {"jira_read", "jira_write", "bitbucket_read", "bitbucket_write", "execution"}
        ),
        "code_change_commit_only": frozenset(
            {"jira_read", "jira_write", "bitbucket_read", "bitbucket_write"}
        ),
        "pr_review": frozenset({"jira_read", "jira_write", "bitbucket_read"}),
        "confluence_doc_create": frozenset(
            {"jira_read", "jira_write", "confluence_read", "confluence_write"}
        ),
        "confluence_doc_update": frozenset(
            {"jira_read", "jira_write", "confluence_read", "confluence_write"}
        ),
        "research_basic": frozenset({"jira_read", "jira_write"}),
        "research_with_web": frozenset({"jira_read", "jira_write", "web_search"}),
        "multi_step": frozenset({"jira_read", "jira_write"}),
        "noop_test": frozenset({"jira_read"}),
        "remote_ssh_test_only": frozenset({"jira_read", "execution"}),
        "script_execute": frozenset({"jira_read", "jira_write", "execution"}),
        "research_publish_confluence": frozenset(
            {
                "jira_read",
                "jira_write",
                "confluence_read",
                "confluence_write",
                "web_search",
            }
        ),
        "research_summary_jira": frozenset({"jira_read", "jira_write"}),
    }

    def test_has_exactly_thirteen_entries(self) -> None:
        assert len(WORKFLOW_TYPE_CAPABILITIES) == 13

    def test_keys_match_design(self) -> None:
        assert set(WORKFLOW_TYPE_CAPABILITIES.keys()) == set(self.EXPECTED.keys())

    @pytest.mark.parametrize(
        "wf_type,expected",
        sorted(EXPECTED.items()),
        ids=sorted(EXPECTED.keys()),
    )
    def test_each_entry_matches_design(
        self, wf_type: str, expected: frozenset[str]
    ) -> None:
        assert WORKFLOW_TYPE_CAPABILITIES[wf_type] == expected

    def test_full_mapping_equals_design_literal(self) -> None:
        assert dict(WORKFLOW_TYPE_CAPABILITIES) == self.EXPECTED

    @pytest.mark.parametrize("wf_type", sorted(EXPECTED.keys()))
    def test_value_is_frozenset(self, wf_type: str) -> None:
        assert isinstance(WORKFLOW_TYPE_CAPABILITIES[wf_type], frozenset)

    def test_mapping_is_immutable_proxy(self) -> None:
        """

        ``MappingProxyType`` rejects mutation attempts with ``TypeError``.
        """
        assert isinstance(WORKFLOW_TYPE_CAPABILITIES, MappingProxyType)
        with pytest.raises(TypeError):
            # type: ignore[index]
            WORKFLOW_TYPE_CAPABILITIES["new_workflow"] = frozenset({"jira_read"})  # noqa: B018

    def test_capabilities_drawn_from_closed_vocabulary(self) -> None:
        allowed: frozenset[str] = frozenset(
            {
                "jira_read",
                "jira_write",
                "bitbucket_read",
                "bitbucket_write",
                "confluence_read",
                "confluence_write",
                "execution",
                "web_search",
            }
        )
        for wf_type, caps in WORKFLOW_TYPE_CAPABILITIES.items():
            unknown = caps - allowed
            assert not unknown, (
                f"workflow_type={wf_type!r} has unknown capabilities: "
                f"{sorted(unknown)}"
            )


# ---------------------------------------------------------------------------
# derive_capabilities - rule table
# ---------------------------------------------------------------------------


class TestDeriveCapabilities:
    """Each rule in the design is exercised in isolation."""

    def test_empty_dept_empty_env_yields_empty_caps(self) -> None:
        d = _Dept(bot=_Bot(), web_search_enabled=False)
        assert derive_capabilities(d, _empty_env()) == frozenset()

    def test_jira_credential_grants_jira_read_and_write(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry(present=True)))
        assert derive_capabilities(d, _empty_env()) == frozenset(
            {"jira_read", "jira_write"}
        )

    def test_jira_credential_absent_grants_no_jira_caps(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry(present=False)))
        assert derive_capabilities(d, _empty_env()) == frozenset()

    def test_bitbucket_credential_grants_bitbucket_read_and_write(self) -> None:
        d = _Dept(bot=_Bot(bitbucket=_BotEntry(present=True)))
        assert derive_capabilities(d, _empty_env()) == frozenset(
            {"bitbucket_read", "bitbucket_write"}
        )

    def test_confluence_credential_grants_confluence_read_and_write(self) -> None:
        d = _Dept(bot=_Bot(confluence=_BotEntry(present=True)))
        assert derive_capabilities(d, _empty_env()) == frozenset(
            {"confluence_read", "confluence_write"}
        )

    def test_all_three_credentials_grant_all_six_caps(self) -> None:
        d = _Dept(
            bot=_Bot(
                jira=_BotEntry(present=True),
                bitbucket=_BotEntry(present=True),
                confluence=_BotEntry(present=True),
            )
        )
        assert derive_capabilities(d, _empty_env()) == frozenset(
            {
                "jira_read",
                "jira_write",
                "bitbucket_read",
                "bitbucket_write",
                "confluence_read",
                "confluence_write",
            }
        )

    def test_ssh_host_env_grants_execution_capability(self) -> None:
        """

        Default flag values mean dept-pinning is *off*: presence of
        any ``SSH_HOST_<n>`` key in env grants ``execution`` regardless
        of department identity.
        """
        d = _Dept(bot=_Bot())
        env = {"SSH_HOST_1": "runner-a.example.com"}
        assert "execution" in derive_capabilities(d, env)

    def test_multiple_ssh_hosts_still_grants_execution_once(self) -> None:
        d = _Dept(bot=_Bot())
        env = {
            "SSH_HOST_1": "runner-a.example.com",
            "SSH_HOST_2": "runner-b.example.com",
        }
        caps = derive_capabilities(d, env)
        # frozenset never duplicates; assert we still have exactly the one entry
        assert "execution" in caps
        assert sum(1 for c in caps if c == "execution") == 1

    def test_ssh_runner_dept_pinning_flag_is_not_consulted(self) -> None:
        """

        Setting ``SSH_RUNNER_DEPT_PINNING_ENABLED`` to any value must not
        change the result of :func:`derive_capabilities` - the flag is
        outside this function's scope (default off).
        """
        d = _Dept(bot=_Bot())
        env_off = {"SSH_HOST_1": "h"}
        env_flag_true = {"SSH_HOST_1": "h", "SSH_RUNNER_DEPT_PINNING_ENABLED": "true"}
        env_flag_false = {"SSH_HOST_1": "h", "SSH_RUNNER_DEPT_PINNING_ENABLED": "false"}
        assert (
            derive_capabilities(d, env_off)
            == derive_capabilities(d, env_flag_true)
            == derive_capabilities(d, env_flag_false)
        )

    def test_ssh_dept_quota_flag_is_not_consulted(self) -> None:
        d = _Dept(bot=_Bot())
        env_off = {"SSH_HOST_1": "h"}
        env_flag_true = {"SSH_HOST_1": "h", "SSH_DEPT_QUOTA_ENABLED": "true"}
        assert derive_capabilities(d, env_off) == derive_capabilities(d, env_flag_true)

    def test_web_search_requires_dept_optin_and_firecrawl_flag(self) -> None:
        # Both off
        d_off = _Dept(bot=_Bot(), web_search_enabled=False)
        assert "web_search" not in derive_capabilities(d_off, {"FIRECRAWL_ENABLED": "true"})

        # Dept opt-in, firecrawl off
        d_on = _Dept(bot=_Bot(), web_search_enabled=True)
        assert "web_search" not in derive_capabilities(d_on, {})
        assert "web_search" not in derive_capabilities(
            d_on, {"FIRECRAWL_ENABLED": "false"}
        )

        # Both on
        assert "web_search" in derive_capabilities(
            d_on, {"FIRECRAWL_ENABLED": "true"}
        )

    def test_returns_frozenset(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry(present=True)))
        result = derive_capabilities(d, _empty_env())
        assert isinstance(result, frozenset)

    def test_is_pure_deterministic(self) -> None:
        """

        Repeated calls with identical input return equal results.
        """
        d = _Dept(
            bot=_Bot(
                jira=_BotEntry(),
                bitbucket=_BotEntry(),
            ),
            web_search_enabled=True,
        )
        env = {"SSH_HOST_1": "h", "FIRECRAWL_ENABLED": "true"}
        r1 = derive_capabilities(d, env)
        r2 = derive_capabilities(d, env)
        assert r1 == r2


# ---------------------------------------------------------------------------
# gate - set-algebra
# ---------------------------------------------------------------------------


class TestGate:
    """``gate(workflow_type, dept, env)`` decision behaviour."""

    def _full_dept(self) -> _Dept:
        return _Dept(
            bot=_Bot(
                jira=_BotEntry(),
                bitbucket=_BotEntry(),
                confluence=_BotEntry(),
            ),
            web_search_enabled=True,
        )

    def _full_env(self) -> dict[str, str]:
        return {"SSH_HOST_1": "runner.example.com", "FIRECRAWL_ENABLED": "true"}

    def test_returns_gate_decision(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry()))
        decision = gate("noop_test", d, _empty_env())
        assert isinstance(decision, GateDecision)

    def test_allowed_when_all_required_caps_present(self) -> None:
        decision = gate("code_change_with_test", self._full_dept(), self._full_env())
        assert decision.allowed is True
        assert decision.missing == frozenset()

    def test_denied_when_capability_missing(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry()))  # no bitbucket
        decision = gate("code_change_commit_only", d, _empty_env())
        assert decision.allowed is False
        assert decision.missing == frozenset({"bitbucket_read", "bitbucket_write"})

    def test_missing_is_set_difference(self) -> None:
        # No credentials at all, but workflow_type only needs jira_read
        d = _Dept(bot=_Bot())
        decision = gate("noop_test", d, _empty_env())
        assert decision.allowed is False
        assert decision.missing == frozenset({"jira_read"})

    def test_unknown_workflow_type_raises_key_error(self) -> None:
        d = _Dept(bot=_Bot())
        with pytest.raises(KeyError):
            gate("definitely_not_a_workflow", d, _empty_env())

    def test_decision_is_frozen(self) -> None:
        """

        ``GateDecision`` is a frozen dataclass; attribute assignment fails.
        """
        d = _Dept(bot=_Bot(jira=_BotEntry()))
        decision = gate("noop_test", d, _empty_env())
        with pytest.raises((AttributeError, TypeError)):
            decision.allowed = False  # type: ignore[misc]

    def test_remote_ssh_test_only_requires_jira_read_and_execution(self) -> None:
        # Jira present but no SSH_HOST in env
        d = _Dept(bot=_Bot(jira=_BotEntry()))
        decision = gate("remote_ssh_test_only", d, _empty_env())
        assert decision.allowed is False
        assert decision.missing == frozenset({"execution"})

        # Jira + SSH host
        decision_ok = gate(
            "remote_ssh_test_only", d, {"SSH_HOST_1": "runner.example.com"}
        )
        assert decision_ok.allowed is True
        assert decision_ok.missing == frozenset()

    def test_research_with_web_requires_web_search(self) -> None:
        d = _Dept(bot=_Bot(jira=_BotEntry()), web_search_enabled=False)
        decision = gate("research_with_web", d, {"FIRECRAWL_ENABLED": "true"})
        assert decision.allowed is False
        assert "web_search" in decision.missing

    def test_pure_no_io(self) -> None:
        """

        ``gate`` must not depend on global state or perform I/O.
        Repeated calls return equal decisions.
        """
        d = self._full_dept()
        env = self._full_env()
        d1 = gate("pr_review", d, env)
        d2 = gate("pr_review", d, env)
        assert d1 == d2
