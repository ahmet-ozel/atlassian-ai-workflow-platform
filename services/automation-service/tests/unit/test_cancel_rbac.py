"""Unit tests for :func:`automation_service.api.cancel.is_cancel_authorized`.

Validates: Requirements 11.1 (workflows spec, task 13.1).

The predicate is a tiny pure function; the unit test enumerates the
truth table called out in the workflows-spec design document
(Property 11(a)):

* ``actor == reporter`` -> ``True``
* ``actor ∈ past_assignees`` -> ``True``
* ``actor ∉ {reporter} ∪ past_assignees`` -> ``False``
* Empty ``past_assignees`` is handled correctly (``False`` unless the
  actor is the reporter).

The Hypothesis-driven property test that exercises the same predicate
across the full input space lives at
``platform/tests/property/test_cancel_rbac.py``; this file keeps
focused, fast example-based assertions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup so ``automation_service`` resolves without an ``hatch build``.
# Mirrors the bootstrap used in sibling unit tests
# (``test_app.py``, ``test_inbound_slack.py``).
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

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


class TestIsCancelAuthorizedTruthTable:
    """Truth table from workflows-spec Property 11(a).

    ``is_cancel_authorized(actor, reporter, past_assignees) == True``
    iff ``actor == reporter OR actor ∈ past_assignees``.
    """

    def test_actor_equals_reporter_returns_true(self) -> None:
        assert (
            is_cancel_authorized(
                actor_user_id="alice",
                reporter_id="alice",
                past_assignees=frozenset(),
            )
            is True
        )

    def test_actor_in_past_assignees_returns_true(self) -> None:
        assert (
            is_cancel_authorized(
                actor_user_id="bob",
                reporter_id="alice",
                past_assignees=frozenset({"bob", "carol"}),
            )
            is True
        )

    def test_actor_in_neither_returns_false(self) -> None:
        assert (
            is_cancel_authorized(
                actor_user_id="dave",
                reporter_id="alice",
                past_assignees=frozenset({"bob", "carol"}),
            )
            is False
        )

    def test_empty_past_assignees_only_reporter_authorized(self) -> None:
        # Reporter still authorized.
        assert (
            is_cancel_authorized(
                actor_user_id="alice",
                reporter_id="alice",
                past_assignees=frozenset(),
            )
            is True
        )
        # Anyone else denied.
        assert (
            is_cancel_authorized(
                actor_user_id="bob",
                reporter_id="alice",
                past_assignees=frozenset(),
            )
            is False
        )

    def test_empty_actor_user_id_returns_false(self) -> None:
        # Defensive: an empty actor should never authorize, even if
        # the reporter happens to also be the empty string.
        assert (
            is_cancel_authorized(
                actor_user_id="",
                reporter_id="alice",
                past_assignees=frozenset({"bob"}),
            )
            is False
        )
        assert (
            is_cancel_authorized(
                actor_user_id="",
                reporter_id="",
                past_assignees=frozenset(),
            )
            is False
        )

    def test_actor_is_both_reporter_and_past_assignee(self) -> None:
        """Overlap is fine: a single ``True`` still wins."""

        assert (
            is_cancel_authorized(
                actor_user_id="alice",
                reporter_id="alice",
                past_assignees=frozenset({"alice", "bob"}),
            )
            is True
        )

    @pytest.mark.parametrize(
        "actor, reporter, past, expected",
        [
            # Single past assignee
            ("u1", "u2", frozenset({"u1"}), True),
            # Many past assignees, actor in set
            ("u3", "u2", frozenset({"u1", "u3", "u4"}), True),
            # Many past assignees, actor not in set, not reporter
            ("u5", "u2", frozenset({"u1", "u3", "u4"}), False),
            # Reporter same as one of past_assignees (idempotent)
            ("u2", "u2", frozenset({"u2", "u3"}), True),
        ],
    )
    def test_parametrized_combinations(
        self,
        actor: str,
        reporter: str,
        past: frozenset[str],
        expected: bool,
    ) -> None:
        assert (
            is_cancel_authorized(
                actor_user_id=actor,
                reporter_id=reporter,
                past_assignees=past,
            )
            is expected
        )


class TestIsCancelAuthorizedPurity:
    """The predicate is a pure function: same inputs => same output."""

    def test_repeated_calls_return_same_result(self) -> None:
        actor = "alice"
        reporter = "alice"
        past = frozenset({"bob"})
        first = is_cancel_authorized(actor, reporter, past)
        second = is_cancel_authorized(actor, reporter, past)
        third = is_cancel_authorized(actor, reporter, past)
        assert first == second == third

    def test_does_not_mutate_past_assignees(self) -> None:
        past = frozenset({"bob", "carol"})
        before = frozenset(past)  # snapshot
        is_cancel_authorized("dave", "alice", past)
        assert past == before
