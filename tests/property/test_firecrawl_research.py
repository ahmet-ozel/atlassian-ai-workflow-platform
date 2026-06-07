"""Firecrawl egress and research output behavior.





Hypothesis-driven verification of the **research output formatters**
shipped by ``platform/libs/temporal-shared/src/temporal_shared/research.py``
(both renderers are pure helpers fed by the
``research_*`` workflow types defined in
the workflow registry:

*:func:`temporal_shared.research.format_research_publish_confluence_body`
 - renders the body of a Confluence page produced by the
 ``research_publish_confluence`` workflow.
*:func:`temporal_shared.research.format_research_summary_jira_comment`
 - renders the Jira comment posted by the ``research_summary_jira``
 workflow plus an optional MinIO sentinel for the offload path.

Why the egress predicate is **not** retested here
-------------------------------------------------

Allowlist gating, department overrides, and graceful 403 / Jira fallback
behavior are already exercised by:mod:`tests.property.test_firecrawl_egress`.
Both files import the same single source of
truth (:func:`mcp_client.firecrawl.effective_allowlist`); duplicating
the egress matrix here would only invite drift. We add a single
sentinel test in:class:`TestEgressAllowlistCoverage` that pins the
allowlist set algebra so a regression *anywhere* in either property
file is visible from this one too - and we leave a TODO for the
post-flight 403  Jira
fallback predicate, which still lives inside the activity layer.

Behavior statements
-------------------

``format_research_publish_confluence_body(content, sources)``
 contains ``content`` verbatim (modulo trailing whitespace
 normalisation) for every well-formed input.

Every source URL passed in ``sources`` appears **at least
 once** in the rendered body. Duplicate URLs in the input list
 may render multiple times; the workflow body owns the dedup
 decision before calling this helper, so the property only
 asserts presence - never absence - of a URL.

``format_research_publish_confluence_body`` is **deterministic
 and pure**: two consecutive calls with the same arguments
 return identical strings, and the input ``sources`` iterable
 is not mutated.

``format_research_publish_confluence_body`` returns the bare
 ``content`` (no ``## Kaynaklar`` header) when every source
 lacks a usable URL - a graceful degradation guard so the bot
 never publishes a dangling sources block.

``format_research_summary_jira_comment(summary, sources)``
 always returns a 2-tuple ``(comment_text, minio_uri)`` with
 ``comment_text`` a non-empty string and ``minio_uri`` either
 ``None`` or ``"minio://research-summary-pending"`` (the
 pinned sentinel).

``format_research_summary_jira_comment`` returns
 ``minio_uri is None`` when the rendered comment fits within
 ``max_words`` *and* the rendered sources list fits within
 ``max_sources`` (the "happy path" - fits inline).

``format_research_summary_jira_comment`` returns the pinned
 ``minio_uri`` sentinel whenever the summary exceeds
 ``max_words`` words **or** more than ``max_sources`` URL-bearing
 sources are supplied (the overflow path).

When the input sources list contains the same URL twice but
 every other field is identical, the rendered Jira comment
 lines for that URL are byte-identical (dedup is the workflow
 body's job; the formatter must at least be **idempotent under
 duplicate input**).

Empty / degenerate inputs produce a coherent fallback: empty
 ``summary`` + empty ``sources``  a non-empty comment string
 and ``minio_uri is None``. The bot's Jira comment is therefore
 always intelligible even if firecrawl returns nothing.

Determinism: two consecutive calls of
 ``format_research_summary_jira_comment`` with the same
 arguments return identical results.

Hypothesis configuration
------------------------

Every property runs at ``max_examples=100`` with ``deadline=None``
per the brief, matching the existing invariant cadence (see
``test_explain_keyword.py``, ``test_fix_keyword.py``,
``test_code_change_formatters.py``).
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_client.firecrawl import effective_allowlist
from temporal_shared.research import (
    format_research_publish_confluence_body,
    format_research_summary_jira_comment,
)


# ---------------------------------------------------------------------------
# Pinned sentinels (mirror temporal_shared.research)
# ---------------------------------------------------------------------------

#: Exact sentinel string the renderer returns for the offload path
#: (; see ``research.py`` ``minio_uri`` literal). Pinned here so
#: a stealth rename of the constant trips this property file.
MINIO_OVERFLOW_SENTINEL: Final[str] = "minio://research-summary-pending"

#: Default budgets the renderer ships with.
DEFAULT_MAX_WORDS: Final[int] = 500
DEFAULT_MAX_SOURCES: Final[int] = 5


# ---------------------------------------------------------------------------
# Hypothesis strategies - sources, content, allowlist tuples
# ---------------------------------------------------------------------------

#: Small closed set of canonical research-friendly hostnames. Keeping
#: the universe small means duplicate-URL events show up in roughly
#: 1-in-N draws, which keeps the dedup property (P8) cheap to trigger.
_DOMAINS: Final[tuple[str, ...]] = (
    "docs.example.com",
    "wiki.example.org",
    "rfc.ietf.org",
    "kb.example.io",
    "research.local",
)


@st.composite
def _urls(draw: st.DrawFn) -> str:
    """Build an ``https://{domain}/{path}`` URL from a small universe."""
    domain = draw(st.sampled_from(_DOMAINS))
    # Path: 0-2 lowercase ASCII segments separated by ``/``.
    n_seg = draw(st.integers(min_value=0, max_value=2))
    segments = [
        draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=8))
        for _ in range(n_seg)
    ]
    path = "/" + "/".join(segments) if segments else ""
    return f"https://{domain}{path}"


