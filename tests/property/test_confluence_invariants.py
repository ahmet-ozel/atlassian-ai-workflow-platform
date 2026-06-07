"""invariant 9 - Confluence write invariants.



Hypothesis-driven verification of the five pure helpers used by the
``confluence_doc_create`` / ``confluence_doc_update`` flow of the
``AgentRunnerWorkflow`` in ````:

*:func:`temporal_shared.confluence_dedup.is_probe_page` -
 ``_AI_PROBE_*`` prefix detection (,
 §16.14.6 V6 / foundation).
*:func:`temporal_shared.confluence_dedup.should_skip_overwrite` -
 non-bot edited within freshness window → skip (,
 §16.11 - Confluence overwrite koruması).
*:func:`temporal_shared.confluence_dedup.should_skip_section_update` -
 ``(workflow_id, page_id, section_path, content_hash)`` already in
 the hash table → skip (, §16.14.10 V10).
*:func:`temporal_shared.confluence.compute_provenance_footer` -
 non-empty Jira link → footer that contains the link verbatim
 (, §16.13 S6 / §16.12 B7). Empty / invalid link inputs
 raise:class:`InvalidJiraIssueLinkError` (a:class:`ValueError`
 subclass) so the caller cannot accidentally render a malformed
 page; the invariant pins this fail-fast behaviour.
*:func:`temporal_shared.confluence.format_page_title` -
 ``{topic} - {YYYY-MM-DD}`` shape. Empty / whitespace-only
 topics raise:class:`InvalidTopicError` (a:class:`ValueError`
 subclass).

All five helpers are **pure deterministic** - calling each one twice
with the same inputs must return the same value (or raise the same
exception). The final ``TestDeterminism`` class pins this end-to-end.

Invariant statements (mirror design.md §"invariant")
----------------------------------------------------

(P-probe-1) ``is_probe_page("_AI_PROBE_X")`` is:data:`True` for any
 suffix ``X`` (including the empty string).
(P-probe-2) ``is_probe_page(title)`` is:data:`False` for any title
 whose first ``len("_AI_PROBE_")`` characters do not match
 the prefix.
(P-probe-3) ``is_probe_page`` is deterministic - two consecutive calls
 on the same input return the same boolean.

(P-overwrite-1) ``should_skip_overwrite`` returns ``skip=True`` **iff**
 ``last_editor_account_id is not None`` AND
 ``last_edit_at is not None`` AND
 ``last_editor_account_id ∉ bot_ids`` AND
 ``timedelta(0) <= now - last_edit_at < freshness``.
 A future-dated edit (negative delta) does **not** block.
(P-overwrite-2) When ``skip=True``, the audit event is exactly:data:`AUDIT_CONFLUENCE_OVERWRITE_PROTECTED`; when
 ``skip=False``, ``audit_event is None``.
(P-overwrite-3) ``should_skip_overwrite`` is deterministic.

(P-section-1) ``should_skip_section_update`` returns ``skip=True``
 **iff** the four-tuple ``(workflow_id, page_id,
 section_path, content_hash)`` is in ``hash_table``.
(P-section-2) When ``skip=True``, the audit event is exactly:data:`AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP`; when
 ``skip=False``, ``audit_event is None``.
(P-section-3) ``should_skip_section_update`` is deterministic.

(P-footer-1) For any non-empty, structurally-valid Jira issue link the
 returned footer **contains the link verbatim**
 (substring check - the activity layer relies on this so
 readers can click through to the source issue).
(P-footer-2) The footer is non-empty and contains the canonical
 provenance prefix ``"🤖"`` so tests / readers can locate
 the AI-attribution block.
(P-footer-3) Empty / whitespace-only / structurally-invalid link
 inputs raise:class:`InvalidJiraIssueLinkError`
 (a:class:`ValueError` subclass).

(P-title-1) For any non-empty topic and a calendar date,
 ``format_page_title`` returns a string matching
 ``r"^.+ - \\d{4}-\\d{2}-\\d{2}$"`` (the canonical 
 shape).
(P-title-2) The returned title contains the topic verbatim and the
 ISO-8601 date suffix exactly as ``current_date.strftime(
 "%Y-%m-%d")``.
(P-title-3) Empty / whitespace-only topics raise:class:`InvalidTopicError` (a:class:`ValueError`
 subclass).

Hypothesis configuration
------------------------

Every property runs at ``max_examples=100`` with ``deadline=None`` per
the task brief, matching the existing invariant cadence (see
``test_explain_keyword.py``, ``test_code_change_formatters.py``).

Deviation note (task brief vs implementation)
---------------------------------------------

The task brief for invariant states "empty → empty string" for:func:`compute_provenance_footer`. The production implementation in:mod:`temporal_shared.confluence` raises:class:`InvalidJiraIssueLinkError` instead, on the basis that the
footer is rendered verbatim into Confluence storage format and a
silently-empty footer would strip the AI-attribution required by 
( §16.12 B7 - bot output attribution). The invariant tests
the **as-implemented** contract (raise on empty) since that module's
docstring and unit tests in
``platform/libs/temporal-shared/tests/test_confluence.py`` are the
source of truth for the helper. The mismatch is recorded in this
test's docstring so a future reviewer can adjudicate without re-reading
the source.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap - mirrors ``test_explain_keyword.py``.
#
# ``temporal_shared`` ships under ``libs/temporal-shared/src/`` and is
# already on the workspace ``pytest.ini`` ``pythonpath``; we add it
# explicitly here as well so this file remains importable from a bare
# ``python -m pytest`` invocation outside the workspace pytest config.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "libs" / "temporal-shared" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# noqa: E402 - imports must follow the ``sys.path`` bootstrap above.

from temporal_shared.confluence import (  # noqa: E402
    InvalidJiraIssueLinkError,
    InvalidTopicError,
    compute_provenance_footer,
    format_page_title,
)
from temporal_shared.confluence_dedup import (  # noqa: E402
    AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
    AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
    DEFAULT_OVERWRITE_FRESHNESS,
    PROBE_PAGE_TITLE_PREFIX,
    is_probe_page,
    should_skip_overwrite,
    should_skip_section_update,
)


# ---------------------------------------------------------------------------
# Constants - pinned from the production modules
# ---------------------------------------------------------------------------

#: Anchor timestamp for the overwrite-protection strategy. Fixed so the
#: ``±10 minute`` offset window the brief calls out is reproducible
#: across runs and Hypothesis shrinks; the chosen value lies safely
#: away from any DST transition or UNIX-epoch edge.
_NOW_ANCHOR: Final[datetime] = datetime(
    2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc
)

#: Default freshness window (5 minutes per). Pulled from the
#: production module so a future widening of the the operational rule only has
#: to be made in one place.
_FRESHNESS: Final[timedelta] = DEFAULT_OVERWRITE_FRESHNESS

#: Small alphabet for ``bot_ids`` and ``last_editor_account_id`` so the
#: probability of an editor-being-a-bot draw is non-trivial; without
#: the small alphabet Hypothesis would almost never land on the
#: bot-on-bot proceed branch and the property would be poorly covered.
_ACTOR_ALPHABET: Final[tuple[str, ...]] = (
    "bot-1",
    "bot-2",
    "bot-3",
    "human-1",
    "human-2",
    "human-3",
    "human-4",
)

#: ``YYYY-MM-DD`` regex used by P-title-1 and P-title-2.
_TITLE_SHAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"^.+ - \d{4}-\d{2}-\d{2}$"
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# ---- is_probe_page -------------------------------------------------------


# Probe-shape titles: the canonical foundation form is
# ``_AI_PROBE_<unix_ts>_DELETE_ME`` but the prefix check is
# deliberately loose ( / module docstring), so we generate the
# prefix + an arbitrary printable-ASCII tail.
_PROBE_TAIL: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.characters(
        min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters=""
    ),
    min_size=0,
    max_size=32,
)


@st.composite
def _probe_titles(draw: st.DrawFn) -> str:
    """Strategy emitting a title that matches the probe sentinel prefix."""

    return PROBE_PAGE_TITLE_PREFIX + draw(_PROBE_TAIL)


# Non-probe titles: any reasonable Confluence title that does NOT start
# with the probe prefix. We use a short alphabet of letters / digits /
# spaces / punctuation so the strategy is fast and the filter rejection
# rate stays low.
_NON_PROBE_TITLE: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        max_codepoint=0x7E,
        blacklist_characters="",
    ),
    min_size=0,
    max_size=64,
).filter(lambda s: not s.startswith(PROBE_PAGE_TITLE_PREFIX))


# Mixed strategy - half probe shapes, half non-probe shapes - used for
# the determinism property which does not care which branch is hit.
_ANY_TITLE: Final[st.SearchStrategy[str]] = st.one_of(
    _probe_titles(),
    _NON_PROBE_TITLE,
)


# ---- should_skip_overwrite ----------------------------------------------


# Bot-id sets: small frozensets drawn from the alphabet above. Sized 0-3
# so the empty-set edge case (no configured bots) is well-covered.
_BOT_IDS: Final[st.SearchStrategy[frozenset[str]]] = st.sets(
    st.sampled_from(_ACTOR_ALPHABET),
    min_size=0,
    max_size=3,
).map(frozenset)


# Editor: any actor from the alphabet, plus ``None`` (page never edited).
_LAST_EDITOR: Final[st.SearchStrategy[str | None]] = st.one_of(
    st.none(),
    st.sampled_from(_ACTOR_ALPHABET),
)


# Last-edit timestamp offset relative to the anchor ``now``. The brief
# pins this to ``±10 minutes`` so the 5-minute boundary is straddled
# in both directions; we also include ``None`` (no recorded edit).
_OFFSET_SECONDS: Final[st.SearchStrategy[int]] = st.integers(
    min_value=-600, max_value=600
)


@st.composite
def _last_edit_ats(
    draw: st.DrawFn,
) -> datetime | None:
    """Strategy emitting a tz-aware datetime within ±10 min of anchor, or None."""

    if draw(st.booleans()):
        return None
    offset = draw(_OFFSET_SECONDS)
    return _NOW_ANCHOR + timedelta(seconds=offset)


# ---- should_skip_section_update -----------------------------------------


# Identifier alphabets - all non-empty since the helper rejects empty
# key components with ``ValueError`` (we exercise the happy path here;
# the validation paths are covered by the unit-test suite).
_KEY_COMPONENT: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.characters(
        min_codepoint=ord("a"),
        max_codepoint=ord("z"),
        whitelist_characters="0123456789-_",
    ),
    min_size=1,
    max_size=8,
)


# Hash table: a frozenset of four-tuples drawn from the same component
# alphabet. We deliberately keep the table small (0-4 entries) so
# Hypothesis covers both the "key present" and "key absent" branches
# with high probability.
_HASH_KEY_TUPLE: Final[st.SearchStrategy[tuple[str, str, str, str]]] = (
    st.tuples(_KEY_COMPONENT, _KEY_COMPONENT, _KEY_COMPONENT, _KEY_COMPONENT)
)
_HASH_TABLE: Final[
    st.SearchStrategy[frozenset[tuple[str, str, str, str]]]
] = st.sets(_HASH_KEY_TUPLE, min_size=0, max_size=4).map(frozenset)


# ---- compute_provenance_footer ------------------------------------------


# Valid Jira issue links: HTTPS URL with a recognisable ``/browse/{KEY}``
# tail. The production validator accepts ``https://<host>/.../browse/
# {ISSUE_KEY}`` so we emit the simplest shape (no extra path segments)
# and also include a sample with a query/fragment tail.
_HOSTS: Final[st.SearchStrategy[str]] = st.sampled_from(
    (
        "acme.atlassian.net",
        "jira.example.com",
        "issues.acme.example",
    )
)


@st.composite
def _issue_keys(draw: st.DrawFn) -> str:
    """``PROJ-NNN`` shape - same alphabet used by ``identifiers``."""

    head = draw(st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    tail = draw(
        st.text(
            alphabet=st.sampled_from(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            ),
            min_size=1,
            max_size=4,
        )
    )
    num = draw(st.integers(min_value=1, max_value=99_999))
    return f"{head}{tail}-{num}"


@st.composite
def _jira_issue_links(draw: st.DrawFn) -> str:
    host = draw(_HOSTS)
    issue_key = draw(_issue_keys())
    return f"https://{host}/browse/{issue_key}"


# Empty / whitespace-only inputs that the helper must reject.
_EMPTY_OR_WS_LINKS: Final[st.SearchStrategy[str]] = st.sampled_from(
    ("", " ", " ", "\t", "\n", " \t \n ")
)


# ---- format_page_title --------------------------------------------------


# Topics: non-empty short ASCII / Latin-extended text. We exclude the
# control / XML-reserved characters that the validator rejects so the
# happy-path strategy does not spend Hypothesis budget on inputs that
# would always raise.
_TOPIC_ALPHABET: Final[st.SearchStrategy[str]] = st.characters(
    min_codepoint=0x20,
    max_codepoint=0x7E,
    blacklist_characters="<>&\"",
)


@st.composite
def _topics(draw: st.DrawFn) -> str:
    """Non-empty topic that survives the validator's char-class filter."""

    body = draw(
        st.text(alphabet=_TOPIC_ALPHABET, min_size=1, max_size=40)
    )
    # Reject all-whitespace bodies - those would raise InvalidTopicError
    # and we want the happy-path strategy to land on success.
    if not body.strip():
        body = body + "x"
    return body


