"""Research output formatters (task 9.2 — Spec 2 §9 R9.4 / R9.5).

Pure-Python renderers consumed by the ``research_*`` workflow types
(``research_publish_confluence`` and ``research_summary_jira``).  The
module owns *only* the rendering — the workflow body is responsible
for invoking firecrawl, picking sources, and translating
``EgressBlocked`` outcomes into Jira comments.

Shapes
------

A *source* is an opaque mapping carrying at least these keys:

    ``title``: Human-readable title of the source (required).
    ``url``:   Canonical URL (required; rendered verbatim).
    ``accessed_at``: ISO-8601 date string (``YYYY-MM-DD``) of the
        scrape; optional — when missing the renderer omits the
        ``erişim tarihi`` clause for that source.

The mapping form is preserved (rather than upgraded to a
``dataclass``) so callers can pass through the firecrawl response
payload without an intermediate transform.

Validates Requirements: 9.4, 9.5.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


# ---------------------------------------------------------------------------
# Confluence body renderer (R9.4)
# ---------------------------------------------------------------------------


def _coerce_source(source: Any) -> tuple[str, str, str | None]:
    """Normalise a heterogeneous source value to ``(title, url, accessed_at)``.

    Accepts either a mapping (``dict``-like) or an object exposing the
    same attributes; returns a tuple of strings (with ``accessed_at``
    optional). Empty / missing fields collapse to empty strings — the
    caller decides whether to render or skip the entry.
    """

    if isinstance(source, Mapping):
        title = str(source.get("title") or "").strip()
        url = str(source.get("url") or "").strip()
        accessed_raw = source.get("accessed_at")
    else:
        title = str(getattr(source, "title", "") or "").strip()
        url = str(getattr(source, "url", "") or "").strip()
        accessed_raw = getattr(source, "accessed_at", None)
    accessed = (
        str(accessed_raw).strip()
        if isinstance(accessed_raw, str) and accessed_raw.strip()
        else None
    )
    return title, url, accessed


def format_research_publish_confluence_body(
    content: str,
    sources: Iterable[Any],
) -> str:
    """Render a Confluence-friendly research body with a sources block.

    Format::

        {content}

        ## Kaynaklar

        1. {title} — {url} — erişim tarihi {YYYY-MM-DD}
        2. ...

    Sources without a usable ``url`` are skipped so the rendered list
    never carries a dangling reference. The ``erişim tarihi`` clause
    is omitted for sources that don't supply ``accessed_at``.

    Validates Requirement 9.4.
    """

    body = (content or "").rstrip()
    rendered_sources: list[str] = []
    for index, source in enumerate(sources, start=1):
        title, url, accessed = _coerce_source(source)
        if not url:
            continue
        label = title or url
        line = f"{index}. {label} — {url}"
        if accessed:
            line = f"{line} — erişim tarihi {accessed}"
        rendered_sources.append(line)

    if not rendered_sources:
        return body

    sources_block = "\n".join(rendered_sources)
    return f"{body}\n\n## Kaynaklar\n\n{sources_block}"


# ---------------------------------------------------------------------------
# Jira comment renderer (R9.5)
# ---------------------------------------------------------------------------


def _truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_words`` whitespace-separated tokens.

    Returns ``(truncated_text, was_truncated)``.  The whitespace
    splitter is intentionally permissive — Jira renders the result
    unchanged so collapsing runs of whitespace is acceptable for a
    summary preview.
    """

    if max_words <= 0:
        return "", bool(text)
    words = (text or "").split()
    if len(words) <= max_words:
        return " ".join(words), False
    return " ".join(words[:max_words]), True


def format_research_summary_jira_comment(
    summary: str,
    sources: Iterable[Any],
    max_words: int = 500,
    max_sources: int = 5,
) -> tuple[str, str | None]:
    """Render a Jira summary comment for ``research_summary_jira``.

    Returns ``(comment_text, minio_uri)``. ``minio_uri`` is ``None``
    when the rendered comment fits within ``max_words`` *and* the
    sources list fits within ``max_sources``; otherwise the second
    element carries the URI of an offloaded full-content artifact
    (the workflow body is responsible for actually writing the
    artifact and threading the URI back).

    The renderer never raises on degenerate input — empty summary
    plus empty sources yields ``("🤖 Araştırma sonucu boş döndü.", None)``
    so the bot's Jira comment is always intelligible.

    Validates Requirement 9.5.
    """

    summary_text = (summary or "").strip()
    truncated_summary, summary_was_truncated = _truncate_words(
        summary_text, max_words
    )

    rendered_sources: list[str] = []
    overflow_sources: list[str] = []
    for index, source in enumerate(sources, start=1):
        title, url, accessed = _coerce_source(source)
        if not url:
            continue
        label = title or url
        line = f"{index}. {label} — {url}"
        if accessed:
            line = f"{line} — erişim tarihi {accessed}"
        if len(rendered_sources) < max_sources:
            rendered_sources.append(line)
        else:
            overflow_sources.append(line)

    if not truncated_summary and not rendered_sources and not overflow_sources:
        # Defensive — empty firecrawl run still leaves a coherent
        # bot message in Jira.  We also require ``overflow_sources``
        # to be empty so a tight ``max_sources`` cap (e.g. 0/1) that
        # routes every URL-bearing source into overflow does not get
        # mistaken for an empty firecrawl run; otherwise the MinIO
        # offload sentinel would be dropped silently.
        return ("🤖 Araştırma sonucu boş döndü.", None)

    parts: list[str] = ["🤖 Araştırma özeti"]
    if truncated_summary:
        parts.append("")
        parts.append(truncated_summary)
    if rendered_sources:
        parts.append("")
        parts.append("## Kaynaklar")
        parts.append("")
        parts.extend(rendered_sources)

    has_overflow = bool(summary_was_truncated or overflow_sources)
    if has_overflow:
        parts.append("")
        parts.append(
            "ℹ️ Tam içerik uzun olduğu için MinIO'ya yazıldı; aşağıdaki "
            "bağlantıdan inceleyebilirsiniz."
        )
        # The URI itself is appended by the workflow body once the
        # MinIO artifact has been written; we leave a sentinel so
        # downstream code can detect the placeholder.
        minio_uri = "minio://research-summary-pending"
    else:
        minio_uri = None

    comment = "\n".join(parts).rstrip()
    return comment, minio_uri


__all__ = [
    "format_research_publish_confluence_body",
    "format_research_summary_jira_comment",
]