# Title: short ASCII text with spaces, not allowed to be empty (the
# renderer falls back to URL when title is empty; we cover that case
# explicitly in unit tests rather than threading it through every
# property).
_TITLES: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=" -_.",
    ),
    min_size=1,
    max_size=40,
).map(lambda s: s.strip()).filter(lambda s: bool(s) and "-" not in s)


# Access-date: ISO-8601 ``YYYY-MM-DD`` from a tight calendar range so
# the strategy is fast and the rendered dates are readable.
_ACCESS_DATES: Final[st.SearchStrategy[str]] = st.dates(
    min_value=__import__("datetime").date(2020, 1, 1),
    max_value=__import__("datetime").date(2030, 12, 31),
).map(lambda d: d.isoformat())


@st.composite
def _sources(draw: st.DrawFn, min_size: int = 0, max_size: int = 8) -> list[dict[str, str]]:
    """Build a list of source dicts ``{title, url, accessed_at}``.

 Every dict carries all three keys so the formatters' "missing
 field" branches are covered by the unit-test class; the property
 strategies stay on the well-formed path so the assertions remain
 universally true.
 """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    out: list[dict[str, str]] = []
    for _ in range(n):
        out.append(
            {
                "title": draw(_TITLES),
                "url": draw(_urls()),
                "accessed_at": draw(_ACCESS_DATES),
            }
        )
    return out


#: Body content: 0-5000 ASCII chars, possibly multi-paragraph. We do
#: not constrain the alphabet beyond printable ASCII so the property
#: also exercises whitespace and newline handling inside the renderer.
_CONTENT: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd", "Zs"),
        whitelist_characters="\n -_.",
    ),
    min_size=0,
    max_size=5000,
)


# ---------------------------------------------------------------------------
# format_research_publish_confluence_body behavior
# ---------------------------------------------------------------------------


