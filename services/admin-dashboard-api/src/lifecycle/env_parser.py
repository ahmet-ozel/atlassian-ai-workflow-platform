"""`.env.example` parser — pure, deterministic, I/O-free.

This module turns the textual contents of a Component's
``.env.example`` file into a list of :class:`EnvField` records that the
admin-dashboard control plane uses to render the *Servis Yapılandırma
Formu* (Requirement 5) and to validate operator-supplied
``Env_Override`` payloads (Requirement 5.6, Property P4).

Design references
-----------------
* design §4.7 — ``.env.example`` Parse Modeli (line classification,
  comment buffer, quote handling, ordering).
* design §4.6 — form schema field shape (``key``, ``default_value``,
  ``comment``, ``is_sensitive``).
* Requirements 5.1, 5.2, 5.4, 5.6 — form-schema fidelity, comment
  surfacing, deterministic ordering, LHS-key set equality.

Parser contract (per design §4.7)
---------------------------------
Lines are classified in this order:

1. **Assignment** — matches ``^[A-Z][A-Z0-9_]*=.*$``. Produces an
   :class:`EnvField` whose ``comment`` is the joined comment buffer
   accumulated since the previous blank line / assignment, and whose
   ``default_value`` has matching outer quotes stripped.
2. **Comment** — matches ``^#.*$``. Each comment line contributes one
   string to the comment buffer (leading ``#`` plus a single optional
   space stripped). Consecutive comment lines join with ``\\n`` and
   are attached to the *next* assignment.
3. **Blank line** — resets the comment buffer so dangling comments do
   not leak across blank-line-separated stanzas.
4. **Anything else** — silently skipped without touching the buffer.

The parser is pure: no I/O, no global state, no exceptions on
malformed input. Order in the returned list mirrors the order of
assignment lines in ``text``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sensitive import is_sensitive_env_key

# Assignment line — uppercase identifier, ``=``, then arbitrary value.
# Matches design §4.7's regex character-for-character so the form-schema
# LHS set is deterministic across Python and TypeScript consumers.
_ASSIGNMENT_RE: re.Pattern[str] = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

# Quote characters that participate in default-value stripping.
_QUOTE_CHARS: frozenset[str] = frozenset({'"', "'"})


@dataclass(frozen=True)
class EnvField:
    """One ``KEY=VALUE`` entry parsed out of an ``.env.example`` file.

    Frozen so callers can hand instances around the control plane (form
    rendering, schema validation, audit-detail assembly) without fear
    of accidental mutation.

    Attributes
    ----------
    key:
        The LHS identifier (uppercase, digits, underscores; must start
        with a letter).
    default_value:
        The RHS as written in the file, with matching outer ASCII
        single or double quotes stripped. May be the empty string.
    comment:
        The block of comment lines immediately preceding this
        assignment, joined with ``\\n``. ``None`` when no comment
        precedes the assignment (or when a blank line reset the
        buffer).
    is_sensitive:
        Result of :func:`.sensitive.is_sensitive_env_key` on
        :attr:`key`. Mirrored on the TypeScript side via
        ``libs/web-shared/src/sensitive.ts`` so the form renders
        ``<input type="password">`` consistently (Property C4).
    """

    key: str
    default_value: str
    comment: str | None
    is_sensitive: bool


def _strip_quotes(value: str) -> str:
    """Strip a single matching pair of outer ASCII quotes from ``value``.

    Mirrors the dotenv subset described in design §4.7: ``KEY="abc"``
    and ``KEY='abc'`` both yield ``abc``. Mismatched, unclosed or
    interior quotes are left untouched so the parser does not silently
    reshape values it cannot unambiguously decode.
    """

    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARS:
        return value[1:-1]
    return value


def _strip_comment_prefix(line: str) -> str:
    """Drop the leading ``#`` (and one optional following space) of a comment line.

    ``# foo``  → ``foo``
    ``#foo``   → ``foo``
    ``#  foo`` → `` foo`` (only one space is stripped — the rest is
    preserved verbatim to keep manually-indented comment blocks intact).
    ``##``     → ``#``
    """

    # Caller has verified ``line`` starts with ``#``; trim that one char,
    # then optionally one trailing leading space.
    body = line[1:]
    if body.startswith(" "):
        body = body[1:]
    return body


def parse_env_example(text: str) -> list[EnvField]:
    """Parse the textual contents of an ``.env.example`` file.

    Parameters
    ----------
    text:
        Raw file contents. ``\\r\\n`` and ``\\n`` line endings are
        both supported via :meth:`str.splitlines`.

    Returns
    -------
    list[EnvField]
        One :class:`EnvField` per assignment line, in the order they
        appear in ``text``. Empty input yields an empty list. Lines
        that match no rule (e.g. lowercase ``key=value`` or naked
        identifiers) are silently skipped without resetting the
        comment buffer — this preserves the spec rule that *only*
        blank lines reset comment accumulation.

    Notes
    -----
    The parser is intentionally permissive on malformed input: it
    cannot raise. Design §4.7 only mandates behaviour for the three
    recognised line shapes; anything else is treated as harmless
    noise so a stray BOM or editor artefact does not break the form
    schema for an entire service.
    """

    fields: list[EnvField] = []
    comment_buffer: list[str] = []

    for raw_line in text.splitlines():
        # ``splitlines`` already drops the newline terminator, but a
        # trailing carriage return can survive on mixed-newline input
        # produced by Windows editors; strip it defensively.
        line = raw_line.rstrip("\r")

        # Rule 3: blank line → reset the comment buffer.
        if line.strip() == "":
            comment_buffer = []
            continue

        # Rule 2: comment line.
        if line.startswith("#"):
            comment_buffer.append(_strip_comment_prefix(line))
            continue

        # Rule 1: assignment line.
        match = _ASSIGNMENT_RE.match(line)
        if match is not None:
            key = match.group(1)
            value = _strip_quotes(match.group(2))
            comment = "\n".join(comment_buffer) if comment_buffer else None
            fields.append(
                EnvField(
                    key=key,
                    default_value=value,
                    comment=comment,
                    is_sensitive=is_sensitive_env_key(key),
                )
            )
            # Comment buffer always belongs to *one* assignment; reset
            # so the next field starts fresh.
            comment_buffer = []
            continue

        # Rule 4: anything else — silently skipped, buffer preserved
        # so a malformed line between a comment block and its target
        # assignment does not orphan the comment.

    return fields


__all__ = ["EnvField", "parse_env_example"]
