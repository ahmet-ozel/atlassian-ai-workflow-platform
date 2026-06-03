"""Pure formatters for Confluence page titles and provenance footers.

This module hosts two **pure** helper functions used by the
``confluence_doc_create`` and ``confluence_doc_update`` flows of the
``AgentRunnerWorkflow``:

* :func:`format_page_title` — composes a Confluence page title in the
  format ``{topic_in_target_lang} - {YYYY-MM-DD}``.
* :func:`compute_provenance_footer` — returns the collapsible markdown
  provenance footer that is appended to every bot-authored Confluence
  page.

Both functions perform **string composition + structural validation
only** — no I/O, no clocks, no random numbers, no UUIDs, no external
calls.  This is a hard precondition for being safe to invoke from
inside Temporal workflow code: a workflow that called
``datetime.now()`` here would break replay determinism.  The caller
(workflow or activity) is responsible for sourcing ``current_date``
deterministically (e.g. ``workflow.now().date()``).

The module is the single source of truth for these two formats.
Invariant-style tests and the unit tests in
``platform/libs/temporal-shared/tests/test_confluence.py`` validate
the contracts pinned here.

"""

from __future__ import annotations

import re
from datetime import date
from typing import Final, Literal, get_args

__all__ = [
    "TargetLang",
    "InvalidTopicError",
    "InvalidTargetLangError",
    "InvalidJiraIssueLinkError",
    "PAGE_TITLE_DATE_FORMAT",
    "PAGE_TITLE_SEPARATOR",
    "PAGE_TITLE_MAX_LENGTH",
    "PROVENANCE_FOOTER_TEXT_TR",
    "format_page_title",
    "compute_provenance_footer",
]


# ---------------------------------------------------------------------------
# Public type aliases and constants
# ---------------------------------------------------------------------------

#: Languages supported for Confluence page titles.
#: Mirrors the ``departments.default_language`` Literal used throughout
#: the platform (``Literal["tr", "en"]``).
TargetLang = Literal["tr", "en"]

#: Default ``target_lang`` value when the caller does not specify one
#: defaults to ``"tr"``.
_DEFAULT_TARGET_LANG: Final[TargetLang] = "tr"

#: Date format applied to ``current_date`` in :func:`format_page_title`.
#: ISO-8601 calendar date (``YYYY-MM-DD``), matching the worked example
#: (``"KVKK Yönetmelik Analizi - 2026-05-14"``).
PAGE_TITLE_DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: The literal separator (`` - `` — space, hyphen-minus, space) inserted
#: between ``topic`` and the formatted date in the page title.  Pinned
#: as a module constant so tests and downstream callers can reference
#: it without re-deriving the format string.
PAGE_TITLE_SEPARATOR: Final[str] = " - "

#: Hard upper bound on the produced page title length.  Confluence's
#: documented page-title limit is 255 characters; we apply the same
#: ceiling here so a malformed ``topic`` is rejected at composition
#: time rather than surfacing as a 400 from the Atlassian REST API.
PAGE_TITLE_MAX_LENGTH: Final[int] = 255

#: Footer body in Turkish.
#: Stored as a module constant so the property test can assert the
#: literal substring is present in the output verbatim.
PROVENANCE_FOOTER_TEXT_TR: Final[str] = (
    "🤖 Bu sayfa AI asistanı yardımıyla yazılmıştır. Kaynak: {jira_issue_link}"
)


# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

# Confluence rejects page titles containing certain control characters
# and characters that conflict with its storage-format XHTML.  We block
# all C0 control characters (including ``\t``, ``\n``, ``\r``) and the
# small set of XML-reserved characters that would otherwise need to be
# entity-encoded by the activity layer.
_DISALLOWED_TITLE_CHARS_RE: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x1f\x7f<>&\"]"
)

