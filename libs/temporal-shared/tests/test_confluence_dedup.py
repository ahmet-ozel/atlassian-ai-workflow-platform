"""Unit tests for ``temporal_shared.confluence_dedup``.

Validates the pure skip-decision predicates
:func:`should_skip_section_update`, :func:`should_skip_overwrite`,
and :func:`is_probe_page` against ``platform-mimari-workflows``
requirements.md §R8.2, §R8.3, §R8.7.

The dedicated property-test suite covering Property 9 (Confluence
write invariants) lives in
``platform/tests/property/test_confluence_invariants.py`` (task 8.5)
— this file covers concrete examples and the validation error paths
so a ``pytest libs/temporal-shared`` run remains hermetic.

Validates: Requirements 8.2, 8.3, 8.7.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from temporal_shared.confluence_dedup import (
    AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
    AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
    DEFAULT_OVERWRITE_FRESHNESS,
    PROBE_PAGE_TITLE_PREFIX,
    SkipDecision,
    is_probe_page,
    should_skip_overwrite,
    should_skip_section_update,
)


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Audit-action constants and defaults are pinned by the requirements."""

    def test_section_dedup_audit_action_is_pinned(self) -> None:
        """**Validates: Requirement 8.2**

        The audit action string is part of the requirement text and
        must not drift; downstream tests query ``audit_events`` by
        this exact value.
        """
        assert AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP == "confluence_section_dedup_skip"

    def test_overwrite_protected_audit_action_is_pinned(self) -> None:
        """**Validates: Requirement 8.7**"""
        assert AUDIT_CONFLUENCE_OVERWRITE_PROTECTED == "confluence_overwrite_protected"

    def test_default_overwrite_freshness_is_five_minutes(self) -> None:
        """**Validates: Requirement 8.7**

        R8.7 explicitly pins the freshness window at 5 minutes.
        """
        assert DEFAULT_OVERWRITE_FRESHNESS == timedelta(minutes=5)

    def test_probe_page_prefix_matches_foundation(self) -> None:
        """**Validates: Requirement 8.3**

        The probe sentinel prefix mirrors
        ``automation_service.probe.PROBE_ARTIFACT_PREFIX`` (foundation
        R5).  The two constants must agree so the workflow's filter
        and the probe runner agree on which titles are probes.
        """
        # Import locally so test collection does not pull in the
        # automation-service package when only the libs slice is on
        # the path.  The assertion still fires whenever the full
        # platform tree is available.
        try:
            from automation_service.probe import PROBE_ARTIFACT_PREFIX
        except ModuleNotFoundError:
            pytest.skip("automation-service not on sys.path in this slice")
        assert PROBE_PAGE_TITLE_PREFIX == PROBE_ARTIFACT_PREFIX


class TestSkipDecisionShape:
    """The dataclass invariants are part of the public contract."""

    def test_proceed_decision_is_well_formed(self) -> None:
        """**Validates: Requirement 8.2, 8.7**"""
        decision = SkipDecision(skip=False, audit_event=None)
        assert decision.skip is False
        assert decision.audit_event is None

    def test_skip_decision_requires_audit_event(self) -> None:
        """**Validates: Requirement 8.2, 8.7**

        Constructing ``skip=True`` without an audit_event would let
        callers silently drop the audit emission.  The dataclass
        must reject this at construction time.
        """
        with pytest.raises(ValueError, match="non-empty audit_event"):
            SkipDecision(skip=True, audit_event=None)

    def test_proceed_decision_must_not_carry_audit_event(self) -> None:
        """**Validates: Requirement 8.2, 8.7**

        Conversely, the proceed branch must not carry an audit
        action — successful writes are audited by the activity, not
        by this skip path.
        """
        with pytest.raises(ValueError, match="must not carry an audit_event"):
            SkipDecision(skip=False, audit_event="something")

    def test_skip_decision_is_frozen(self) -> None:
        """**Validates: Requirement 8.2, 8.7**

        Frozen dataclass — assignment after construction must raise.
        """
        decision = SkipDecision(skip=False, audit_event=None)
        with pytest.raises(Exception):  # FrozenInstanceError subclasses Exception
            decision.skip = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# should_skip_section_update — happy paths (Requirement 8.2)
# ---------------------------------------------------------------------------


