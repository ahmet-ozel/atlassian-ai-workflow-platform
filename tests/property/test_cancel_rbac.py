"""Hypothesis property test for ``is_cancel_authorized``.

**Validates: Requirements 11.1**

Drives the cancel RBAC predicate
(``automation_service.api.cancel.is_cancel_authorized``) across the
full input space. Three properties:

1. **Specification equivalence** — for any ``(actor, reporter,
   past_assignees)``, the predicate returns ``True`` iff
   ``actor == reporter`` OR ``actor ∈ past_assignees``.
2. **Determinism (purity)** — repeated calls with identical inputs
   yield identical outputs and never mutate the inputs.
3. **Monotonicity** — adding members to ``past_assignees`` never
   *reduces* authorization. Equivalently: if ``A ⊆ B``, then
   ``is_cancel_authorized(actor, reporter, A) == True`` implies
   ``is_cancel_authorized(actor, reporter, B) == True``.

The predicate is pure (no I/O, no clock) so this property test
exercises it directly without monkey-patching anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, strategies as st

# ---------------------------------------------------------------------------
# Path setup — mirror sibling property tests that import from
# ``automation_service``.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT = Path(__file__).resolve().parents[2]
_AUTOMATION_ROOT = _PLATFORM_ROOT / "services" / "automation-service"

for _path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
):
    _path_str = str(_path)
    if _path.is_dir() and _path_str not in sys.path:
        sys.path.insert(0, _path_str)


from automation_service.api.cancel import is_cancel_authorized  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Account ids in production come from Atlassian / OIDC and are short,
# stable opaque strings. We use a small alphabet so Hypothesis quickly
# explores collisions (actor == reporter, actor ∈ past_assignees).
_user_ids = st.text(
    alphabet="abcdefghij",
    min_size=1,
    max_size=4,
)


_actor_strategy = _user_ids
_reporter_strategy = _user_ids
_past_assignees_strategy = st.frozensets(_user_ids, min_size=0, max_size=8)


# ---------------------------------------------------------------------------
# Property 1 — specification equivalence
# ---------------------------------------------------------------------------


@given(
    actor=_actor_strategy,
    reporter=_reporter_strategy,
    past=_past_assignees_strategy,
)
def test_predicate_matches_specification(
    actor: str,
    reporter: str,
    past: frozenset[str],
) -> None:
    """``is_cancel_authorized == (actor == reporter or actor in past)``.

    Verbatim specification from workflows-spec Requirement 11.1 and
    design Property 11(a).
    """

    expected = (actor == reporter) or (actor in past)
    assert is_cancel_authorized(actor, reporter, past) is expected


# ---------------------------------------------------------------------------
# Property 2 — determinism / purity
# ---------------------------------------------------------------------------


@given(
    actor=_actor_strategy,
    reporter=_reporter_strategy,
    past=_past_assignees_strategy,
)
def test_predicate_is_deterministic_and_pure(
    actor: str,
    reporter: str,
    past: frozenset[str],
) -> None:
    """Repeated invocations yield identical results and inputs are
    not mutated.

    The predicate has no I/O, no clock and no global state; this
    property guards against accidental regressions that would break
    Temporal replay determinism if the function were ever consumed
    inside a workflow body.
    """

    snapshot = frozenset(past)
    a = is_cancel_authorized(actor, reporter, past)
    b = is_cancel_authorized(actor, reporter, past)
    c = is_cancel_authorized(actor, reporter, past)
    assert a == b == c
    assert past == snapshot


# ---------------------------------------------------------------------------
# Property 3 — monotonicity in ``past_assignees``
# ---------------------------------------------------------------------------


@given(
    actor=_actor_strategy,
    reporter=_reporter_strategy,
    base=_past_assignees_strategy,
    extra=st.frozensets(_user_ids, min_size=0, max_size=4),
)
def test_predicate_is_monotonic_in_past_assignees(
    actor: str,
    reporter: str,
    base: frozenset[str],
    extra: frozenset[str],
) -> None:
    """Adding past assignees never reduces authorization.

    Formally: ``A ⊆ B`` implies
    ``is_cancel_authorized(actor, reporter, A) == True``
    => ``is_cancel_authorized(actor, reporter, B) == True``.

    We construct ``B = A ∪ extra`` by union so ``A ⊆ B`` is enforced
    by construction, then check that authorization at ``A`` carries
    over to ``B``.
    """

    bigger = base | extra
    assert base.issubset(bigger)

    auth_base = is_cancel_authorized(actor, reporter, base)
    auth_bigger = is_cancel_authorized(actor, reporter, bigger)

    if auth_base:
        assert auth_bigger, (
            f"monotonicity violated: actor={actor!r}, reporter={reporter!r}, "
            f"base={sorted(base)!r}, bigger={sorted(bigger)!r}"
        )