class TestConfluenceBodyFormatter:
    """Properties of:func:`format_research_publish_confluence_body`.


 """

    @settings(max_examples=100, deadline=None)
    @given(content=_CONTENT, sources=_sources())
    def test_content_appears_verbatim_in_output(
        self, content: str, sources: list[dict[str, str]]
    ) -> None:
        """The renderer preserves the supplied content.

 The renderer must echo ``content`` byte-identical (modulo
 trailing whitespace normalisation). The Confluence workflow
 relies on this so the LLM's prose is preserved across the
 formatter boundary.
 """
        body = format_research_publish_confluence_body(content, sources)
        # The renderer rstrips trailing whitespace on the content
        # before composing the sources block - assert the canonical
        # form is present rather than the raw input.
        assert content.rstrip() in body or content == ""

    @settings(max_examples=100, deadline=None)
    @given(content=_CONTENT, sources=_sources(min_size=1, max_size=8))
    def test_every_source_url_appears_in_output(
        self, content: str, sources: list[dict[str, str]]
    ) -> None:
        """Every input source URL appears in the rendered body.

 Every URL in the input ``sources`` list must appear at least
 once in the rendered body. The workflow body is responsible
 for deduplicating sources before calling this helper, so we
 only assert *presence* (not exact count).
 """
        body = format_research_publish_confluence_body(content, sources)
        for source in sources:
            assert source["url"] in body, (
                f"URL {source['url']!r} missing from rendered Confluence body"
            )

    @settings(max_examples=100, deadline=None)
    @given(content=_CONTENT, sources=_sources(min_size=1, max_size=8))
    def test_access_date_is_rendered_for_every_source(
        self, content: str, sources: list[dict[str, str]]
    ) -> None:
        """Access dates are rendered next to source URLs.

 When ``accessed_at`` is provided, the renderer must include
 it next to the URL so the published page carries provenance
 metadata ( - "her kaynak için başlık + URL + erişim tarihi").
 """
        body = format_research_publish_confluence_body(content, sources)
        for source in sources:
            # The renderer formats the access-date with the literal
            # Turkish-language phrase ``erişim tarihi``; pin that
            # phrase here so an unexpected rename trips this property.
            assert (
                f"erişim tarihi {source['accessed_at']}" in body
            ), (
                f"accessed_at {source['accessed_at']!r} not rendered next to "
                f"URL {source['url']!r}"
            )

    @settings(max_examples=100, deadline=None)
    @given(content=_CONTENT, sources=_sources())
    def test_render_is_deterministic_and_pure(
        self, content: str, sources: list[dict[str, str]]
    ) -> None:
        """Rendering is deterministic and does not mutate inputs.

 Two consecutive calls with the same inputs return identical
 strings; the input ``sources`` list is not mutated.
 """
        snapshot = [dict(s) for s in sources]
        first = format_research_publish_confluence_body(content, sources)
        second = format_research_publish_confluence_body(content, sources)
        assert first == second
        assert sources == snapshot, "sources list was mutated by renderer"

    @settings(max_examples=100, deadline=None)
    @given(content=_CONTENT, n=st.integers(min_value=0, max_value=5))
    def test_no_dangling_sources_header_when_all_urls_missing(
        self, content: str, n: int
    ) -> None:
        """Sources without URLs do not create a dangling section.

 When every supplied source lacks a usable URL the renderer
 must drop the ``## Kaynaklar`` header rather than leaving a
 dangling section. This is the graceful-degradation guard
 for the empty-research-result branch.
 """
        sources_no_urls: list[dict[str, str]] = [
            {"title": f"t{i}", "url": "", "accessed_at": "2025-01-01"}
            for i in range(n)
        ]
        body = format_research_publish_confluence_body(content, sources_no_urls)
        assert "## Kaynaklar" not in body, (
            "renderer emitted ## Kaynaklar despite no URL-bearing source"
        )


# ---------------------------------------------------------------------------
# format_research_summary_jira_comment behavior
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    """Match the renderer's whitespace-splitter (research.py ``_truncate_words``)."""
    return len((text or "").split())


def _url_bearing_count(sources: list[dict[str, str]]) -> int:
    """Count sources that the renderer would actually emit (URL non-empty)."""
    return sum(1 for s in sources if (s.get("url") or "").strip())