# Calendar dates within a wide-but-safe window so ``strftime`` always
# produces a well-formed ``YYYY-MM-DD`` string.
_DATES: Final[st.SearchStrategy[date]] = st.dates(
    min_value=date(2020, 1, 1),
    max_value=date(2099, 12, 31),
)


# Empty / whitespace-only topics that must raise ``InvalidTopicError``.
_EMPTY_OR_WS_TOPICS: Final[st.SearchStrategy[str]] = st.sampled_from(
    ("", " ", " ", "\t", "\n", " \t \n ")
)


# ---------------------------------------------------------------------------
# is_probe_page, invariant.e)
# ---------------------------------------------------------------------------


class TestIsProbePage:
    """invariant - ``_AI_PROBE_*`` prefix detection."""

    @settings(max_examples=100, deadline=None)
    @given(title=_probe_titles())
    def test_probe_titles_are_detected(self, title: str) -> None:
        """P-probe-1: every ``_AI_PROBE_`` prefixed title is a probe.


 """
        assert title.startswith(PROBE_PAGE_TITLE_PREFIX)
        assert is_probe_page(title) is True

    @settings(max_examples=100, deadline=None)
    @given(title=_NON_PROBE_TITLE)
    def test_non_probe_titles_are_rejected(self, title: str) -> None:
        """P-probe-2: titles that do not start with the prefix return False.


 """
        assert not title.startswith(PROBE_PAGE_TITLE_PREFIX)
        assert is_probe_page(title) is False

    @settings(max_examples=100, deadline=None)
    @given(title=_ANY_TITLE)
    def test_is_probe_page_is_deterministic(self, title: str) -> None:
        """P-probe-3: same input → same output across two calls.


 """
        assert is_probe_page(title) == is_probe_page(title)

    def test_concrete_example_canonical_probe(self) -> None:
        """Concrete regression for the documented foundation shape.


 """
        assert is_probe_page("_AI_PROBE_1700000000_DELETE_ME") is True

    def test_concrete_example_non_probe(self) -> None:
        """Concrete regression for a normal-looking page title.


 """
        assert is_probe_page("Quarterly Review - 2026-05-14") is False