class TestShouldSkipSectionUpdate:
    """Concrete examples for R8.2 / Property 9.b."""

    _KEY = ("automation-jira-PAY-1", "p1", "§1/Implementation", "abc123")

    def test_empty_table_returns_proceed(self) -> None:
        """**Validates: Requirement 8.2**"""
        assert should_skip_section_update(*self._KEY, set()) == SkipDecision(
            skip=False, audit_event=None
        )

    def test_unrelated_entries_do_not_match(self) -> None:
        """**Validates: Requirement 8.2**

        The four-tuple is the natural key — a different page id /
        section path / hash must not falsely dedup.
        """
        table = {
            ("automation-jira-PAY-1", "p1", "§1/Implementation", "DIFFERENT"),
            ("automation-jira-PAY-1", "p1", "§1/Other", "abc123"),
            ("automation-jira-PAY-1", "OTHER", "§1/Implementation", "abc123"),
            ("automation-jira-PAY-OTHER", "p1", "§1/Implementation", "abc123"),
        }
        assert should_skip_section_update(*self._KEY, table).skip is False

    def test_exact_match_returns_skip_with_audit(self) -> None:
        """**Validates: Requirement 8.2**

        Audit action must match the requirement-pinned literal.
        """
        decision = should_skip_section_update(*self._KEY, {self._KEY})
        assert decision.skip is True
        assert decision.audit_event == AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP

    def test_accepts_frozenset(self) -> None:
        """**Validates: Requirement 8.2**

        Workflow state may use frozenset for an immutable snapshot;
        the predicate must not require a mutable set.
        """
        decision = should_skip_section_update(*self._KEY, frozenset({self._KEY}))
        assert decision.skip is True

    def test_accepts_tuple_container(self) -> None:
        """**Validates: Requirement 8.2**

        ``Container`` is the only protocol we require.
        """
        decision = should_skip_section_update(*self._KEY, (self._KEY,))
        assert decision.skip is True

    @pytest.mark.parametrize(
        "field",
        ["workflow_id", "page_id", "section_path", "content_hash"],
    )
    def test_empty_key_component_is_rejected(self, field: str) -> None:
        """**Validates: Requirement 8.2**

        Empty key components would collide across unrelated pages.
        """
        kwargs: dict[str, object] = {
            "workflow_id": "automation-jira-PAY-1",
            "page_id": "p1",
            "section_path": "§1",
            "content_hash": "abc",
            "hash_table": set(),
        }
        kwargs[field] = ""
        with pytest.raises(ValueError, match=f"{field} must not be empty"):
            should_skip_section_update(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field",
        ["workflow_id", "page_id", "section_path", "content_hash"],
    )
    def test_non_string_key_component_is_rejected(self, field: str) -> None:
        """**Validates: Requirement 8.2**"""
        kwargs: dict[str, object] = {
            "workflow_id": "automation-jira-PAY-1",
            "page_id": "p1",
            "section_path": "§1",
            "content_hash": "abc",
            "hash_table": set(),
        }
        kwargs[field] = 42
        with pytest.raises(TypeError, match=f"{field} must be a string"):
            should_skip_section_update(**kwargs)  # type: ignore[arg-type]

    def test_non_container_table_is_rejected(self) -> None:
        """**Validates: Requirement 8.2**"""
        with pytest.raises(TypeError, match="hash_table must support"):
            should_skip_section_update(
                "automation-jira-PAY-1",
                "p1",
                "§1",
                "abc",
                42,  # type: ignore[arg-type]
            )

    def test_function_is_pure_no_side_effects(self) -> None:
        """**Validates: Requirement 8.2**

        Calling twice with the same inputs produces the same
        :class:`SkipDecision`.  The hash table is read-only — the
        caller is responsible for inserting after a successful write.
        """
        table: set[tuple[str, str, str, str]] = set()
        first = should_skip_section_update(*self._KEY, table)
        second = should_skip_section_update(*self._KEY, table)
        assert first == second == SkipDecision(skip=False, audit_event=None)
        # The hash table is untouched — inserts are the caller's job.
        assert table == set()


# ---------------------------------------------------------------------------
# should_skip_overwrite — happy paths (Requirement 8.7)
# ---------------------------------------------------------------------------