# Jira issue links are HTTPS URLs that contain ``/browse/{ISSUE_KEY}``.
# We accept any HTTPS URL whose path includes ``/browse/`` followed by
# a valid issue key (``[A-Z][A-Z0-9_]+-\d+``) — this matches both
# Atlassian Cloud (``https://acme.atlassian.net/browse/PAY-1``) and
# self-hosted DC instances (``https://jira.acme.example/browse/PAY-1``)
# without coupling the helper to a specific tenant hostname.
_JIRA_ISSUE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^\s]*)?/browse/[A-Z][A-Z0-9_]+-\d+(?:[/?#][^\s]*)?$"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidTopicError(ValueError):
    """Raised when ``topic`` cannot be embedded in a Confluence title.

    The error fires for an empty / whitespace-only topic, a topic that
    contains control characters or XML-reserved characters that would
    break Confluence storage format, or a topic that — after composition
    — would exceed :data:`PAGE_TITLE_MAX_LENGTH`.
    """

    def __init__(self, topic: object, *, reason: str) -> None:
        super().__init__(f"Invalid topic {topic!r}: {reason}")
        self.topic = topic
        self.reason = reason


class InvalidTargetLangError(ValueError):
    """Raised when ``target_lang`` is not one of the supported values.

    Pinned to ``Literal["tr", "en"]``.
    """

    def __init__(self, target_lang: object) -> None:
        super().__init__(
            f"Invalid target_lang {target_lang!r}: "
            f"must be one of {sorted(get_args(TargetLang))}"
        )
        self.target_lang = target_lang


class InvalidJiraIssueLinkError(ValueError):
    """Raised when ``jira_issue_link`` is not a usable HTTPS Jira URL.

    The footer is rendered verbatim into the page body, so we refuse
    inputs that would produce a malformed Confluence page (empty
    string, plain ``http://`` URLs, whitespace, control characters,
    or URLs that do not point at a recognisable ``/browse/{KEY}``
    path).
    """

    def __init__(self, jira_issue_link: object, *, reason: str) -> None:
        super().__init__(
            f"Invalid jira_issue_link {jira_issue_link!r}: {reason}"
        )
        self.jira_issue_link = jira_issue_link
        self.reason = reason


# ---------------------------------------------------------------------------
# format_page_title
# ---------------------------------------------------------------------------


def format_page_title(
    topic: str,
    target_lang: TargetLang = _DEFAULT_TARGET_LANG,
    current_date: date | None = None,
) -> str:
    """Compose a Confluence page title in the canonical platform format.

    Format
    ------
    ``{topic_in_target_lang} - {YYYY-MM-DD}``

    The function is **pure**: it performs string concatenation and
    structural validation only; it does not call ``date.today()`` or
    any other clock.  The caller is responsible for sourcing
    ``current_date`` deterministically (e.g. ``workflow.now().date()``
    inside a Temporal workflow, or a fixed Hypothesis-generated
    ``date`` inside a property test).

    The ``topic`` argument is assumed to **already be expressed in
    ``target_lang``** — the upstream LLM activity is responsible for
    translation. This helper does **not**
    translate; it only composes and validates.  The ``target_lang``
    argument is therefore part of the structural contract (the
    workflow always passes the resolved department language to make
    that contract explicit at the call site) but the value itself is
    only validated here, not used to transform ``topic``.

    Parameters
    ----------
    topic:
        The page topic, already in the target language.  Must be
        non-empty after stripping leading/trailing whitespace and must
        not contain control characters or XML-reserved characters
        (``<``, ``>``, ``&``, ``"``).  The composed title (topic +
        separator + date) must not exceed
        :data:`PAGE_TITLE_MAX_LENGTH` characters.
    target_lang:
        ISO-639-1 code for the page language; one of ``"tr"`` or
        ``"en"``. Defaults to ``"tr"``.
    current_date:
        Calendar date to embed in the title.  Required.  Passing
        ``None`` raises :class:`TypeError`; we deliberately do **not**
        default to ``date.today()`` because that would introduce
        non-determinism and silently break replay if the helper were
        called inside a workflow.

    Returns
    -------
    str
        The composed title, e.g.
        ``"KVKK Yönetmelik Analizi - 2026-05-14"``.

    Raises
    ------
    InvalidTopicError
        If ``topic`` is empty / whitespace-only, contains forbidden
        characters, or the composed title exceeds the length limit.
    InvalidTargetLangError
        If ``target_lang`` is not one of ``"tr"``, ``"en"``.
    TypeError
        If ``current_date`` is ``None`` or not a :class:`datetime.date`
        instance (a :class:`datetime.datetime` is also rejected to
        avoid time-of-day leaking into the title).

    Examples
    --------
    >>> from datetime import date
    >>> format_page_title("KVKK Yönetmelik Analizi", "tr", date(2026, 5, 14))
    'KVKK Yönetmelik Analizi - 2026-05-14'
    >>> format_page_title("Quarterly Review", "en", date(2026, 1, 7))
    'Quarterly Review - 2026-01-07'
    >>> format_page_title("Türkçe Konu", current_date=date(2026, 5, 14))
    'Türkçe Konu - 2026-05-14'
    """
    # ----- target_lang ----------------------------------------------------
    if target_lang not in get_args(TargetLang):
        raise InvalidTargetLangError(target_lang)

    # ----- current_date ---------------------------------------------------
    # ``datetime`` is a subclass of ``date``; reject it explicitly so
    # callers cannot accidentally embed a wall-clock timestamp (which
    # would silently leak into the title's date suffix).
    from datetime import datetime as _datetime  # local import — keeps
    # the public namespace tight.

    if current_date is None:
        raise TypeError(
            "format_page_title() missing required argument: 'current_date'"
        )
    if not isinstance(current_date, date) or isinstance(
        current_date, _datetime
    ):
        raise TypeError(
            f"current_date must be a datetime.date (not "
            f"{type(current_date).__name__})"
        )

    # ----- topic ---------------------------------------------------------
    if not isinstance(topic, str):
        raise InvalidTopicError(topic, reason="topic must be a string")

    # We strip outer whitespace so callers do not need to pre-trim, but
    # we reject inner control characters (newlines, tabs) which would
    # break the Confluence page-title field.
    cleaned = topic.strip()
    if not cleaned:
        raise InvalidTopicError(
            topic, reason="topic is empty after stripping whitespace"
        )
    if _DISALLOWED_TITLE_CHARS_RE.search(cleaned):
        raise InvalidTopicError(
            topic,
            reason="topic contains control or XML-reserved characters "
            "(C0 controls, '<', '>', '&', '\"')",
        )

    # ----- compose -------------------------------------------------------
    formatted_date = current_date.strftime(PAGE_TITLE_DATE_FORMAT)
    title = f"{cleaned}{PAGE_TITLE_SEPARATOR}{formatted_date}"

    if len(title) > PAGE_TITLE_MAX_LENGTH:
        raise InvalidTopicError(
            topic,
            reason=(
                f"composed title length {len(title)} exceeds "
                f"limit {PAGE_TITLE_MAX_LENGTH}"
            ),
        )

    return title


# ---------------------------------------------------------------------------
# compute_provenance_footer
# ---------------------------------------------------------------------------


def compute_provenance_footer(jira_issue_link: str) -> str:
    """Return the collapsible provenance footer for a bot-authored page.

    The footer body reads:

        🤖 Bu sayfa AI asistanı yardımıyla yazılmıştır. Kaynak:
        ``{jira_issue_link}``

    Confluence's storage format renders the standard HTML5
    ``<details>`` / ``<summary>`` element as a collapsible section, so
    we wrap the body in that element to satisfy the "collapsible
    markdown block requirement. The ``>`` blockquote marker is preserved inside
    the ``<details>`` body so the rendered prose still reads as a
    quoted note, while the surrounding ``<details>`` makes the entire
    footer collapsible by readers.

    Parameters
    ----------
    jira_issue_link:
        HTTPS URL pointing at the originating Jira issue
        (e.g. ``"https://acme.atlassian.net/browse/PAY-4211"``).  The
        URL is required to be HTTPS, contain a recognisable
        ``/browse/{ISSUE_KEY}`` path, and contain no whitespace or
        control characters — these constraints prevent the verbatim
        embedding from corrupting the resulting Confluence storage
        format.

    Returns
    -------
    str
        A multi-line markdown block containing the collapsible footer.
        The trailing newline is included so the activity layer can
        unconditionally append the footer to a page body.

    Raises
    ------
    InvalidJiraIssueLinkError
        If ``jira_issue_link`` is not a string, is empty after
        stripping, contains whitespace or control characters, or does
        not match the HTTPS ``/browse/{KEY}`` shape.

    Examples
    --------
    >>> footer = compute_provenance_footer(
    ...     "https://acme.atlassian.net/browse/PAY-4211"
    ... )
    >>> "🤖" in footer
    True
    >>> "https://acme.atlassian.net/browse/PAY-4211" in footer
    True
    >>> footer.startswith("<details>")
    True
    >>> footer.rstrip().endswith("</details>")
    True
    """
    if not isinstance(jira_issue_link, str):
        raise InvalidJiraIssueLinkError(
            jira_issue_link, reason="jira_issue_link must be a string"
        )

    cleaned = jira_issue_link.strip()
    if not cleaned:
        raise InvalidJiraIssueLinkError(
            jira_issue_link,
            reason="jira_issue_link is empty after stripping whitespace",
        )

    # Reject any embedded whitespace or control characters: the URL is
    # rendered verbatim into the page body, so a stray ``\n`` would
    # leak into the surrounding markdown and break the page.
    if any(ch.isspace() for ch in cleaned) or _DISALLOWED_TITLE_CHARS_RE.search(
        cleaned
    ):
        raise InvalidJiraIssueLinkError(
            jira_issue_link,
            reason="jira_issue_link contains whitespace or "
            "control / XML-reserved characters",
        )

    if not _JIRA_ISSUE_LINK_RE.match(cleaned):
        raise InvalidJiraIssueLinkError(
            jira_issue_link,
            reason="jira_issue_link must be an HTTPS URL of the form "
            "'https://<host>/.../browse/{ISSUE_KEY}'",
        )

    body_line = PROVENANCE_FOOTER_TEXT_TR.format(jira_issue_link=cleaned)

    # Multi-line markdown block:
    #
    #   <details>
    #   <summary>🤖 AI provenance</summary>
    #
    #   > 🤖 Bu sayfa AI asistanı yardımıyla yazılmıştır. Kaynak: {link}
    #
    #   </details>
    #
    # The blank lines around the blockquote are required for Confluence
    # to render the Markdown body inside ``<details>`` as a quote
    # rather than as adjacent inline text.
    return (
        "<details>\n"
        "<summary>🤖 AI provenance</summary>\n"
        "\n"
        f"> {body_line}\n"
        "\n"
        "</details>\n"
    )