# ---------------------------------------------------------------------------
# should_skip_overwrite, invariant.d)
# ---------------------------------------------------------------------------


class TestShouldSkipOverwrite:
    """invariant - non-bot edit within freshness window blocks update."""

    @settings(max_examples=100, deadline=None)
    @given(
        last_editor=_LAST_EDITOR,
        last_edit_at=_last_edit_ats(),
        bot_ids=_BOT_IDS,
    )
    def test_skip_iff_recent_non_bot_edit(
        self,
        last_editor: str | None,
        last_edit_at: datetime | None,
        bot_ids: frozenset[str],
    ) -> None:
        """P-overwrite-1: skip iff non-bot edited within the freshness window.

 The full predicate (per + module docstring):

 skip ⇔ last_editor is not None
 AND last_edit_at is not None
 AND last_editor ∉ bot_ids
 AND timedelta(0) <= now - last_edit_at < freshness

 A future-dated edit (negative delta) does not block - the
 helper treats clock skew as "not recent enough to block"
 rather than always-blocking.


 """
        decision = should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        )

        # Compute the expected decision using the same predicate the
        # implementation documents - re-derive rather than mirror so a
        # silent contract change in the source surfaces as a property
        # failure rather than a stale duplicate constant.
        if last_editor is None or last_edit_at is None:
            expected_skip = False
        elif last_editor in bot_ids:
            expected_skip = False
        else:
            delta = _NOW_ANCHOR - last_edit_at
            expected_skip = (
                timedelta(0) <= delta < _FRESHNESS
            )

        assert decision.skip is expected_skip

    @settings(max_examples=100, deadline=None)
    @given(
        last_editor=_LAST_EDITOR,
        last_edit_at=_last_edit_ats(),
        bot_ids=_BOT_IDS,
    )
    def test_audit_event_is_pinned_when_skipping(
        self,
        last_editor: str | None,
        last_edit_at: datetime | None,
        bot_ids: frozenset[str],
    ) -> None:
        """P-overwrite-2: audit event is the the operational rule-pinned literal.


 """
        decision = should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        )

        if decision.skip:
            assert decision.audit_event == AUDIT_CONFLUENCE_OVERWRITE_PROTECTED
        else:
            assert decision.audit_event is None

    @settings(max_examples=100, deadline=None)
    @given(
        last_editor=_LAST_EDITOR,
        last_edit_at=_last_edit_ats(),
        bot_ids=_BOT_IDS,
    )
    def test_should_skip_overwrite_is_deterministic(
        self,
        last_editor: str | None,
        last_edit_at: datetime | None,
        bot_ids: frozenset[str],
    ) -> None:
        """P-overwrite-3: same input → same decision across two calls.


 """
        first = should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        )
        second = should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        )
        assert first == second