class TestShouldSkipOverwrite:
    """Concrete examples for R8.7 / Property 9.d."""

    _NOW = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    _BOTS = frozenset({"bot-1", "bot-2"})

    def test_recent_human_edit_blocks(self) -> None:
        """**Validates: Requirement 8.7**

        The pinned 5-minute window blocks an edit 2 minutes old.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW - timedelta(minutes=2),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is True
        assert decision.audit_event == AUDIT_CONFLUENCE_OVERWRITE_PROTECTED

    def test_recent_bot_edit_does_not_block(self) -> None:
        """**Validates: Requirement 8.7**

        Bot-on-bot edits are part of the iteration loop and must
        never trigger overwrite protection.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="bot-1",
            last_edit_at=self._NOW - timedelta(seconds=30),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False
        assert decision.audit_event is None

    def test_stale_human_edit_does_not_block(self) -> None:
        """**Validates: Requirement 8.7**

        An edit older than the 5-minute window is no longer
        considered "ongoing collaboration" — the bot proceeds.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW - timedelta(minutes=10),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False

    def test_exact_freshness_boundary_is_strict(self) -> None:
        """**Validates: Requirement 8.7**

        ``< freshness`` is strict: an edit exactly at the boundary
        does not block.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW - DEFAULT_OVERWRITE_FRESHNESS,
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False

    def test_just_inside_freshness_blocks(self) -> None:
        """**Validates: Requirement 8.7**"""
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW
            - DEFAULT_OVERWRITE_FRESHNESS
            + timedelta(seconds=1),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is True

    def test_missing_editor_does_not_block(self) -> None:
        """**Validates: Requirement 8.7**

        A page with no recorded editor (e.g. freshly created) is not
        in a "recent human edit" state; the bot proceeds.
        """
        decision = should_skip_overwrite(
            last_editor_account_id=None,
            last_edit_at=self._NOW - timedelta(minutes=2),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False

    def test_missing_timestamp_does_not_block(self) -> None:
        """**Validates: Requirement 8.7**"""
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=None,
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False

    def test_empty_bot_set_treats_every_edit_as_human(self) -> None:
        """**Validates: Requirement 8.7**

        With no configured bot accounts, the recent-edit check still
        works against the freshness window alone.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="bot-1",
            last_edit_at=self._NOW - timedelta(minutes=1),
            now=self._NOW,
            bot_ids=frozenset(),
        )
        assert decision.skip is True

    def test_iterable_bot_ids_accepted(self) -> None:
        """**Validates: Requirement 8.7**

        Generators are accepted (the helper freezes them internally
        before the membership check) — the workflow may pass a lazy
        view over ``departments.json``.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="bot-1",
            last_edit_at=self._NOW - timedelta(minutes=1),
            now=self._NOW,
            bot_ids=(b for b in ["bot-1", "bot-2"]),
        )
        assert decision.skip is False

    def test_future_dated_edit_does_not_block(self) -> None:
        """**Validates: Requirement 8.7**

        A clock-skewed editor whose timestamp is in the future
        produces a negative delta; the predicate must treat that as
        "not recent enough to block" rather than always blocking.
        """
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW + timedelta(minutes=1),
            now=self._NOW,
            bot_ids=self._BOTS,
        )
        assert decision.skip is False

    def test_custom_freshness_window(self) -> None:
        """**Validates: Requirement 8.7**

        The function accepts a non-default window for callers that
        want to apply a different policy (e.g. integration tests).
        """
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=self._NOW - timedelta(minutes=20),
            now=self._NOW,
            bot_ids=self._BOTS,
            freshness=timedelta(hours=1),
        )
        assert decision.skip is True

    def test_naive_now_is_rejected(self) -> None:
        """**Validates: Requirement 8.7**

        A naive ``now`` would silently mix timezones; we reject it
        so the comparison semantics stay legible.
        """
        with pytest.raises(TypeError, match="now must be timezone-aware"):
            should_skip_overwrite(
                last_editor_account_id="human-1",
                last_edit_at=self._NOW - timedelta(minutes=2),
                now=datetime(2026, 5, 14, 12, 0),
                bot_ids=self._BOTS,
            )

    def test_naive_last_edit_is_rejected(self) -> None:
        """**Validates: Requirement 8.7**"""
        with pytest.raises(
            TypeError, match="last_edit_at must be timezone-aware"
        ):
            should_skip_overwrite(
                last_editor_account_id="human-1",
                last_edit_at=datetime(2026, 5, 14, 11, 58),
                now=self._NOW,
                bot_ids=self._BOTS,
            )

    def test_zero_freshness_is_rejected(self) -> None:
        """**Validates: Requirement 8.7**"""
        with pytest.raises(ValueError, match="strictly positive"):
            should_skip_overwrite(
                last_editor_account_id="human-1",
                last_edit_at=self._NOW - timedelta(minutes=1),
                now=self._NOW,
                bot_ids=self._BOTS,
                freshness=timedelta(0),
            )

    def test_negative_freshness_is_rejected(self) -> None:
        """**Validates: Requirement 8.7**"""
        with pytest.raises(ValueError, match="strictly positive"):
            should_skip_overwrite(
                last_editor_account_id="human-1",
                last_edit_at=self._NOW - timedelta(minutes=1),
                now=self._NOW,
                bot_ids=self._BOTS,
                freshness=timedelta(seconds=-5),
            )

    def test_non_timedelta_freshness_is_rejected(self) -> None:
        """**Validates: Requirement 8.7**"""
        with pytest.raises(TypeError, match="freshness must be"):
            should_skip_overwrite(
                last_editor_account_id="human-1",
                last_edit_at=self._NOW - timedelta(minutes=1),
                now=self._NOW,
                bot_ids=self._BOTS,
                freshness=300,  # type: ignore[arg-type]
            )

    def test_cross_timezone_is_normalised(self) -> None:
        """**Validates: Requirement 8.7**

        ``now`` and ``last_edit_at`` may be expressed in different
        timezones; the function normalises both to UTC before
        comparing.
        """
        eastern = timezone(timedelta(hours=-5))
        now_utc = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
        last_eastern = datetime(2026, 5, 14, 6, 58, tzinfo=eastern)
        # 6:58 EST == 11:58 UTC, so 2 minutes before now_utc.
        decision = should_skip_overwrite(
            last_editor_account_id="human-1",
            last_edit_at=last_eastern,
            now=now_utc,
            bot_ids=self._BOTS,
        )
        assert decision.skip is True


