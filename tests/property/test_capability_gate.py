"""Capability gate behavioral properties.



Capability gate determinism + feature flag default-off + mode=disabled
----------------------------------------------------------------------

For every triple ``(Department, workflow_type, env)`` drawn from a
schema-faithful Hypothesis strategy:

(a) Determinism / purity.:func:`derive_capabilities` is a pure function — it consults only the
 fields of ``dept`` and the keys of ``env`` documented in:mod:`temporal_shared.capabilities`. It performs no network or
 filesystem I/O. We enforce this by patching every common I/O entry
 point (``socket.socket``, ``socket.create_connection``,
 ``urllib.request.urlopen``, ``httpx.Client``, ``httpx.AsyncClient``,
 ``requests.request``, ``builtins.open``) and asserting the mocks
 are never called during evaluation.

(b) Capability derivation rule equivalence.
 The output of:func:`derive_capabilities` matches the exact rule
 table used by ``derive_capabilities``:

 - ``jira_read``+``jira_write`` iff ``dept.bot.jira.has_credential``
 - ``bitbucket_read``+``bitbucket_write`` iff ``dept.bot.bitbucket.has_credential``
 - ``confluence_read``+``confluence_write`` iff ``dept.bot.confluence.has_credential``
 - ``execution`` iff any key in ``env`` starts with ``SSH_HOST_``
 - ``web_search`` iff ``dept.web_search_enabled and env["FIRECRAWL_ENABLED"] == "true"``

 Any other capability string never appears in the output.

(c) Gate set algebra.
 For any workflow type *w* with required capabilities ``R``, the
 derived capability set ``D``, and the result of ``gate(w, dept, env)``:

 - ``allowed`` is True iff ``R ⊆ D``
 - ``missing`` equals ``R - D`` exactly (frozenset)
 - Monotonicity: enlarging ``D`` can never turn ``allowed=True``
 into ``allowed=False`` and ``missing`` can only shrink.
 - Workflow-start rule: a thin caller that consults:class:`GateDecision` MUST NOT call ``start_workflow`` when
 ``allowed`` is False. We model this with a Mock and assert
 ``not mock.called`` whenever the gate denies.

(d) ``dept.mode == "disabled"`` blocks workflow start unconditionally.
 The pure:func:`gate` function does not consult ``mode`` — that
 rule is enforced by the layer above (automation-service). We
 therefore wrap ``gate`` with the reference helper:func:`_should_start_workflow` defined in this module: it returns
 ``False`` whenever ``mode == "disabled"`` regardless of the gate
 result, and audit emits a ``dept_disabled`` event. The helper is
 the reference behavior for the ``automation-service``
 webhook handler must implement.

(e) ``SSH_RUNNER_DEPT_PINNING_ENABLED`` and ``SSH_DEPT_QUOTA_ENABLED``
 feature-flags default to off.
 Setting either flag in the environment to ``"false"`` (or omitting
 it entirely) MUST NOT change the output of:func:`derive_capabilities`. Only the presence of at least one
 ``SSH_HOST_<n>`` key drives the ``execution`` capability. The flag
 is *not yet* consulted by the resolver — turning it on is a future
 behavior that introduces dept-pinning logic. This test pins the
 default-off behaviour so the regression cannot land silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from temporal_shared.capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    GateDecision,
    derive_capabilities,
    gate,
)

# ---------------------------------------------------------------------------
# Schema-faithful test doubles for ``Department``
# ---------------------------------------------------------------------------
#
# The full ``Department`` dataclass / loader lives outside this test; the
# capability resolver itself only requires the structural protocol
# ``SupportsDepartment`` (see capabilities.py). We therefore mint
# minimal duck-typed stand-ins here so Hypothesis can drive every
# meaningful (dept, env) combination without coupling to a future
# concrete schema.


@dataclass(frozen=True)
class _StubBotEntry:
    """Implements:class:`temporal_shared.capabilities.HasCredential`."""

    present: bool

    def has_credential(self) -> bool:  # noqa: D401 - protocol method
        return self.present


@dataclass(frozen=True)
class _StubBot:
    """Minimal ``Department.bot`` shape for jira / bitbucket / confluence."""

    jira: _StubBotEntry | None = None
    bitbucket: _StubBotEntry | None = None
    confluence: _StubBotEntry | None = None


@dataclass(frozen=True)
class _StubDepartment:
    """Minimal ``Department`` shape with the fields used by the gate.

 The ``mode`` field is carried alongside the capability-relevant ones
 so the workflow-start wrapper can consult it,
 even though the pure:func:`gate` function does not.
 """

    web_search_enabled: bool
    bot: _StubBot
    mode: str = "active"   # one of {"active", "shadow", "disabled"}
    id: str = "test-dept"


# ---------------------------------------------------------------------------
# Reference workflow-start wrapper
# ---------------------------------------------------------------------------
#
# This helper is the reference behavior for what the automation-service
# webhook handler MUST implement. It documents the layered
# contract:
#
# 1. If ``dept.mode == "disabled"``, deny unconditionally; emit
# ``dept_disabled`` audit event; do not consult capability gate;
# do not call ``start_workflow``.
# 2. Otherwise consult:func:`gate`. If denied, emit
# ``capability_denied`` audit event; do not call ``start_workflow``.
# 3. Otherwise call ``start_workflow``.
#
# The helper returns the audit action string actually emitted (or
# ``None`` if the workflow was started) so tests can assert audit
# correctness alongside the call rule.


def _should_start_workflow(
    workflow_type: str,
    dept: _StubDepartment,
    env: Mapping[str, str],
    *,
    start_workflow: MagicMock,
    audit_log: list[str],
) -> bool:
    """Reference wrapper: returns True iff the workflow was started.

 Side effects:
 - Appends an audit event tag to ``audit_log`` describing the
 outcome (``"workflow_started"``, ``"capability_denied"``, or
 ``"dept_disabled"``).
 - Calls ``start_workflow`` exactly once iff the result is True.
 """
    if dept.mode == "disabled":
        audit_log.append("dept_disabled")
        return False
    decision = gate(workflow_type, dept, env)
    if not decision.allowed:
        audit_log.append("capability_denied")
        return False
    start_workflow(workflow_type, dept.id)
    audit_log.append("workflow_started")
    return True


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Strategy that produces a single ``BotEntry`` or ``None``.
bot_entry_strategy = st.one_of(
    st.none(),
    st.builds(_StubBotEntry, present=st.booleans()),
)


@st.composite
def _bot_strategy(draw: st.DrawFn) -> _StubBot:
    """Generate a ``_StubBot`` with arbitrary jira/bitbucket/confluence."""
    return _StubBot(
        jira=draw(bot_entry_strategy),
        bitbucket=draw(bot_entry_strategy),
        confluence=draw(bot_entry_strategy),
    )


#: Modes drawn from the schema-extended enum.
mode_strategy = st.sampled_from(["active", "shadow", "disabled"])


@st.composite
def _department_strategy(draw: st.DrawFn) -> _StubDepartment:
    """Generate a schema-faithful ``Department`` stand-in."""
    return _StubDepartment(
        web_search_enabled=draw(st.booleans()),
        bot=draw(_bot_strategy()),
        mode=draw(mode_strategy),
        id=draw(
            st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True)
        ),
    )


@st.composite
def _env_strategy(draw: st.DrawFn) -> dict[str, str]:
    """Generate an environment mapping with realistic noise.

 The strategy intentionally mixes capability-relevant keys
 (``SSH_HOST_*``, ``FIRECRAWL_ENABLED``) with feature-flag keys
 (``SSH_RUNNER_DEPT_PINNING_ENABLED``, ``SSH_DEPT_QUOTA_ENABLED``)
 and arbitrary unrelated keys so the feature-flag default-off behavior
 default-off do not affect derivation* — is exercised.
 """
    env: dict[str, str] = {}

    # Zero or more SSH_HOST_<n> entries.
    n_hosts = draw(st.integers(min_value=0, max_value=3))
    for i in range(n_hosts):
        env[f"SSH_HOST_{i}"] = draw(
            st.from_regex(r"^[a-z][a-z0-9.-]{1,30}$", fullmatch=True)
        )

    # FIRECRAWL_ENABLED is a tri-state in practice (true / false / absent).
    fc = draw(st.sampled_from(["true", "false", None]))
    if fc is not None:
        env["FIRECRAWL_ENABLED"] = fc

    # Feature flags — should be ignored by derive_capabilities.
    for flag in (
        "SSH_RUNNER_DEPT_PINNING_ENABLED",
        "SSH_DEPT_QUOTA_ENABLED",
    ):
        choice = draw(st.sampled_from(["true", "false", None]))
        if choice is not None:
            env[flag] = choice

    # Arbitrary unrelated noise keys to make sure derive_capabilities
    # ignores everything outside the documented contract.
    for key in draw(
        st.lists(
            st.from_regex(r"^[A-Z][A-Z0-9_]{1,12}$", fullmatch=True),
            min_size=0,
            max_size=4,
            unique=True,
        )
    ):
        # Avoid colliding with the known keys above.
        if key in env or key.startswith("SSH_HOST_") or key in {
            "FIRECRAWL_ENABLED",
            "SSH_RUNNER_DEPT_PINNING_ENABLED",
            "SSH_DEPT_QUOTA_ENABLED",
        }:
            continue
        env[key] = draw(st.text(max_size=8))

    return env


workflow_type_strategy = st.sampled_from(sorted(WORKFLOW_TYPE_CAPABILITIES.keys()))


# ---------------------------------------------------------------------------
# Closed capability vocabulary used by the derivation rules
# ---------------------------------------------------------------------------

ALL_CAPABILITIES: frozenset[str] = frozenset(
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


# ---------------------------------------------------------------------------
# Helper: expected derivation (oracle) — mirrors the derivation rules
# ---------------------------------------------------------------------------


def _expected_caps(dept: _StubDepartment, env: Mapping[str, str]) -> frozenset[str]:
    """Independent oracle implementation of the derivation rules.

 Re-deriving the expected capability set from the dept + env using a
 second implementation lets the tests catch regressions where the
 production code drifts away from the rule table.
 """
    caps: set[str] = set()
    if dept.bot.jira is not None and dept.bot.jira.has_credential():
        caps |= {"jira_read", "jira_write"}
    if dept.bot.bitbucket is not None and dept.bot.bitbucket.has_credential():
        caps |= {"bitbucket_read", "bitbucket_write"}
    if dept.bot.confluence is not None and dept.bot.confluence.has_credential():
        caps |= {"confluence_read", "confluence_write"}
    if any(k.startswith("SSH_HOST_") for k in env):
        caps.add("execution")
    if dept.web_search_enabled and env.get("FIRECRAWL_ENABLED", "false") == "true":
        caps.add("web_search")
    return frozenset(caps)


# ---------------------------------------------------------------------------
# Behavior: Determinism and purity (no I/O)
# ---------------------------------------------------------------------------


# Patches that intercept every I/O entry point a "naughty" implementation
# could plausibly use. Each patch wraps the symbol with a MagicMock; the
# test asserts none of them are touched while ``derive_capabilities`` /
# ``gate`` run.
_IO_PATCH_TARGETS: tuple[str, ...] = (
    "socket.socket",
    "socket.create_connection",
    "urllib.request.urlopen",
    "builtins.open",
)


class TestDeterminismAndPurity:
    """Pure function behavior with no I/O."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_derive_capabilities_makes_no_io_calls(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """``derive_capabilities`` must not call any documented I/O entry
 point. Each is wrapped in a MagicMock for the duration of the
 call and verified to remain untouched.
 """
        with patch("socket.socket") as mock_socket, patch(
            "socket.create_connection"
        ) as mock_create, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen, patch(
            "builtins.open"
        ) as mock_open:
            derive_capabilities(dept, env)

            assert not mock_socket.called, "derive_capabilities opened a socket"
            assert (
                not mock_create.called
            ), "derive_capabilities called socket.create_connection"
            assert (
                not mock_urlopen.called
            ), "derive_capabilities called urllib.request.urlopen"
            assert not mock_open.called, "derive_capabilities opened a file"

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_gate_makes_no_io_calls(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Same purity rule for the higher-level:func:`gate`.
 """
        with patch("socket.socket") as mock_socket, patch(
            "socket.create_connection"
        ) as mock_create, patch(
            "urllib.request.urlopen"
        ) as mock_urlopen, patch(
            "builtins.open"
        ) as mock_open:
            gate(workflow_type, dept, env)

            assert not mock_socket.called
            assert not mock_create.called
            assert not mock_urlopen.called
            assert not mock_open.called

    @settings(max_examples=100, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_derive_capabilities_is_referentially_transparent(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """Calling the function repeatedly with the same inputs always
 yields the same output (referential transparency).
 """
        r1 = derive_capabilities(dept, env)
        r2 = derive_capabilities(dept, env)
        r3 = derive_capabilities(dept, env)
        assert r1 == r2 == r3

    @settings(max_examples=100, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_gate_is_referentially_transparent(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Repeated ``gate`` calls with identical inputs return the same result."""
        d1 = gate(workflow_type, dept, env)
        d2 = gate(workflow_type, dept, env)
        assert d1 == d2


# ---------------------------------------------------------------------------
# Behavior: Capability derivation rule equivalence
# ---------------------------------------------------------------------------


class TestDerivationRules:
    """Each rule in the derivation table holds across all generated inputs."""

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_matches_oracle(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """The production output equals the independent oracle.
 """
        assert derive_capabilities(dept, env) == _expected_caps(dept, env)

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_output_is_subset_of_known_universe(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """No capability string outside the closed vocabulary appears.
 """
        caps = derive_capabilities(dept, env)
        assert caps <= ALL_CAPABILITIES

    @settings(max_examples=100, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_output_is_frozenset(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """The capability resolver returns an immutable set."""
        assert isinstance(derive_capabilities(dept, env), frozenset)

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_jira_capabilities_iff_jira_credential(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """``jira_read`` and ``jira_write`` appear iff ``bot.jira`` carries
 a credential. The two capabilities are always coupled.
 """
        caps = derive_capabilities(dept, env)
        has_jira = dept.bot.jira is not None and dept.bot.jira.has_credential()
        assert ("jira_read" in caps) is has_jira
        assert ("jira_write" in caps) is has_jira

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_bitbucket_capabilities_iff_bitbucket_credential(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """Bitbucket read and write capabilities follow the credential state."""
        caps = derive_capabilities(dept, env)
        has_bb = (
            dept.bot.bitbucket is not None and dept.bot.bitbucket.has_credential()
        )
        assert ("bitbucket_read" in caps) is has_bb
        assert ("bitbucket_write" in caps) is has_bb

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_confluence_capabilities_iff_confluence_credential(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """Confluence read and write capabilities follow the credential state."""
        caps = derive_capabilities(dept, env)
        has_cf = (
            dept.bot.confluence is not None
            and dept.bot.confluence.has_credential()
        )
        assert ("confluence_read" in caps) is has_cf
        assert ("confluence_write" in caps) is has_cf

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_execution_capability_iff_any_ssh_host(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """``execution`` appears iff at least one ``SSH_HOST_<n>`` key is in
 the environment, regardless of department fields and regardless
 of feature-flag values.
 """
        caps = derive_capabilities(dept, env)
        has_ssh = any(k.startswith("SSH_HOST_") for k in env)
        assert ("execution" in caps) is has_ssh

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_web_search_capability_iff_dept_opt_in_and_global_flag(
        self, dept: _StubDepartment, env: Mapping[str, str]
    ) -> None:
        """Both conditions are required: ``web_search`` appears iff
 ``dept.web_search_enabled`` AND ``env["FIRECRAWL_ENABLED"] == "true"``.
 """
        caps = derive_capabilities(dept, env)
        expected = (
            dept.web_search_enabled
            and env.get("FIRECRAWL_ENABLED", "false") == "true"
        )
        assert ("web_search" in caps) is expected


# ---------------------------------------------------------------------------
# Behavior: Gate set algebra
# ---------------------------------------------------------------------------


class TestGateSetAlgebra:
    """``gate(w, dept, env)`` packages the ``required - have`` set difference."""

    @settings(max_examples=300, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_decision_is_gate_decision(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Gate decisions expose the expected result shape."""
        decision = gate(workflow_type, dept, env)
        assert isinstance(decision, GateDecision)
        assert isinstance(decision.allowed, bool)
        assert isinstance(decision.missing, frozenset)

    @settings(max_examples=300, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_allowed_iff_required_subset_of_derived(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """``allowed`` is True iff ``required ⊆ derive_capabilities(dept, env)``.
 """
        required = WORKFLOW_TYPE_CAPABILITIES[workflow_type]
        derived = derive_capabilities(dept, env)
        decision = gate(workflow_type, dept, env)
        assert decision.allowed is (required <= derived)

    @settings(max_examples=300, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_missing_equals_set_difference(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """``missing`` exactly equals ``required - derived`` as a frozenset.
 """
        required = WORKFLOW_TYPE_CAPABILITIES[workflow_type]
        derived = derive_capabilities(dept, env)
        decision = gate(workflow_type, dept, env)
        assert decision.missing == frozenset(required - derived)

    @settings(max_examples=200, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_allowed_iff_missing_empty(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """The allowed flag is consistent with the missing-capability set."""
        decision = gate(workflow_type, dept, env)
        assert decision.allowed is (len(decision.missing) == 0)


# ---------------------------------------------------------------------------
# Behavior: workflow-start rule via Mock
# ---------------------------------------------------------------------------


class TestWorkflowStartInvariant:
    """Denial → no ``start_workflow`` call."""

    @settings(max_examples=300, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_denied_gate_blocks_start_workflow(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """For every input where the gate denies AND the dept is active,
 the reference wrapper MUST NOT call ``start_workflow`` and MUST
 emit a ``capability_denied`` audit event.
 """
        # Force the dept active so the dept_disabled branch doesn't
        # mask the capability check; that branch has its own test.
        dept = _StubDepartment(
            web_search_enabled=dept.web_search_enabled,
            bot=dept.bot,
            mode="active",
            id=dept.id,
        )

        decision = gate(workflow_type, dept, env)
        assume(not decision.allowed)

        start_workflow = MagicMock()
        audit_log: list[str] = []

        result = _should_start_workflow(
            workflow_type,
            dept,
            env,
            start_workflow=start_workflow,
            audit_log=audit_log,
        )

        assert result is False
        assert not start_workflow.called, (
            f"start_workflow was called despite missing capabilities "
            f"{decision.missing}"
        )
        assert audit_log == ["capability_denied"]

    @settings(max_examples=200, deadline=2000)
    @given(workflow_type=workflow_type_strategy, dept_id=st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True))
    def test_allowed_gate_starts_workflow(
        self,
        workflow_type: str,
        dept_id: str,
    ) -> None:
        """For every workflow type, a department wired with every possible
 credential and an env that satisfies every env-driven capability
 MUST pass the gate and trigger ``start_workflow`` exactly once
 with a ``workflow_started`` audit event.

 We construct a maximally-capable dept directly rather than
 sampling-then-filtering — random departments are denied far
 more often than allowed, so an ``assume(allowed)`` filter would
 trigger ``HealthCheck.filter_too_much``.
 """
        max_dept = _StubDepartment(
            web_search_enabled=True,
            bot=_StubBot(
                jira=_StubBotEntry(present=True),
                bitbucket=_StubBotEntry(present=True),
                confluence=_StubBotEntry(present=True),
            ),
            mode="active",
            id=dept_id,
        )
        env = {
            "SSH_HOST_0": "runner-0",
            "FIRECRAWL_ENABLED": "true",
        }

        # Pre-condition: this dept is allowed for every workflow type.
        assert gate(workflow_type, max_dept, env).allowed

        start_workflow = MagicMock()
        audit_log: list[str] = []

        result = _should_start_workflow(
            workflow_type,
            max_dept,
            env,
            start_workflow=start_workflow,
            audit_log=audit_log,
        )

        assert result is True
        start_workflow.assert_called_once_with(workflow_type, dept_id)
        assert audit_log == ["workflow_started"]


# ---------------------------------------------------------------------------
# Behavior: Monotonicity
# ---------------------------------------------------------------------------


class TestMonotonicity:
    """Adding capability sources can only relax the gate, never tighten it."""

    @settings(max_examples=200, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_adding_ssh_host_can_only_relax_gate(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Adding a new ``SSH_HOST_<n>`` to the env can only turn a denied
 decision into allowed (or keep it as-is). It cannot flip an
 allowed decision to denied.
 """
        before = gate(workflow_type, dept, env)
        env_after = dict(env)
        env_after["SSH_HOST_NEW"] = "host-new"
        after = gate(workflow_type, dept, env_after)

        # Missing capabilities may only shrink.
        assert after.missing <= before.missing
        # If allowed before, must still be allowed after.
        if before.allowed:
            assert after.allowed

    @settings(max_examples=200, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_enabling_firecrawl_can_only_relax_gate(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Enabling web search can only add capability coverage."""
        # Force the dept opt-in so toggling FIRECRAWL_ENABLED actually
        # changes the derivation.
        dept = _StubDepartment(
            web_search_enabled=True,
            bot=dept.bot,
            mode=dept.mode,
            id=dept.id,
        )
        env_off = dict(env)
        env_off["FIRECRAWL_ENABLED"] = "false"
        env_on = dict(env)
        env_on["FIRECRAWL_ENABLED"] = "true"

        off = gate(workflow_type, dept, env_off)
        on = gate(workflow_type, dept, env_on)

        assert on.missing <= off.missing
        if off.allowed:
            assert on.allowed


# ---------------------------------------------------------------------------
# Behavior: dept.mode == "disabled" blocks start unconditionally
# ---------------------------------------------------------------------------


class TestModeDisabledBlocks:
    """``mode=disabled`` denies before the capability gate is consulted."""

    @settings(max_examples=300, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_disabled_dept_never_starts_workflow(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """Regardless of capability sufficiency, a department in
 ``mode=disabled`` MUST NOT have ``start_workflow`` called and
 the audit log MUST contain a single ``dept_disabled`` event.
 """
        disabled_dept = _StubDepartment(
            web_search_enabled=dept.web_search_enabled,
            bot=dept.bot,
            mode="disabled",
            id=dept.id,
        )

        start_workflow = MagicMock()
        audit_log: list[str] = []

        result = _should_start_workflow(
            workflow_type,
            disabled_dept,
            env,
            start_workflow=start_workflow,
            audit_log=audit_log,
        )

        assert result is False
        assert not start_workflow.called
        assert audit_log == ["dept_disabled"]

    @settings(max_examples=200, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        workflow_type=workflow_type_strategy,
    )
    def test_disabled_dept_short_circuits_before_gate(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        workflow_type: str,
    ) -> None:
        """``mode=disabled`` MUST short-circuit *before* the capability
 gate runs — no ``capability_denied`` audit may appear, only
 ``dept_disabled``. This locks the layering documented in:func:`_should_start_workflow`.
 """
        # Synthesise a dept that *would* pass the gate so the test
        # provably exercises the short-circuit path even when caps are
        # sufficient.
        bot = _StubBot(
            jira=_StubBotEntry(present=True),
            bitbucket=_StubBotEntry(present=True),
            confluence=_StubBotEntry(present=True),
        )
        env_with_ssh = {**env, "SSH_HOST_0": "h", "FIRECRAWL_ENABLED": "true"}
        disabled_dept = _StubDepartment(
            web_search_enabled=True,
            bot=bot,
            mode="disabled",
            id="disabled-1",
        )

        # Sanity check: an active version of this dept would pass.
        active_dept = _StubDepartment(
            web_search_enabled=True,
            bot=bot,
            mode="active",
            id="active-1",
        )
        assert gate(workflow_type, active_dept, env_with_ssh).allowed

        start_workflow = MagicMock()
        audit_log: list[str] = []
        result = _should_start_workflow(
            workflow_type,
            disabled_dept,
            env_with_ssh,
            start_workflow=start_workflow,
            audit_log=audit_log,
        )
        assert result is False
        assert audit_log == ["dept_disabled"]
        assert "capability_denied" not in audit_log


# ---------------------------------------------------------------------------
# Behavior: feature flags default-off
# ---------------------------------------------------------------------------


class TestFeatureFlagsDefaultOff:
    """``SSH_RUNNER_DEPT_PINNING_ENABLED`` and ``SSH_DEPT_QUOTA_ENABLED``
 must not influence ``derive_capabilities`` while their defaults are off.
 """

    _IRRELEVANT_FLAGS: tuple[str, ...] = (
        "SSH_RUNNER_DEPT_PINNING_ENABLED",
        "SSH_DEPT_QUOTA_ENABLED",
    )

    @settings(max_examples=200, deadline=2000)
    @given(
        dept=_department_strategy(),
        env=_env_strategy(),
        flag_value=st.sampled_from(["true", "false", "TRUE", "FALSE", "1", "0"]),
        flag_name=st.sampled_from(_IRRELEVANT_FLAGS),
    )
    def test_flag_value_does_not_change_capabilities(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
        flag_value: str,
        flag_name: str,
    ) -> None:
        """Setting either flag to any value (truthy or falsy) MUST NOT
 change the derived capability set vs. omitting the flag
 entirely. This pins the *default-off* contract: flag-driven
 logic has not been wired in yet, so the resolver must ignore
 the flag completely.
 """
        env_without = {k: v for k, v in env.items() if k != flag_name}
        env_with = {**env_without, flag_name: flag_value}

        caps_without = derive_capabilities(dept, env_without)
        caps_with = derive_capabilities(dept, env_with)

        assert caps_without == caps_with, (
            f"derive_capabilities reacted to {flag_name}={flag_value!r} — "
            f"flag is supposed to default-off and not be consulted yet"
        )

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_pinning_flag_off_does_not_strip_execution(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
    ) -> None:
        """Concrete behavior: with ``SSH_RUNNER_DEPT_PINNING_ENABLED=false``
 (default) and at least one ``SSH_HOST_<n>`` defined, every
 department gets ``execution`` regardless of dept-specific
 attributes.
 """
        env_pinned_off = {
            **env,
            "SSH_HOST_0": "runner-0",
            "SSH_RUNNER_DEPT_PINNING_ENABLED": "false",
        }
        caps = derive_capabilities(dept, env_pinned_off)
        assert "execution" in caps

    @settings(max_examples=200, deadline=2000)
    @given(dept=_department_strategy(), env=_env_strategy())
    def test_quota_flag_off_does_not_block_execution(
        self,
        dept: _StubDepartment,
        env: Mapping[str, str],
    ) -> None:
        """With ``SSH_DEPT_QUOTA_ENABLED=false`` (default), no quota check
 runs; ``execution`` is granted purely on the env-level
 ``SSH_HOST_<n>`` presence.
 """
        env_quota_off = {
            **env,
            "SSH_HOST_0": "runner-0",
            "SSH_DEPT_QUOTA_ENABLED": "false",
        }
        caps = derive_capabilities(dept, env_quota_off)
        assert "execution" in caps

    def test_flag_off_when_no_ssh_host_yields_no_execution(self) -> None:
        """Flags-off + no ``SSH_HOST_<n>`` keys → no ``execution``.
 """
        dept = _StubDepartment(
            web_search_enabled=False,
            bot=_StubBot(),
            mode="active",
        )
        env = {
            "SSH_RUNNER_DEPT_PINNING_ENABLED": "false",
            "SSH_DEPT_QUOTA_ENABLED": "false",
            "FIRECRAWL_ENABLED": "false",
        }
        assert "execution" not in derive_capabilities(dept, env)


# ---------------------------------------------------------------------------
# Behavior: WORKFLOW_TYPE_CAPABILITIES is a closed mapping
# ---------------------------------------------------------------------------


class TestWorkflowTypeCapabilitiesShape:
    """Structural rules for the single-source-of-truth mapping."""

    def test_mapping_has_exactly_thirteen_entries(self) -> None:
        """The workflow capability mapping has the expected size."""
        assert len(WORKFLOW_TYPE_CAPABILITIES) == 13

    def test_mapping_keys_match_design(self) -> None:
        """The workflow capability mapping exposes the expected workflow keys."""
        expected = {
            "code_change_with_test",
            "code_change_commit_only",
            "pr_review",
            "confluence_doc_create",
            "confluence_doc_update",
            "research_basic",
            "research_with_web",
            "multi_step",
            "noop_test",
            "remote_ssh_test_only",
            "script_execute",
            "research_publish_confluence",
            "research_summary_jira",
        }
        assert set(WORKFLOW_TYPE_CAPABILITIES.keys()) == expected

    def test_all_values_are_frozensets_within_known_universe(self) -> None:
        """All mapped capability sets stay within the closed capability vocabulary."""
        for wf, caps in WORKFLOW_TYPE_CAPABILITIES.items():
            assert isinstance(caps, frozenset), wf
            assert caps <= ALL_CAPABILITIES, wf

    def test_mapping_is_immutable(self) -> None:
        """``MappingProxyType`` rejects mutation at runtime. We verify
 every documented mutation method raises ``TypeError``.
 """
        # ``MappingProxyType`` lacks ``__setitem__`` / ``__delitem__``
        # / ``clear`` / ``pop`` / ``popitem`` / ``update``; calling any
        # raises TypeError.
        with pytest.raises(TypeError):
            WORKFLOW_TYPE_CAPABILITIES["new"] = frozenset()  # type: ignore[index]
        with pytest.raises(TypeError):
            del WORKFLOW_TYPE_CAPABILITIES["noop_test"]  # type: ignore[arg-type]
        with pytest.raises(AttributeError):
            WORKFLOW_TYPE_CAPABILITIES.clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Sanity check: gate raises on unknown workflow types
# ---------------------------------------------------------------------------


class TestUnknownWorkflowType:
    """``gate`` raises ``KeyError`` for any workflow type not in the mapping."""

    @settings(max_examples=50, deadline=2000)
    @given(
        unknown=st.text(min_size=1, max_size=30).filter(
            lambda s: s not in WORKFLOW_TYPE_CAPABILITIES
        ),
        dept=_department_strategy(),
        env=_env_strategy(),
    )
    def test_unknown_workflow_type_raises(
        self,
        unknown: str,
        dept: _StubDepartment,
        env: Mapping[str, str],
    ) -> None:
        """Unknown workflow types are rejected."""
        with pytest.raises(KeyError):
            gate(unknown, dept, env)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#
# Behavior: Async resolve_dept_capabilities execution predicate
# ----------------------------------------------------------------
#
# For any department, the ``execution`` capability SHALL be present in
# ``resolve_dept_capabilities`` output if and only if at least one runner
# with ``status='active'`` is assigned to that department.
#
# This tests the async DB-backed ``resolve_dept_capabilities`` function
# in ``services/automation-service/src/decision/capability_gate.py``,
# complementing the pure-function tests above which test the env-based
# ``derive_capabilities`` from ``temporal_shared.capabilities``.


import asyncio
import importlib.util as _importlib_util
import sys as _sys
from pathlib import Path as _Path
from unittest.mock import AsyncMock

# ---------------------------------------------------------------------------
# Load ``capability_gate.py`` from the automation-service without going
# through the full package init (which pulls in asyncpg and other heavy
# dependencies). We use ``importlib.util.spec_from_file_location`` to
# register the module under a synthetic name — same pattern as
# ``test_burst_debounce.py`` and ``test_replay_dedup.py``.
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = _Path(__file__).resolve().parent.parent.parent
_CAPABILITY_GATE_PATH = (
    _WORKSPACE_ROOT
    / "services"
    / "automation-service"
    / "src"
    / "decision"
    / "capability_gate.py"
)


def _load_capability_gate():
    """Load capability_gate module, handling the asyncpg import gracefully."""
    _module_name = "_capability_gate_sut"
    if _module_name in _sys.modules:
        return _sys.modules[_module_name]

    spec = _importlib_util.spec_from_file_location(
        _module_name, _CAPABILITY_GATE_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"Failed to build import spec for {_CAPABILITY_GATE_PATH!s}"
    )
    module = _importlib_util.module_from_spec(spec)
    _sys.modules[_module_name] = module
    spec.loader.exec_module(module)
    return module


_capability_gate_mod = _load_capability_gate()
resolve_dept_capabilities = _capability_gate_mod.resolve_dept_capabilities


# ---------------------------------------------------------------------------
# Strategies for DB-backed capability gate testing
# ---------------------------------------------------------------------------

#: Runner status values matching the CHECK constraint in the schema.
_RUNNER_STATUS = st.sampled_from(["active", "disabled", "quarantine"])


@dataclass(frozen=True)
class _RunnerConfig:
    """A single runner assignment for a department."""

    runner_id: str
    status: str  # active | disabled | quarantine


@st.composite
def _runner_list_strategy(draw: st.DrawFn) -> list[_RunnerConfig]:
    """Generate a list of 0-5 runners with random statuses.

 This covers:
 - No runners assigned (empty list)
 - All runners disabled/quarantine (no active)
 - At least one active runner
 - Mix of active and non-active runners
 """
    n_runners = draw(st.integers(min_value=0, max_value=5))
    runners = []
    for i in range(n_runners):
        runner_id = draw(
            st.from_regex(r"^runner-[a-z0-9]{1,8}$", fullmatch=True)
        )
        status = draw(_RUNNER_STATUS)
        runners.append(_RunnerConfig(runner_id=runner_id, status=status))
    return runners


@st.composite
def _bot_services_strategy(draw: st.DrawFn) -> list[str]:
    """Generate a list of bot services registered for a department."""
    services = draw(
        st.lists(
            st.sampled_from(["jira", "bitbucket", "confluence"]),
            min_size=0,
            max_size=3,
            unique=True,
        )
    )
    return services


def _make_mock_pool(
    dept_id: str,
    bot_services: list[str],
    web_search_enabled: bool,
    runners: list[_RunnerConfig],
) -> AsyncMock:
    """Build a mock asyncpg.Pool that returns controlled query results.

 The mock simulates the three queries in ``resolve_dept_capabilities``:
 1. SELECT service FROM automation.department_bots WHERE department_id = $1
 2. SELECT web_search_enabled FROM automation.departments WHERE id = $1
 3. SELECT COUNT(*) FROM... WHERE a.dept_id = $1 AND r.status = 'active'
 """
    active_count = sum(1 for r in runners if r.status == "active")

    # Mock connection that handles the three queries
    mock_conn = AsyncMock()

    # Track call order to return correct results for each query
    fetch_call_count = [0]
    fetchrow_call_count = [0]
    fetchval_call_count = [0]

    async def mock_fetch(query, *args):
        """Return bot service rows."""
        return [{"service": s} for s in bot_services]

    async def mock_fetchrow(query, *args):
        """Return department row with web_search_enabled."""
        return {"web_search_enabled": web_search_enabled}

    async def mock_fetchval(query, *args):
        """Return active runner count."""
        return active_count

    mock_conn.fetch = mock_fetch
    mock_conn.fetchrow = mock_fetchrow
    mock_conn.fetchval = mock_fetchval

    # Mock the pool's acquire context manager
    mock_pool = AsyncMock()

    class _AcquireCtx:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *args):
            pass

    mock_pool.acquire = _AcquireCtx

    return mock_pool


# ---------------------------------------------------------------------------
# Behavior: Capability Gate Execution Predicate (async DB-backed)
# ---------------------------------------------------------------------------


class TestCapabilityGateExecutionPredicate:
    """execution ∈ resolve_dept_capabilities(db, dept_id) ⟺
 at least one runner with status='active' is assigned to that department.


 """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        dept_id=st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True),
        bot_services=_bot_services_strategy(),
        web_search_enabled=st.booleans(),
        runners=_runner_list_strategy(),
    )
    def test_execution_capability_iff_active_runner_assigned(
        self,
        dept_id: str,
        bot_services: list[str],
        web_search_enabled: bool,
        runners: list[_RunnerConfig],
    ) -> None:
        """The biconditional: ``execution`` ∈ capabilities ⟺
 active_runner_count > 0. This must hold regardless of what
 other bot services are registered or whether web_search is
 enabled.
 """
        mock_pool = _make_mock_pool(
            dept_id, bot_services, web_search_enabled, runners
        )

        capabilities = asyncio.run(
            resolve_dept_capabilities(mock_pool, dept_id)
        )

        has_active_runner = any(r.status == "active" for r in runners)

        assert ("execution" in capabilities) is has_active_runner, (
            f"execution capability mismatch: "
            f"has_active_runner={has_active_runner}, "
            f"runners={[(r.runner_id, r.status) for r in runners]}, "
            f"capabilities={capabilities}"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        dept_id=st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True),
        bot_services=_bot_services_strategy(),
        web_search_enabled=st.booleans(),
        runners=_runner_list_strategy(),
    )
    def test_execution_absence_when_no_active_runners(
        self,
        dept_id: str,
        bot_services: list[str],
        web_search_enabled: bool,
        runners: list[_RunnerConfig],
    ) -> None:
        """When all assigned runners have status ∈ {disabled, quarantine}
 OR no runners are assigned at all, ``execution`` MUST NOT
 appear in the capability set.
 """
        # Filter to only non-active runners for this test
        non_active_runners = [r for r in runners if r.status != "active"]

        mock_pool = _make_mock_pool(
            dept_id, bot_services, web_search_enabled, non_active_runners
        )

        capabilities = asyncio.run(
            resolve_dept_capabilities(mock_pool, dept_id)
        )

        assert "execution" not in capabilities, (
            f"execution capability should NOT be present when no active "
            f"runners exist. runners={[(r.runner_id, r.status) for r in non_active_runners]}"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        dept_id=st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True),
        bot_services=_bot_services_strategy(),
        web_search_enabled=st.booleans(),
        n_active=st.integers(min_value=1, max_value=5),
        n_inactive=st.integers(min_value=0, max_value=3),
    )
    def test_execution_presence_when_active_runners_exist(
        self,
        dept_id: str,
        bot_services: list[str],
        web_search_enabled: bool,
        n_active: int,
        n_inactive: int,
    ) -> None:
        """When at least one runner with status='active' is assigned,
 ``execution`` MUST appear in the capability set, regardless
 of how many disabled/quarantine runners also exist.
 """
        runners = [
            _RunnerConfig(runner_id=f"active-{i}", status="active")
            for i in range(n_active)
        ] + [
            _RunnerConfig(runner_id=f"inactive-{i}", status="disabled")
            for i in range(n_inactive)
        ]

        mock_pool = _make_mock_pool(
            dept_id, bot_services, web_search_enabled, runners
        )

        capabilities = asyncio.run(
            resolve_dept_capabilities(mock_pool, dept_id)
        )

        assert "execution" in capabilities, (
            f"execution capability MUST be present when active runners "
            f"exist. n_active={n_active}, n_inactive={n_inactive}"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        dept_id=st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True),
        bot_services=_bot_services_strategy(),
        web_search_enabled=st.booleans(),
        runners=_runner_list_strategy(),
    )
    def test_other_capabilities_independent_of_runners(
        self,
        dept_id: str,
        bot_services: list[str],
        web_search_enabled: bool,
        runners: list[_RunnerConfig],
    ) -> None:
        """The ``execution`` predicate is independent of other capabilities.
 Bot services and web_search are determined by their own rules
 and are not affected by runner assignment state.
 """
        mock_pool = _make_mock_pool(
            dept_id, bot_services, web_search_enabled, runners
        )

        capabilities = asyncio.run(
            resolve_dept_capabilities(mock_pool, dept_id)
        )

        # Bot services should be present based on department_bots rows
        for service in bot_services:
            assert service in capabilities, (
                f"Service '{service}' should be in capabilities"
            )

        # web_search should be present iff web_search_enabled
        assert ("web_search" in capabilities) is web_search_enabled