# ---------------------------------------------------------------------------
# should_skip_section_update, invariant.b)
# ---------------------------------------------------------------------------


class TestShouldSkipSectionUpdate:
    """invariant - section dedup by content hash."""

    @settings(max_examples=100, deadline=None)
    @given(
        workflow_id=_KEY_COMPONENT,
        page_id=_KEY_COMPONENT,
        section_path=_KEY_COMPONENT,
        content_hash=_KEY_COMPONENT,
        hash_table=_HASH_TABLE,
    )
    def test_skip_iff_key_in_table(
        self,
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
        hash_table: frozenset[tuple[str, str, str, str]],
    ) -> None:
        """P-section-1: skip ⇔ ``(wf, page, section, hash) ∈ hash_table``.


 """
        key = (workflow_id, page_id, section_path, content_hash)
        decision = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        )
        assert decision.skip is (key in hash_table)

    @settings(max_examples=100, deadline=None)
    @given(
        workflow_id=_KEY_COMPONENT,
        page_id=_KEY_COMPONENT,
        section_path=_KEY_COMPONENT,
        content_hash=_KEY_COMPONENT,
        hash_table=_HASH_TABLE,
    )
    def test_audit_event_is_pinned_when_skipping(
        self,
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
        hash_table: frozenset[tuple[str, str, str, str]],
    ) -> None:
        """P-section-2: audit event is the the operational rule-pinned literal.


 """
        decision = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        )
        if decision.skip:
            assert decision.audit_event == AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP
        else:
            assert decision.audit_event is None

    @settings(max_examples=100, deadline=None)
    @given(
        workflow_id=_KEY_COMPONENT,
        page_id=_KEY_COMPONENT,
        section_path=_KEY_COMPONENT,
        content_hash=_KEY_COMPONENT,
        hash_table=_HASH_TABLE,
    )
    def test_should_skip_section_update_is_deterministic(
        self,
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
        hash_table: frozenset[tuple[str, str, str, str]],
    ) -> None:
        """P-section-3: same input → same decision across two calls.


 """
        first = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        )
        second = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        )
        assert first == second

    @settings(max_examples=100, deadline=None)
    @given(
        workflow_id=_KEY_COMPONENT,
        page_id=_KEY_COMPONENT,
        section_path=_KEY_COMPONENT,
        content_hash=_KEY_COMPONENT,
        extra_keys=st.sets(_HASH_KEY_TUPLE, min_size=0, max_size=4),
    )
    def test_inserting_key_makes_subsequent_call_skip(
        self,
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
        extra_keys: set[tuple[str, str, str, str]],
    ) -> None:
        """Round-trip: insert the key into the table → next call skips.

 This is the natural cache-population path the workflow follows
 after a successful Confluence update (see the
 ``ConfluenceSectionHashRepo`` design notes).


 """
        key = (workflow_id, page_id, section_path, content_hash)
        # Start with a table that does NOT contain ``key`` so the first
        # call lands on the proceed branch.
        table_before: frozenset[tuple[str, str, str, str]] = frozenset(
            extra_keys - {key}
        )
        first = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, table_before
        )
        assert first.skip is False

        table_after = table_before | {key}
        second = should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, table_after
        )
        assert second.skip is True
        assert second.audit_event == AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP


