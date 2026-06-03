"""Pure structured-choice helpers — Y8 multi-repo + Z3 execution fallback.

This module is the **single source of truth** for the structured-choice
helpers used when the LLM analysis is ambiguous (multi-repo), or when
a dept is missing the ``execution`` capability for a test-bearing flow.
The ``AutomationWorkflow`` emits a Jira comment listing the candidate
workflow types as ``[A]`` / ``[B]`` / ... markers and waits for the
user's reply.

Public API
----------

* :func:`format_choice_list` — render a list of candidates as
  Turkish Jira-comment prose with ``[A]`` / ``[B]`` markers.
* :func:`resolve_choice` — parse a user's reply for an ``[A]`` /
  ``[B]`` / ``[C]`` marker and return the matching candidate's
  ``workflow_type`` (preferred) or ``label``; on miss / out-of-range
  / no marker returns the literal string ``"unresolved"``.

Both functions are **pure** — no I/O, no clock, no randomness — so
they are safe to call directly from a workflow body via
``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

import re
from typing import Final, Mapping, Sequence

__all__ = [
    "MAX_CANDIDATES",
    "UNRESOLVED",
    "format_choice_list",
    "resolve_choice",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard cap on the number of candidates surfaced in a single
#: structured-choice menu.  Five letters (``A``–``E``) is the design
#: limit; any extra candidates are silently dropped by
#: :func:`format_choice_list`.
MAX_CANDIDATES: Final[int] = 5

#: Sentinel returned by :func:`resolve_choice` when the comment text
#: does not carry a parseable marker.
UNRESOLVED: Final[str] = "unresolved"

#: Letter labels in order — ``A`` first, ``E`` last.  Tuple so the
#: constant cannot be mutated at runtime.
_LETTERS: Final[tuple[str, ...]] = ("A", "B", "C", "D", "E")

#: Case-sensitive regex for ``[A]`` / ``[B]`` / ... markers.  The
#: design contract pins the marker to the upper-case letter so a
#: lower-case ``[a]`` reply is unresolved (matches the property
#: test's ``case-sensitive`` expectation).
_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\[([A-E])\]")

#: Header used by :func:`format_choice_list` so the rendered prose
#: opens with a stable, human-readable preamble.
_HEADER_TR: Final[str] = "Önerilen seçenekler:"

#: Footer instructing the user how to reply.  The substring
#: ``"yorum olarak"`` (lower-case) is asserted by the property test
#: so the user contract stays stable across implementations.  Kept
#: lower-case throughout the rendered prose so a case-sensitive
#: ``in`` check matches verbatim.
_FOOTER_TEMPLATE_TR: Final[str] = "yorum olarak {markers} yazın."


# ---------------------------------------------------------------------------
# format_choice_list — Turkish prose with ``[A]`` / ``[B]`` markers.
# ---------------------------------------------------------------------------


def format_choice_list(
    candidates: Sequence[Mapping[str, str]],
) -> str:
    """Render a structured-choice menu as Turkish Jira-comment prose.

    The output contains:

    * A header line (``Önerilen seçenekler:``).
    * One blank line.
    * One ``[<letter>] {label} — {rationale}`` line per candidate
      (capped at :data:`MAX_CANDIDATES`).
    * One blank line.
    * A footer instructing the user to reply with one of the
      letters (``Yorum olarak [A] veya [B] yazın.`` for two
      candidates; ``[A], [B] veya [C]`` for three; etc.).

    Each candidate dict is expected to carry at least ``label``,
    ``workflow_type`` and ``rationale`` keys; the helper is tolerant
    of missing keys via :meth:`dict.get` so a degraded input still
    produces a readable menu.

    Parameters
    ----------
    candidates:
        Sequence of candidate dicts.  Anything past the
        :data:`MAX_CANDIDATES` cap is dropped.

    Returns
    -------
    str
        Multi-line Turkish prose — never empty.  Returns a
        diagnostic placeholder if ``candidates`` is empty so callers
        never produce a malformed Jira comment.
    """

    capped = list(candidates)[:MAX_CANDIDATES]
    if not capped:
        # Defensive: a zero-candidate menu is a programming error,
        # not a user-facing case.  Return a clearly-marked
        # diagnostic so the bug shows up in the Jira comment rather
        # than silently producing an empty post.
        return f"{_HEADER_TR}\n\n(seçenek yok)"

    lines: list[str] = [_HEADER_TR, ""]
    for idx, candidate in enumerate(capped):
        letter = _LETTERS[idx]
        label = candidate.get("label") or candidate.get("workflow_type") or ""
        rationale = candidate.get("rationale") or ""
        if rationale:
            lines.append(f"[{letter}] {label} — {rationale}")
        else:
            lines.append(f"[{letter}] {label}")

    markers = [f"[{_LETTERS[i]}]" for i in range(len(capped))]
    if len(markers) == 1:
        marker_phrase = markers[0]
    elif len(markers) == 2:
        marker_phrase = f"{markers[0]} veya {markers[1]}"
    else:
        marker_phrase = ", ".join(markers[:-1]) + f" veya {markers[-1]}"

    lines.append("")
    lines.append(_FOOTER_TEMPLATE_TR.format(markers=marker_phrase))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# resolve_choice — parse ``[A]`` / ``[B]`` / ... reply markers.
# ---------------------------------------------------------------------------


def resolve_choice(
    comment_text: str,
    candidates: Sequence[Mapping[str, str]],
) -> str:
    """Resolve a user reply to one of the candidates.

    Parses ``comment_text`` for an upper-case ``[A]`` / ``[B]`` /
    ``[C]`` / ``[D]`` / ``[E]`` marker.  On the **first** match
    inside the candidate range, returns the matching candidate's
    ``workflow_type`` if present, else its ``label``.  Returns
    :data:`UNRESOLVED` when:

    * No marker is present.
    * The marker letter is past ``len(candidates)`` (e.g. ``[C]`` on
      a 2-candidate list).
    * The marker letter is outside ``A``–``E`` (the regex already
      filters these, but the contract is explicit).
    * The candidate at the matched index has neither
      ``workflow_type`` nor ``label`` populated.

    Parameters
    ----------
    comment_text:
        Jira / Bitbucket comment body.  May contain leading,
        trailing, or interleaved free-form prose; only the first
        bracketed marker is consulted.
    candidates:
        Same sequence that was passed to :func:`format_choice_list`.

    Returns
    -------
    str
        The resolved candidate's ``workflow_type`` or ``label``, or
        the literal string ``"unresolved"``.
    """

    if not isinstance(comment_text, str) or not comment_text:
        return UNRESOLVED

    match = _MARKER_RE.search(comment_text)
    if match is None:
        return UNRESOLVED

    letter = match.group(1)
    try:
        index = _LETTERS.index(letter)
    except ValueError:
        return UNRESOLVED

    if index >= len(candidates) or index >= MAX_CANDIDATES:
        return UNRESOLVED

    candidate = candidates[index]
    workflow_type = candidate.get("workflow_type")
    if isinstance(workflow_type, str) and workflow_type:
        return workflow_type
    label = candidate.get("label")
    if isinstance(label, str) and label:
        return label
    return UNRESOLVED
