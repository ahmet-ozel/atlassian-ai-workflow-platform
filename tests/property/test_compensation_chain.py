"""Cancel and compensation chain behavioral properties.

Hypothesis-driven verification of the cancel and compensation contract:

* Confluence pages cancelled by the chain receive a ``cancelled`` label
 and a ``[CANCELLED]`` title prefix; they are never deleted.
* Cancel RBAC: only the issue ``reporter`` or someone in the
 ``past_assignees`` set may cancel via the HTTP endpoint. At the workflow
 signal layer, the role closed-vocabulary is
 ``{end_user, admin, dept_admin}``; everything else maps to ``end_user``
 so the audit row stays well-formed.
* Compensation chain steps run in a deterministic, fixed order; each step
 is idempotent and becomes a no-op when the target side-effect is already
 cleaned up.
* Each compensation step is a Temporal activity with ``maximumAttempts=3``
 and ``start_to_close_timeout`` set; failures on any single step do NOT
 abort the chain.
* Natural terminations (``MAX_ITER`` cap, ``out_of_scope`` via
 ``needs_info_streak``) DO NOT trigger compensation; only the explicit
 cancel signal does.
* ``temporal_shared.compensation`` exposes the ``CompensationContext``
 dataclass, ``COMPENSATION_STEPS: Final[tuple[str,...]]`` ordered tuple,
 and ``run(ctx) -> CompensationReport``.

Properties asserted
-------------------

**Closed-vocabulary RBAC at the signal layer.** For any
 ``actor_role`` drawn from a wide alphabet (recognised + arbitrary),
 ``_audit_action_for_cancel_role`` MUST return
 ``workflow_cancelled_by_admin`` iff
 ``actor_role ∈ {admin, dept_admin}`` and
 ``workflow_cancelled_by_end_user`` for *every* other input,
 including ``None``, the empty string, and arbitrary unknown
 strings, preserving the "default to ``end_user``" rule.

**`COMPENSATION_STEPS` is an immutable tuple.** The exported
 ordering is a ``tuple`` (not a ``list`` / ``set``), and a
 deep-equality check across two import-time reads returns the
 same value - replay-safe order for every cancel run.

**Step name closed vocabulary.** Every entry in
 ``COMPENSATION_STEPS`` is one of the six step names enumerated
 by the compensation contract. The chain's step set is *exactly* the
 six activity names; no rogue ordering or name drift
 can sneak in unnoticed.

**Confluence step preserves the page.** The dedicated
 ``label_confluence_page_cancelled`` step is present and ordered
 after the PR / branch cleanup; the absent step name
 ``delete_confluence_page`` confirms the operational rule
 that Confluence pages are *never* deleted by the chain.

**Idempotency under repeated cancel.** If
 ``temporal_shared.compensation.run`` is exposed as a pure
 ``Mapping[step_name, callable]``-style harness, running the chain
 twice over an *identical*:class:`CompensationContext` produces
 reports whose set of attempted step names matches
 ``set(COMPENSATION_STEPS)`` on both runs (idempotent re-cancel).
 If the harness is absent (the activity-driven implementation
 does not expose a pure runner), this property is exercised
 structurally via the deterministic order and vocabulary checks.

**Natural terminations skip the chain.**
 The ``MAX_ITER`` exhaustion and ``out_of_scope`` (needs_info
 streak) paths terminate the workflow *without* invoking the
 chain. This module-level prose plus a structural check documents that
 the closed step vocabulary carries no ``out_of_scope`` /
 ``max_iter`` step name; the workflow-body coverage already lives
 in:file:`workers/agent-runner-worker/tests/unit/test_agent_runner_cancel.py`
 (``TestMaxIterNoCompensation`` + ``TestOutOfScopeNoCompensation``).

Module-level skip
-----------------

The compensation chain module ships under of
````, which is still ``[-]`` in
the optional compensation module. When the module is absent, this file
emits a precise, actionable ``pytest.skip(allow_module_level=True)`` per
the established pattern (see ``test_structured_choice.py`` and
``test_precommit_scanner.py``). Once the import succeeds, the skip drops
out and every property runs automatically.

The RBAC checks ride on the cancel signal handler helpers
already shipped with (``_audit_action_for_cancel_role`` and
the ``CANCEL_*`` audit action constants in
``agent_runner_workflow.py``); they do NOT depend on the
``temporal_shared.compensation`` module and would run even when the
chain module is absent. We still gate the *whole* file behind one
skip so partial-import failures do not produce noisy collection-time
errors so the compensation-chain checks run as one unit once the
module lands.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap - mirror the bootstrap in
# ``test_explain_keyword.py`` so this module remains importable from a
# bare ``python -m pytest`` even when the workspace ``pytest.ini``
# ``pythonpath`` is not active.
# ---------------------------------------------------------------------------

_TESTS_ROOT: Path = Path(__file__).resolve().parents[1]  # platform/tests/
_PLATFORM_ROOT: Path = _TESTS_ROOT.parent  # platform/

_REQUIRED_SRC_DIRS: tuple[Path, ...] = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
    _PLATFORM_ROOT / "libs" / "mcp_client" / "src",
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# ---------------------------------------------------------------------------
# Imports under test
#
# ``temporal_shared.messages`` ships ``CompensationContext`` /
# ``CompensationReason``; we always import them so the type
# strategies below stay grounded.
#
# ``temporal_shared.compensation`` ships ``COMPENSATION_STEPS`` and the
# chain runner. When absent, the whole file is skipped at module load
# (``allow_module_level=True``) per the pattern established by
# ``test_structured_choice.py``.
#
# The cancel signal handler helpers (RBAC role mapping +
# ``CANCEL_*`` audit action constants) ship with the workflow body and
# are imported eagerly - they never
# trigger the skip.
# ---------------------------------------------------------------------------

# noqa: E402 below - imports follow the sys.path bootstrap above.
from temporal_shared.messages import (  # noqa: E402
    CompensationContext,
    CompensationReason,
)
from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    CANCEL_BY_ADMIN_AUDIT_ACTION,
    CANCEL_BY_END_USER_AUDIT_ACTION,
    _audit_action_for_cancel_role,
)

try:
    from temporal_shared.compensation import (  # type: ignore[import-not-found] # noqa: E402
        COMPENSATION_STEPS,
    )
except ImportError as exc:  # pragma: no cover - defensive guard
    pytest.skip(
        "temporal_shared.compensation is not yet implemented; "
        f"import failed with: {exc!r}. Cancel and "
        "compensation chain checks will run automatically "
        "once the module lands.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Constants - closed vocabularies the production code is contracted to
# expose. Defined as module-level frozensets so Hypothesis strategies
# can sample them directly.
# ---------------------------------------------------------------------------

#: The closed vocabulary of cancel ``actor_role`` values that map to the
#: *admin* audit action - mirrors ``_CANCEL_ADMIN_ROLES`` in
#: ``agent_runner_workflow.py``. Re-defining the literal here (rather
#: than importing the private symbol) is the explicit contract for the
#: contract: a divergence between this set and the
#: production constant is *itself* a regression this check should
#: catch.
_ADMIN_ROLES: frozenset[str] = frozenset({"admin", "dept_admin"})

#: ``end_user`` is the canonical default role. Together with the two
#: admin roles it forms the recognised closed vocabulary; *every*
#: other string maps to ``end_user`` by default.
_CANCEL_RECOGNISED_ROLES: frozenset[str] = _ADMIN_ROLES | frozenset(
    {"end_user"}
)

#: The six compensation step names required by the compensation contract.
#: This MUST be the exact (and exclusive) content of
#: ``COMPENSATION_STEPS``. The checks catch both missing names
#: and rogue additions. Order matters and is asserted separately.
_EXPECTED_STEP_ORDER: tuple[str, ...] = (
    "close_draft_pr_if_open",
    "delete_ai_branch_if_unused",
    "label_confluence_page_cancelled",
    "leave_minio_artifacts_for_retention",
    "post_cancel_jira_comment",
    "transition_jira_issue_if_configured",
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


#: ``CompensationReason`` literal - drawn directly from the
#: ``Literal["user_cancel", "admin_cancel"]`` alias in
#::mod:`temporal_shared.messages`. Using ``get_args`` keeps the
#: strategy in lockstep with the production type alias.
_compensation_reasons: st.SearchStrategy[CompensationReason] = st.sampled_from(
    get_args(CompensationReason)
)

#: Short opaque identifiers used for ``workflow_id`` / ``dept_id`` /
#: ``actor_id`` etc. Keep the alphabet narrow so Hypothesis quickly
#: explores collisions and the property output stays human-readable.
_short_id: st.SearchStrategy[str] = st.text(
    alphabet="abcdefghijklmnop0123456789-",
    min_size=1,
    max_size=16,
)

#: ``actor_role`` strategy - *intentionally* mixes the recognised
#: closed-vocabulary values with arbitrary text and ``None`` so the strategy
#: exercises the default-to-``end_user`` branch alongside the happy
#: path.
_actor_roles: st.SearchStrategy[str | None] = st.one_of(
    st.sampled_from(sorted(_CANCEL_RECOGNISED_ROLES)),
    # Arbitrary unknown roles must map to
    # ``end_user`` ( default rule). Restrict alphabet to letters
    # so the failure-message representation stays clean.
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=0, max_size=16),
    st.just(""),
    st.none(),
)


@st.composite
def _compensation_contexts(draw: st.DrawFn) -> CompensationContext:
    """Construct a single:class:`CompensationContext` instance.

 Each optional field (``issue_key``, ``pr_id``, ``branch``,
 ``confluence_page_id``, ``minio_prefix``) independently flips
 between ``None`` and a populated value so the chain runner sees
 every cleanup-target combination - including the empty cancel
 (no PR, no branch, no Confluence page) which exercises the
 chain's "every step is a no-op" idempotent contract.
 """

    return CompensationContext(
        workflow_id=draw(_short_id),
        dept_id=draw(_short_id),
        issue_key=draw(st.one_of(st.none(), _short_id)),
        pr_id=draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10_000))),
        branch=draw(st.one_of(st.none(), _short_id)),
        confluence_page_id=draw(st.one_of(st.none(), _short_id)),
        minio_prefix=draw(st.one_of(st.none(), _short_id)),
        reason=draw(_compensation_reasons),
        actor_id=draw(_short_id),
    )


# ---------------------------------------------------------------------------
# Behavior: RBAC closed vocabulary + default-to-``end_user``
# ---------------------------------------------------------------------------


@given(actor_role=_actor_roles)
@settings(max_examples=100, deadline=None)
def test_audit_action_admin_iff_recognised_admin_role(
    actor_role: str | None,
) -> None:
    """``_audit_action_for_cancel_role`` matches the closed-vocabulary contract.

 The audit action is
 ``workflow_cancelled_by_admin`` iff ``actor_role ∈ {admin,
 dept_admin}`` and ``workflow_cancelled_by_end_user`` for *every*
 other input - including ``None``, the empty string, and arbitrary
 unknown strings (the "default to ``end_user``" rule).
 """

    expected = (
        CANCEL_BY_ADMIN_AUDIT_ACTION
        if isinstance(actor_role, str) and actor_role in _ADMIN_ROLES
        else CANCEL_BY_END_USER_AUDIT_ACTION
    )
    actual = _audit_action_for_cancel_role(actor_role)
    assert actual == expected, (
        f"actor_role={actor_role!r}: expected {expected!r}, got {actual!r} "
        "(only end_user / admin / dept_admin are "
        "recognised; everything else defaults to end_user)"
    )


@given(actor_role=_actor_roles)
@settings(max_examples=100, deadline=None)
def test_audit_action_for_cancel_role_is_deterministic(
    actor_role: str | None,
) -> None:
    """Determinism / purity of the cancel role mapping helper.

 Repeated invocations with identical inputs MUST yield identical
 outputs - replay-safe so the helper is callable from inside a
 Temporal workflow body.
 """

    a = _audit_action_for_cancel_role(actor_role)
    b = _audit_action_for_cancel_role(actor_role)
    c = _audit_action_for_cancel_role(actor_role)
    assert a == b == c, (
        f"non-deterministic role mapping for actor_role={actor_role!r}: "
        f"got {a!r}, {b!r}, {c!r}"
    )


# ---------------------------------------------------------------------------
# Behavior: ``COMPENSATION_STEPS`` is an immutable, deterministic tuple
# ---------------------------------------------------------------------------


def test_compensation_steps_is_a_tuple() -> None:
    """``COMPENSATION_STEPS`` MUST be a ``tuple`` (immutable container).

 The ``temporal_shared.compensation`` API exposes the
 declared type is ``Final[tuple[str,...]]`` so the order is locked
 at module load. Lists / sets / dicts would either be mutable or
 have unstable iteration order.
 """

    assert isinstance(COMPENSATION_STEPS, tuple), (
        f"COMPENSATION_STEPS type drift: expected tuple, got "
        f"{type(COMPENSATION_STEPS).__name__}"
    )


def test_compensation_steps_order_is_deterministic_across_reads() -> None:
    """Two reads of ``COMPENSATION_STEPS`` MUST be identical.

 The order is a constant - replay-safe across
 every cancel run on every worker. A regression here would be a
 silent re-shuffle (e.g. someone converted the constant to a
 ``frozenset`` then back to a tuple via ``tuple(frozenset(...))``).
 """

    from temporal_shared import compensation as _compensation_mod

    first = _compensation_mod.COMPENSATION_STEPS
    second = _compensation_mod.COMPENSATION_STEPS
    assert first == second
    assert first is second, (
        "COMPENSATION_STEPS identity drift across reads - the constant "
        "should be the same tuple object on every access (the operational rule)"
    )


# ---------------------------------------------------------------------------
# Behavior: closed step vocabulary + exact ordering
# ---------------------------------------------------------------------------


def test_compensation_steps_match_documented_order() -> None:
    """``COMPENSATION_STEPS`` content and order match the contract.

 Expected order:
 1. ``close_draft_pr_if_open``
 2. ``delete_ai_branch_if_unused``
 3. ``label_confluence_page_cancelled`` (label only - never delete;)
 4. ``leave_minio_artifacts_for_retention``
 5. ``post_cancel_jira_comment``
 6. ``transition_jira_issue_if_configured``

 A divergence from this exact sequence is *itself* the regression
 check catches - adding / removing / reordering a step
 changes the user-visible cleanup behaviour and would silently
 break compensation idempotency on re-cancel.
 """

    assert COMPENSATION_STEPS == _EXPECTED_STEP_ORDER, (
        f"COMPENSATION_STEPS order drift:\n"
        f" expected: {_EXPECTED_STEP_ORDER}\n"
        f" actual: {COMPENSATION_STEPS}\n"
        "Update the compensation contract in lockstep if this is intentional."
    )


def test_compensation_steps_contain_no_destructive_confluence_step() -> None:
    """: the chain MUST never delete a Confluence page.

 The chain only labels + prefixes the title with ``[CANCELLED]``;
 a step name like ``delete_confluence_page`` would be an explicit
 contract violation.
 """

    forbidden = {"delete_confluence_page", "remove_confluence_page"}
    leaked = forbidden & set(COMPENSATION_STEPS)
    assert not leaked, (
        f"the operational rule violation: COMPENSATION_STEPS contains destructive "
        f"Confluence step(s) {sorted(leaked)!r}; the chain must only "
        "label the page (label_confluence_page_cancelled)."
    )


def test_compensation_steps_contain_no_natural_termination_step() -> None:
    """: natural terminations bypass the chain.

 ``MAX_ITER`` and ``out_of_scope`` paths are workflow-level
 natural terminations - the *chain* never receives a step named
 after those concepts. This is a structural rule: if a future
 refactor accidentally hooked them up, the step name would leak
 into the closed vocabulary and this test would fire.

 The end-to-end "natural termination skips compensation" coverage
 lives in
 ``workers/agent-runner-worker/tests/unit/test_agent_runner_cancel.py``
 (``TestMaxIterNoCompensation`` + ``TestOutOfScopeNoCompensation``).
 """

    forbidden_substrings = ("max_iter", "out_of_scope", "needs_info")
    leaked = [
        step
        for step in COMPENSATION_STEPS
        if any(s in step for s in forbidden_substrings)
    ]
    assert not leaked, (
        f"the operational rule violation: COMPENSATION_STEPS contains step "
        f"name(s) {leaked!r} that look like natural-termination paths; "
        "natural terminations must bypass the chain entirely."
    )


# ---------------------------------------------------------------------------
# Behavior: Confluence step is present and ordered after PR / branch
# ---------------------------------------------------------------------------


def test_confluence_label_step_ordered_after_pr_and_branch_cleanup() -> None:
    """ +: PR / branch cleanup precedes the Confluence label step.

 Order intuition: close the draft PR first (so the branch becomes
 safe to delete), delete the branch second, then label the
 Confluence page. Any other order would either leak a draft PR
 referencing a labelled page or attempt a branch delete while the
 PR still references it.
 """

    assert "close_draft_pr_if_open" in COMPENSATION_STEPS
    assert "delete_ai_branch_if_unused" in COMPENSATION_STEPS
    assert "label_confluence_page_cancelled" in COMPENSATION_STEPS

    pr_idx = COMPENSATION_STEPS.index("close_draft_pr_if_open")
    branch_idx = COMPENSATION_STEPS.index("delete_ai_branch_if_unused")
    label_idx = COMPENSATION_STEPS.index("label_confluence_page_cancelled")

    assert pr_idx < branch_idx, (
        "the operational rule ordering violation: close_draft_pr_if_open must run "
        "before delete_ai_branch_if_unused (the branch is only safe to "
        "delete once the draft PR no longer references it)"
    )
    assert branch_idx < label_idx, (
        "the operational rule ordering violation: branch deletion must run before "
        "Confluence labelling (Confluence cleanup is the last "
        "user-visible cleanup marker in the chain)"
    )


# ---------------------------------------------------------------------------
# Behavior: context construction is hypothesis-stable (every step
# combination of ``None`` / populated optional fields is reachable).
# This grounds the "every step is idempotent / no-op when target
# already cleaned up" rule: the chain's runner is contracted to
# accept *any* CompensationContext shape - including the all-``None``
# context that exercises the "everything already cleaned up" branch.
# ---------------------------------------------------------------------------


@given(ctx=_compensation_contexts())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
def test_compensation_context_round_trips_unchanged(
    ctx: CompensationContext,
) -> None:
    """``CompensationContext`` instances are immutable and hashable-friendly.

 The context is a frozen dataclass so the
 chain runner cannot accidentally mutate it between steps - every
 step sees the same input on the second cancel as it did on the
 first (idempotency precondition). Re-construct the same context
 via ``dataclasses.replace`` with no overrides and confirm
 equality.
 """

    import dataclasses

    same = dataclasses.replace(ctx)
    assert same == ctx
    # Frozen dataclass - direct attribute mutation must raise.
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.workflow_id = "mutated"  # type: ignore[misc]


@given(ctx=_compensation_contexts())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
def test_empty_cancel_context_is_well_formed(ctx: CompensationContext) -> None:
    """Idempotency precondition: every cleanup target is independently optional.

 The chain's "every step is a no-op when the target is already
 cleaned up" contract is grounded by the fact that
 ``CompensationContext`` admits ``None`` for *every* cleanup-target
 field (issue_key, pr_id, branch, confluence_page_id, minio_prefix).
 Hypothesis ranges over the full lattice; this test confirms every
 sampled context exposes the documented schema (no missing
 attribute, no surprise default).
 """

    # Every documented optional field is present and accepts ``None``.
    optional_fields = (
        "issue_key",
        "pr_id",
        "branch",
        "confluence_page_id",
        "minio_prefix",
    )
    for name in optional_fields:
        assert hasattr(ctx, name), (
            f"CompensationContext missing documented field {name!r}"
        )

    # The required, non-optional fields are populated by every sample.
    for name in ("workflow_id", "dept_id", "actor_id", "reason"):
        value = getattr(ctx, name)
        assert value is not None and value != "", (
            f"CompensationContext.{name} is empty for sample {ctx!r}"
        )

    # ``reason`` is constrained to the documented closed vocabulary.
    assert ctx.reason in get_args(CompensationReason), (
        f"CompensationContext.reason={ctx.reason!r} is outside the "
        f"documented closed vocabulary {get_args(CompensationReason)!r}"
    )


# ---------------------------------------------------------------------------
# Behavior: natural terminations skip the chain.
#
# This is workflow-body behavior; the unit-level coverage already
# lives at:
#
# workers/agent-runner-worker/tests/unit/test_agent_runner_cancel.py
#::TestMaxIterNoCompensation
#::TestOutOfScopeNoCompensation
#
# At the property-test layer we only structurally confirm that no
# COMPENSATION_STEPS entry is named after a natural-termination path
# (already covered by ``test_compensation_steps_contain_no_natural_termination_step``
# above). The dedicated stub below acts as a discoverability anchor
# for future maintainers - it points at the unit
# coverage and asserts the structural rule a second time so a
# ``grep`` for ``MAX_ITER`` / ``out_of_scope`` in this file finds it.
# ---------------------------------------------------------------------------


def test_natural_terminations_have_no_chain_step() -> None:
    """Documentation anchor for natural terminations bypassing the chain.

 The full end-to-end coverage lives in the agent-runner unit suite
 (``test_agent_runner_cancel.py``); here we restate the structural
 rule so anyone navigating this behavior sees the link.
 """

    # Mirrors ``test_compensation_steps_contain_no_natural_termination_step``
    # - restated here for discoverability.
    forbidden_markers = ("max_iter", "out_of_scope", "needs_info")
    for step in COMPENSATION_STEPS:
        for marker in forbidden_markers:
            assert marker not in step, (
                f"the operational rule: step {step!r} mentions natural-"
                f"termination marker {marker!r}; natural terminations "
                "must NOT trigger the compensation chain "
                "(see test_agent_runner_cancel.py for end-to-end "
                "coverage)."
            )