# ---------------------------------------------------------------------------
# compute_provenance_footer, invariant.c)
# ---------------------------------------------------------------------------


class TestComputeProvenanceFooter:
    """invariant - provenance footer contains the Jira link verbatim."""

    @settings(max_examples=100, deadline=None)
    @given(jira_issue_link=_jira_issue_links())
    def test_p_footer_1_contains_link_verbatim(
        self, jira_issue_link: str
    ) -> None:
        """P-footer-1: the link appears verbatim in the rendered footer.

 The activity layer relies on the verbatim embedding so readers
 can click through to the source issue from the rendered
 Confluence page.


 """
        footer = compute_provenance_footer(jira_issue_link)
        assert jira_issue_link in footer

    @settings(max_examples=100, deadline=None)
    @given(jira_issue_link=_jira_issue_links())
    def test_p_footer_2_footer_is_well_formed(
        self, jira_issue_link: str
    ) -> None:
        """P-footer-2: footer is non-empty and carries the AI marker.


 """
        footer = compute_provenance_footer(jira_issue_link)
        assert footer  # non-empty
        # The 🤖 emoji is the canonical AI-attribution marker ( / B7).
        assert "🤖" in footer

    @settings(max_examples=100, deadline=None)
    @given(empty_link=_EMPTY_OR_WS_LINKS)
    def test_p_footer_3_empty_link_raises_value_error(
        self, empty_link: str
    ) -> None:
        """P-footer-3: empty / whitespace-only inputs raise ``ValueError``.

 Note (deviation): the task brief states "empty → empty
 string", but the production helper raises:class:`InvalidJiraIssueLinkError` (a ``ValueError`` subclass)
 so a silently-empty footer cannot strip the AI-attribution
 required by / §16.12 B7. The invariant
 validates the as-implemented contract; see the module
 docstring for the rationale.


 """
        with pytest.raises(InvalidJiraIssueLinkError):
            compute_provenance_footer(empty_link)
        # The exception class is explicitly a ``ValueError`` subclass so
        # callers using the broader ``except ValueError`` block still
        # catch it.
        assert issubclass(InvalidJiraIssueLinkError, ValueError)

    @settings(max_examples=100, deadline=None)
    @given(jira_issue_link=_jira_issue_links())
    def test_compute_provenance_footer_is_deterministic(
        self, jira_issue_link: str
    ) -> None:
        """Same input → same footer across two calls.


 """
        first = compute_provenance_footer(jira_issue_link)
        second = compute_provenance_footer(jira_issue_link)
        assert first == second