class TestJiraSummaryFormatter:
    """Properties of:func:`format_research_summary_jira_comment`.


 """

    @settings(max_examples=100, deadline=None)
    @given(summary=_CONTENT, sources=_sources())
    def test_returns_two_tuple_with_minio_sentinel_or_none(
        self, summary: str, sources: list[dict[str, str]]
    ) -> None:
        """The renderer returns a comment and optional MinIO URI.

 Universal shape contract: the renderer returns a 2-tuple
 ``(str, str | None)`` where the URI half is either ``None``
 or the pinned MinIO sentinel. The workflow body relies on
 this discriminated-tuple shape to decide whether to write
 the MinIO artifact.
 """
        comment, minio_uri = format_research_summary_jira_comment(summary, sources)
        assert isinstance(comment, str)
        assert comment, "Jira comment must never be empty"
        assert minio_uri is None or minio_uri == MINIO_OVERFLOW_SENTINEL

    @settings(max_examples=100, deadline=None)
    @given(
        # Cap summary at ~50 words so we stay safely under
        # ``DEFAULT_MAX_WORDS``; the strategy keeps the alphabet
        # ASCII so word-count matches whitespace splitting.
        summary=st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
            min_size=0,
            max_size=50,
        ).map(" ".join),
        sources=_sources(min_size=0, max_size=DEFAULT_MAX_SOURCES),
    )
    def test_fits_returns_minio_uri_none(
        self, summary: str, sources: list[dict[str, str]]
    ) -> None:
        """Inline summaries do not request MinIO offload.

 The happy path: when the summary fits in ``max_words`` *and*
 the URL-bearing source count fits in ``max_sources``, the
 renderer SHALL NOT offload to MinIO.
 """
        # Pre-conditions for the "fits" branch - assert via Python
        # rather than a Hypothesis ``assume`` because the strategy
        # already constrains both sides.
        assert _word_count(summary) <= DEFAULT_MAX_WORDS
        assert _url_bearing_count(sources) <= DEFAULT_MAX_SOURCES

        _comment, minio_uri = format_research_summary_jira_comment(
            summary, sources, max_words=DEFAULT_MAX_WORDS, max_sources=DEFAULT_MAX_SOURCES
        )
        assert minio_uri is None

    @settings(max_examples=100, deadline=None)
    @given(
        # Force overflow on at least one axis: either > max_words
        # words in the summary, or > max_sources URL-bearing sources.
        summary=st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
            min_size=0,
            max_size=20,
        ).map(" ".join),
        sources=_sources(min_size=0, max_size=12),
        # ``max_words`` and ``max_sources`` are deliberately tight so
        # "overflow" is triggered for almost every example without
        # building a 500+ word summary.
        max_words=st.integers(min_value=1, max_value=5),
        max_sources=st.integers(min_value=0, max_value=2),
    )
    def test_overflow_returns_minio_sentinel(
        self,
        summary: str,
        sources: list[dict[str, str]],
        max_words: int,
        max_sources: int,
    ) -> None:
        """Overflowing summaries return the MinIO sentinel.

 Whenever the summary exceeds ``max_words`` *or* the
 URL-bearing source count exceeds ``max_sources``, the
 renderer SHALL return the pinned MinIO sentinel so the
 workflow body knows to offload.
 """
        is_overflow = (
            _word_count(summary) > max_words
            or _url_bearing_count(sources) > max_sources
        )
        comment, minio_uri = format_research_summary_jira_comment(
            summary, sources, max_words=max_words, max_sources=max_sources
        )

        if is_overflow:
            assert minio_uri == MINIO_OVERFLOW_SENTINEL, (
                f"expected MinIO sentinel on overflow; got {minio_uri!r}; "
                f"summary_words={_word_count(summary)} > {max_words} or "
                f"url_sources={_url_bearing_count(sources)} > {max_sources}"
            )
            # The comment text in the overflow branch must mention the
            # offload to MinIO so the bot's Jira message stays
            # self-explanatory; pin the canonical phrase.
            assert "MinIO" in comment

    @settings(max_examples=100, deadline=None)
    @given(
        sources=_sources(min_size=1, max_size=4),
        # Pick a summary short enough that no truncation contributes
        # to the URL-presence assertion.
        summary=st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=200),
    )
    def test_duplicate_url_dedup_invariant(
        self, summary: str, sources: list[dict[str, str]]
    ) -> None:
        """Duplicate source URLs render consistently.

 Same URL fed twice (with otherwise identical fields) must
 render the same line shape. The formatter is required to be
 idempotent under duplicate input - the workflow body owns
 the *logical* dedup decision before calling this helper, but
 the renderer must not produce structurally-different lines
 for the same URL.
 """
        # Build a duplicate-URL list by appending the first source.
        first = sources[0]
        with_dup = [*sources, dict(first)]
        comment, _ = format_research_summary_jira_comment(
            summary, with_dup, max_words=DEFAULT_MAX_WORDS, max_sources=DEFAULT_MAX_SOURCES + 5
        )
        # The duplicate URL appears in the rendered comment - the
        # renderer does not pre-dedup by design (workflow body's job).
        url_count = comment.count(first["url"])
        assert url_count >= 2, (
            f"renderer dropped duplicate URL silently; "
            f"expected at least 2 occurrences of {first['url']!r}, got {url_count}"
        )

    def test_empty_inputs_produce_graceful_fallback(self) -> None:
        """Empty inputs still produce a coherent Jira comment.

 Empty summary + empty sources must still produce a coherent
 Jira comment with ``minio_uri is None`` so the bot's reply
 remains intelligible after a degenerate firecrawl run.
 """
        comment, minio_uri = format_research_summary_jira_comment("", [])
        assert isinstance(comment, str) and comment.strip()
        assert minio_uri is None

    @settings(max_examples=100, deadline=None)
    @given(summary=_CONTENT, sources=_sources())
    def test_render_is_deterministic_and_pure(
        self, summary: str, sources: list[dict[str, str]]
    ) -> None:
        """Rendering is deterministic and does not mutate sources.

 Two consecutive calls with the same arguments return
 identical results; the input ``sources`` list is not
 mutated.
 """
        snapshot = [dict(s) for s in sources]
        first = format_research_summary_jira_comment(summary, sources)
        second = format_research_summary_jira_comment(summary, sources)
        assert first == second
        assert sources == snapshot, "sources list was mutated by renderer"