# ---------------------------------------------------------------------------
# is_probe_page  (Requirement 8.3)
# ---------------------------------------------------------------------------


class TestIsProbePage:
    """Concrete examples for R8.3 / Property 9.e."""

    def test_canonical_probe_title(self) -> None:
        """**Validates: Requirement 8.3**"""
        assert is_probe_page("_AI_PROBE_1700000000_DELETE_ME") is True

    def test_legacy_probe_title(self) -> None:
        """**Validates: Requirement 8.3**

        The check is prefix-only so historical / human-edited
        variants still match (mirrors the foundation
        ``is_probe_artifact_title`` behaviour).
        """
        assert is_probe_page("_AI_PROBE_legacy_artifact") is True

    def test_unrelated_title_does_not_match(self) -> None:
        """**Validates: Requirement 8.3**"""
        assert is_probe_page("Quarterly Review - 2026-05-14") is False

    def test_close_but_not_prefix(self) -> None:
        """**Validates: Requirement 8.3**

        Titles that contain the prefix mid-string must not match —
        only the start matters.
        """
        assert is_probe_page("Notes on _AI_PROBE_1234_DELETE_ME") is False

    def test_empty_string_is_not_a_probe(self) -> None:
        """**Validates: Requirement 8.3**"""
        assert is_probe_page("") is False

    def test_none_is_not_a_probe(self) -> None:
        """**Validates: Requirement 8.3**

        ``None`` and other non-string inputs return ``False`` rather
        than raising — the helper is used inline in filter chains.
        """
        assert is_probe_page(None) is False  # type: ignore[arg-type]

    def test_int_is_not_a_probe(self) -> None:
        """**Validates: Requirement 8.3**"""
        assert is_probe_page(42) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Purity / replay-safety smoke tests
# ---------------------------------------------------------------------------


class TestPurity:
    """The helpers must not import wall-clock or random sources."""

    def test_module_does_not_import_clocks_or_randomness(self) -> None:
        """**Validates: Requirement 8.2, 8.3, 8.7, design.md replay determinism**

        A workflow that imported ``time`` / ``random`` / ``uuid`` in
        this module would fail the AST-based replay-determinism
        property test (task 2.7).  Asserting the source text here
        catches the violation early with a clear error message.
        """
        from temporal_shared import confluence_dedup

        source = inspect.getsource(confluence_dedup)
        forbidden = ("import time", "import random", "import uuid")
        for needle in forbidden:
            assert needle not in source, (
                f"confluence_dedup must not import {needle!r} — it would "
                "break replay determinism when invoked from a Temporal "
                "workflow."
            )