# ---------------------------------------------------------------------------
# format_page_title, invariant.a)
# ---------------------------------------------------------------------------


class TestFormatPageTitle:
    """invariant - page-title shape + empty-topic rejection."""

    @settings(max_examples=100, deadline=None)
    @given(topic=_topics(), current_date=_DATES)
    def test_p_title_1_matches_canonical_shape(
        self, topic: str, current_date: date
    ) -> None:
        """P-title-1: output matches ``r"^.+ - \\d{4}-\\d{2}-\\d{2}$"``.


 """
        title = format_page_title(topic, "tr", current_date)
        assert _TITLE_SHAPE_RE.match(title) is not None, (
            f"Title {title!r} does not match the canonical shape"
        )

    @settings(max_examples=100, deadline=None)
    @given(topic=_topics(), current_date=_DATES)
    def test_p_title_2_contains_topic_and_iso_date(
        self, topic: str, current_date: date
    ) -> None:
        """P-title-2: title carries the topic and the ``YYYY-MM-DD`` suffix.

 The validator strips outer whitespace before composing, so the
 topic substring assertion uses ``topic.strip`` to mirror the
 documented contract.


 """
        title = format_page_title(topic, "tr", current_date)
        cleaned_topic = topic.strip()

        # Topic appears verbatim (after the validator's outer-whitespace
        # strip).
        assert cleaned_topic in title
        # Date suffix is exactly ``YYYY-MM-DD``.
        expected_date_suffix = current_date.strftime("%Y-%m-%d")
        assert title.endswith(expected_date_suffix)
        # And the canonical separator (`` - ``) sits between them.
        assert title == f"{cleaned_topic} - {expected_date_suffix}"

    @settings(max_examples=100, deadline=None)
    @given(empty_topic=_EMPTY_OR_WS_TOPICS, current_date=_DATES)
    def test_p_title_3_empty_topic_raises_value_error(
        self, empty_topic: str, current_date: date
    ) -> None:
        """P-title-3: empty / whitespace-only topics raise ``ValueError``.:class:`InvalidTopicError` is a:class:`ValueError` subclass so
 callers using ``except ValueError`` catch it.


 """
        with pytest.raises(InvalidTopicError):
            format_page_title(empty_topic, "tr", current_date)
        assert issubclass(InvalidTopicError, ValueError)

    @settings(max_examples=100, deadline=None)
    @given(topic=_topics(), current_date=_DATES)
    def test_format_page_title_is_deterministic(
        self, topic: str, current_date: date
    ) -> None:
        """Same input → same title across two calls.


 """
        first = format_page_title(topic, "tr", current_date)
        second = format_page_title(topic, "tr", current_date)
        assert first == second