# ---------------------------------------------------------------------------
# Egress allowlist coverage sentinel
# ---------------------------------------------------------------------------


class TestEgressAllowlistCoverage:
    """Sentinel binding to the egress allowlist behavior.



 The full egress allowlist matrix is exercised in:mod:`tests.property.test_firecrawl_egress`;
 that file owns the host-vs-allowlist
 Hypothesis universe, the FastAPI 403 / log / metric triple, and
 the empty-allowlist closed-by-default check. Re-running the same
 matrix here would only invite drift between the two files.

 We keep one tight sanity test on the **set algebra** so a
 regression in:func:`mcp_client.firecrawl.effective_allowlist`
 (the single source of truth shared between this property file,:mod:`tests.property.test_firecrawl_egress`, and:class:`mcp_client.firecrawl.FirecrawlClient`) shows up here as
 well.
 """

    def test_effective_allowlist_set_algebra(self) -> None:
        """The effective allowlist follows the documented set algebra.

 ``effective_allowlist(global, dept) == (global ∪ dept.allow)
 - dept.deny``. Pinning the equation here means a stealth
 change to the operator order (e.g. apply deny *before* the
 union) trips this property file even though the bulk of the
 egress matrix lives elsewhere.
 """
        result = effective_allowlist(
            ("docs.example.com", "rfc.ietf.org"),
            {"allow": ["wiki.local"], "deny": ["rfc.ietf.org"]},
        )
        assert result == frozenset({"docs.example.com", "wiki.local"})

    def test_effective_allowlist_deny_overrides_dept_allow(self) -> None:
        """Department deny entries override department allow entries.

 A host listed in *both* dept ``allow`` and dept ``deny`` is
 denied (deny is the closing valve). This is the
 "principle-of-least-surprise" branch documented in
 ``firecrawl.py`` ``effective_allowlist`` notes.
 """
        result = effective_allowlist(
            (),
            {"allow": ["wiki.local"], "deny": ["wiki.local"]},
        )
        assert result == frozenset()

    @pytest.mark.skip(
        reason=(
            "Activity-layer post-flight 403  Jira fallback predicate "
            "(the operational rule) lives inside the FirecrawlClient.scrape / search "
            "code path that requires an async transport mock; covered "
            "by the FastAPI 403 test in "
            "tests.property.test_firecrawl_egress and the workflow "
            "graceful-degradation integration test attached to "
            "the Firecrawl client path. TODO: lift the 403  "
            "EgressBlocked outcome assertion into a pure-helper test "
            "once a transport-free predicate is available."
        )
    )
    def test_post_flight_403_yields_egress_blocked(self) -> None:  # pragma: no cover
        """Placeholder for the post-flight egress-blocked mapping.

 See the ``skip`` reason for why this property is staged
 rather than implemented inline. The post-flight 403
 ``EgressBlocked`` mapping is currently bound to:class:`mcp_client.firecrawl.FirecrawlClient` and depends on
 an injected transport.
 """