# ---------------------------------------------------------------------------
# End-to-end determinism - every helper is pure
# ---------------------------------------------------------------------------


class TestDeterminism:
    """All five helpers are pure deterministic - same input → same output.

 The per-helper test classes already pin the contract for each
 function individually; this class exists as a single, easy-to-grep
 anchor for the "all helpers pure deterministic" line of the task
 brief so a future reader can confirm the property is covered
 without scrolling through five separate test classes.
 """

    @settings(max_examples=100, deadline=None)
    @given(
        title=_ANY_TITLE,
        last_editor=_LAST_EDITOR,
        last_edit_at=_last_edit_ats(),
        bot_ids=_BOT_IDS,
        workflow_id=_KEY_COMPONENT,
        page_id=_KEY_COMPONENT,
        section_path=_KEY_COMPONENT,
        content_hash=_KEY_COMPONENT,
        hash_table=_HASH_TABLE,
        jira_issue_link=_jira_issue_links(),
        topic=_topics(),
        current_date=_DATES,
    )
    def test_all_helpers_are_deterministic(
        self,
        title: str,
        last_editor: str | None,
        last_edit_at: datetime | None,
        bot_ids: frozenset[str],
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
        hash_table: frozenset[tuple[str, str, str, str]],
        jira_issue_link: str,
        topic: str,
        current_date: date,
    ) -> None:
        """End-to-end determinism: every helper agrees with itself.


 """
        assert is_probe_page(title) == is_probe_page(title)

        assert should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        ) == should_skip_overwrite(
            last_editor_account_id=last_editor,
            last_edit_at=last_edit_at,
            now=_NOW_ANCHOR,
            bot_ids=bot_ids,
        )

        assert should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        ) == should_skip_section_update(
            workflow_id, page_id, section_path, content_hash, hash_table
        )

        assert compute_provenance_footer(
            jira_issue_link
        ) == compute_provenance_footer(jira_issue_link)

        assert format_page_title(
            topic, "tr", current_date
        ) == format_page_title(topic, "tr", current_date)
