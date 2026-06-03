"""Property-based test for the pre-commit secret scan.

Hypothesis-driven verification of the ``precommit_scanner`` activity's
pure regex-based detection core,
:func:`src.activities.precommit_scan.scan_diff`.

For any hypothesis-generated diff variant
  (random clean code with optionally injected secret patterns:
  AWS access key, Atlassian API token, Bearer header, password=),
  ``precommit_scanner(diff)`` must (a) return ``"block"`` for every
  diff containing at least one secret pattern and report which
  pattern matched, (b) return ``"pass"`` for every diff containing
  no secret pattern, (c) return the same :class:`ScanResult` for
  the same diff on repeated invocations (determinism).

Property statements
-------------------

For any hypothesis-generated diff string ``d`` and pattern selection
``p`` drawn from the four documented secret families, the pure
:func:`scan_diff` core MUST satisfy:

(P1) **Clean diff → pass.** A diff drawn from the *clean* generator
     (random ASCII / unified-diff text guaranteed to contain none of
     the four documented secret patterns) returns
     ``ScanResult(decision="pass", matched_patterns=())``.

(P2) **Dirty diff → block + matched name.** A diff drawn from the
     *dirty* generator (clean text with one or more secret literals
     spliced in) returns ``decision == "block"`` and the corresponding
     pattern name(s) appear in ``matched_patterns``. The "block
     decision must include ``secret_pattern_matched`` field" clause of
     the blocking contract is satisfied by the non-empty ``matched_patterns`` tuple
     (the field is named ``matched_patterns`` rather than
     ``secret_pattern_matched``; the contract here is that the block decision carries
     a non-empty enumeration of which secret pattern fired).

(P3) **Idempotence / determinism.** ``scan_diff(d) == scan_diff(d)``
     for every ``d``: scanning the same diff twice yields the same
     :class:`ScanResult` (same ``decision``, same ``matched_patterns``
     in the same order).

Strategy design
---------------

The clean-diff generator emits printable ASCII without any of the
secret literal *fragments* (``AKIA``, ``ATATT3x``, ``Bearer``,
``password = "``). This makes the *clean → pass* assertion robust
against accidental collisions in random text. The dirty-diff
generator picks 1..N pattern names from the four-family table and
splices a synthesised literal of each into a random clean carrier
diff. The synthesised literals are concrete, non-overlapping shapes
that match the production regexes documented in
:mod:`src.activities.precommit_scan`.

Why test the pure core, not the activity wrapper?
-------------------------------------------------

:func:`precommit_scanner` is the Temporal activity that wraps
:func:`scan_diff` with audit emission and (in production) workflow
context lookup. The audit / context layer has its own unit-test
coverage at
``platform/workers/agent-runner-worker/tests/unit/test_precommit_scan.py``
and ``platform/tests/unit/test_precommit_scanner.py``. The property
oracle here is the *deterministic regex sweep*, which lives in the
pure :func:`scan_diff` helper — exercising it directly keeps the
property test self-contained, free of ``asyncio.run`` overhead, and
trivially replay-safe.

Hypothesis configuration
------------------------

* ``max_examples=100`` — sample budget;
  matches the rest of this property suite (``test_fix_keyword.py``,
  ``test_explain_keyword.py``).
* ``deadline=None`` — the regex sweep is fast but pytest's default
  deadline trips on debug builds and CI cold-starts; mirrors the
  pattern used by every other property test in this directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap — the precommit scanner ships under the worker
# tree at ``platform/workers/agent-runner-worker/src/activities/``.
# ``platform/pytest.ini`` only injects ``libs/*/src`` onto ``pythonpath``,
# so we wire the worker root in ahead of the import. This mirrors the
# bootstrap pattern used by ``platform/tests/unit/test_precommit_scanner.py``
# and ``test_explain_keyword.py`` in this directory.
# ---------------------------------------------------------------------------

_TESTS_ROOT: Path = Path(__file__).resolve().parents[1]  # platform/tests/
_PLATFORM_ROOT: Path = _TESTS_ROOT.parent  # platform/
_WORKER_ROOT: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker"
)  # contains ``src`` as a sub-package

_worker_root_str: str = str(_WORKER_ROOT)
if _worker_root_str not in sys.path:
    sys.path.insert(0, _worker_root_str)


# noqa: E402 below — imports follow the sys.path bootstrap above.
try:
    from src.activities.precommit_scan import (  # noqa: E402
        SECRET_PATTERNS,
        ScanResult,
        scan_diff,
    )
except ImportError as exc:  # pragma: no cover - defensive guard
    pytest.skip(
        "precommit_scanner pure helper not yet available: "
        f"{exc!r}. This coverage will run automatically once "
        "src.activities.precommit_scan lands.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------


#: The four documented secret pattern names. Mirrors the keys of
#: :data:`SECRET_PATTERNS` exactly — assert at module load time so a
#: rename in the production module surfaces as a clean error here
#: rather than an opaque generator failure.
_PATTERN_NAMES: tuple[str, ...] = (
    "aws_access_key",
    "atlassian_api_token",
    "bearer_token",
    "generic_password",
)
assert set(_PATTERN_NAMES).issubset(SECRET_PATTERNS.keys()), (
    "precommit_scan.SECRET_PATTERNS no longer exposes the documented "
    "P0 pattern names; update _PATTERN_NAMES alongside the production "
    "table or the property test loses its oracle."
)


#: Substrings that, when present in otherwise random text, would
#: accidentally fire one of the production regexes. The clean-diff
#: generator filters them out so the *clean → pass* assertion never
#: depends on luck. ``AKIA`` is the AWS prefix, ``ATATT3x`` is the
#: Atlassian token prefix, ``Bearer`` triggers the bearer-header
#: regex when followed by token chars, and ``password`` (case
#: insensitive) is the trigger for the generic-password regex.
_FORBIDDEN_FRAGMENTS: tuple[str, ...] = (
    "AKIA",
    "ATATT3x",
    "Bearer",
    "bearer",
    "BEARER",
    "password",
    "Password",
    "PASSWORD",
)


def _is_clean(text: str) -> bool:
    """Return ``True`` iff *text* contains no secret-literal fragment.

    Used as a Hypothesis ``filter`` predicate so the *clean* generator
    cannot accidentally emit a string that one of the production
    regexes would match.
    """

    return not any(frag in text for frag in _FORBIDDEN_FRAGMENTS)


# ---------------------------------------------------------------------------
# Clean-diff strategy
# ---------------------------------------------------------------------------


#: Random printable ASCII text that is guaranteed to contain none of
#: the four secret-literal fragments. Bounded at 256 chars so each
#: example fits comfortably in a property-test failure message and
#: keeps the regex sweep below the millisecond mark.
_clean_text: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,  # space
        max_codepoint=0x7E,  # tilde
        # Stick to printable ASCII; control chars don't add coverage
        # and complicate failure-message rendering.
    ),
    min_size=0,
    max_size=256,
).filter(_is_clean)


# ---------------------------------------------------------------------------
# Dirty-pattern literal generators
# ---------------------------------------------------------------------------


#: AWS access keys are ``AKIA`` + 16 chars from ``[0-9A-Z]``. We pick
#: from a fixed alphabet rather than ``st.text`` so the literal is
#: guaranteed to satisfy the production regex on every draw.
_aws_key_literal: st.SearchStrategy[str] = st.text(
    alphabet="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    min_size=16,
    max_size=16,
).map(lambda body: f"AKIA{body}")


#: Atlassian API tokens are ``ATATT3x`` + 1+ chars from
#: ``[A-Za-z0-9_-]``. Bounded at 64 chars on the body so the literal
#: fits a typical config line.
_atlassian_token_literal: st.SearchStrategy[str] = st.text(
    alphabet=(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789_-"
    ),
    min_size=1,
    max_size=64,
).map(lambda body: f"ATATT3x{body}")


#: Bearer headers are ``Bearer`` + whitespace + token chars. The
#: production regex anchors ``\bBearer`` with a word boundary, so we
#: emit the canonical ``"Bearer <token>"`` shape.
_bearer_token_literal: st.SearchStrategy[str] = st.text(
    alphabet=(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789._-"
    ),
    min_size=1,
    max_size=64,
).map(lambda body: f"Bearer {body}")


#: Generic ``password = "..."`` assignments. The production regex
#: accepts straight or double quotes (case-insensitive); we pick the
#: double-quote shape with a non-empty body for stability.
_generic_password_literal: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        min_codepoint=0x21,  # ``!``
        max_codepoint=0x7E,  # ``~``
        blacklist_characters=("'", '"'),  # value cannot contain quotes
    ),
    min_size=1,
    max_size=32,
).map(lambda body: f'password = "{body}"')


#: Mapping from pattern name → literal generator. The dirty-diff
#: composite below picks names from this table and splices the
#: matching literal into a clean carrier.
_LITERAL_BY_NAME: dict[str, st.SearchStrategy[str]] = {
    "aws_access_key": _aws_key_literal,
    "atlassian_api_token": _atlassian_token_literal,
    "bearer_token": _bearer_token_literal,
    "generic_password": _generic_password_literal,
}


# ---------------------------------------------------------------------------
# Dirty-diff composite strategy
# ---------------------------------------------------------------------------


@st.composite
def _dirty_diffs(draw: st.DrawFn) -> tuple[str, frozenset[str]]:
    """Generate ``(diff, expected_pattern_names)`` for the dirty branch.

    The diff is built by:

    1. drawing a non-empty subset of pattern names from
       :data:`_PATTERN_NAMES`,
    2. drawing a concrete literal for each name from
       :data:`_LITERAL_BY_NAME`,
    3. drawing a clean carrier prefix and suffix from
       :data:`_clean_text`,
    4. concatenating the parts with ``\\n`` separators so the result
       resembles a unified-diff body.

    The returned ``frozenset`` of pattern names is what
    :func:`scan_diff` MUST report under ``matched_patterns`` (modulo
    sort order, which is checked as part of the equality on the
    sorted tuple).
    """

    # Draw 1..4 distinct pattern names. ``unique=True`` + bounded
    # ``max_size`` guarantees Hypothesis can shrink the failing
    # example to a single-pattern minimal counterexample.
    names = draw(
        st.lists(
            st.sampled_from(_PATTERN_NAMES),
            min_size=1,
            max_size=len(_PATTERN_NAMES),
            unique=True,
        )
    )

    prefix = draw(_clean_text)
    suffix = draw(_clean_text)

    literal_lines: list[str] = []
    for name in names:
        literal = draw(_LITERAL_BY_NAME[name])
        literal_lines.append(literal)

    # Newline-separate the parts so the regexes see token boundaries
    # rather than concatenated soup. The carrier diff is glued in
    # front and behind to mimic a real diff body.
    diff = "\n".join([prefix, *literal_lines, suffix])

    return diff, frozenset(names)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


_PROFILE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.filter_too_much,),
)


@given(diff=_clean_text)
@_PROFILE
def test_clean_diff_returns_pass(diff: str) -> None:
    """(P1) A diff with no secret patterns returns ``decision='pass'``.

    The clean-diff generator filters out every documented secret
    literal fragment, so the only acceptable outcome is
    ``ScanResult(decision='pass', matched_patterns=())``. A failure
    here indicates either a generator escape (a forbidden fragment
    slipped through ``_is_clean``) or a regression in the production
    regex table that fires on innocent input.
    """

    result = scan_diff(diff)

    assert result.decision == "pass", (
        "clean diff unexpectedly produced a block decision; "
        f"matched={result.matched_patterns!r} diff={diff!r}"
    )
    assert result.matched_patterns == (), (
        "clean diff produced non-empty matched_patterns: "
        f"{result.matched_patterns!r}"
    )
    # Frozen-dataclass equality cross-check — guards against an
    # accidental override of ``__eq__`` that happens to satisfy the
    # field assertions above but breaks structural equality.
    assert result == ScanResult(decision="pass", matched_patterns=())


@given(payload=_dirty_diffs())
@_PROFILE
def test_dirty_diff_blocks_with_matched_pattern(
    payload: tuple[str, frozenset[str]],
) -> None:
    """A diff with at least one secret literal returns ``block``.

    The dirty-diff generator splices one or more synthesised secret
    literals into a clean carrier and reports the set of pattern
    names it injected. :func:`scan_diff` MUST:

    * return ``decision == "block"``,
    * include every injected pattern name in ``matched_patterns`` —
      i.e. ``injected_names ⊆ set(matched_patterns)``. Equality is
      *not* asserted because a particular literal could legitimately
      satisfy more than one regex (eg. a generated body containing
      ``Bearer`` would also fire the bearer regex); the contract is
      "block decision must include the injected pattern", not
      "exactly that and nothing else".
    * carry a non-empty ``matched_patterns`` tuple — the
      "secret_pattern_matched field" clause.
    """

    diff, injected_names = payload
    result = scan_diff(diff)

    assert result.decision == "block", (
        "dirty diff unexpectedly passed; "
        f"injected={sorted(injected_names)} diff={diff!r}"
    )
    # The "block decision must include ``secret_pattern_matched``
    # field" clause is satisfied by the non-empty tuple — the field
    # is named ``matched_patterns`` by the helper contract.
    assert result.matched_patterns, (
        "block decision missing matched_patterns enumeration "
        f"(diff={diff!r})"
    )
    matched_set = set(result.matched_patterns)
    assert injected_names.issubset(matched_set), (
        "matched_patterns is missing at least one injected pattern; "
        f"injected={sorted(injected_names)} matched={sorted(matched_set)}"
    )
    # Sanity: every reported name is from the documented table.
    assert matched_set.issubset(set(_PATTERN_NAMES)), (
        f"matched_patterns reports unknown pattern name(s): "
        f"{sorted(matched_set - set(_PATTERN_NAMES))}"
    )


@given(diff=st.one_of(_clean_text, _dirty_diffs().map(lambda p: p[0])))
@_PROFILE
def test_scan_is_idempotent(diff: str) -> None:
    """(P3) Idempotence — ``scan_diff(d) == scan_diff(d)`` for every ``d``.

    The task brief frames this as "scanning the same diff twice
    yields same decision". We assert structural equality on the
    full :class:`ScanResult` (decision *and* the sorted matched
    pattern tuple) so the property covers both the boolean gate
    decision and the audit-payload enumeration.

    The strategy union exercises both the clean and dirty branches
    so the determinism guarantee holds across both control-flow
    paths in :func:`scan_diff`.
    """

    first = scan_diff(diff)
    second = scan_diff(diff)

    # Frozen-dataclass equality covers both fields; the explicit
    # field-by-field cross-checks below catch a hypothetical
    # accidental ``__eq__`` override that masks a tuple reordering.
    assert first == second, (
        f"scan_diff is non-deterministic on diff={diff!r}: "
        f"first={first!r} second={second!r}"
    )
    assert first.decision == second.decision
    assert first.matched_patterns == second.matched_patterns


# ---------------------------------------------------------------------------
# Static sanity — generator escape guard
# ---------------------------------------------------------------------------


def test_clean_diff_filter_blocks_every_documented_fragment() -> None:
    """The clean-diff filter must reject every fragment in the regex table.

    A regression in :data:`_FORBIDDEN_FRAGMENTS` (eg. dropping
    ``AKIA`` after a refactor) would let the clean-diff generator
    emit AWS-key-shaped strings and silently break P1. This test is
    not parametrised over Hypothesis examples; it is a static guard
    that the fragment list still covers every documented pattern by
    spot-checking a known literal against ``_is_clean``.
    """

    for literal in (
        "AKIAIOSFODNN7EXAMPLE",
        "ATATT3xVeryLongAtlassianTokenContent_With-Dashes",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        'password = "hunter2"',
    ):
        assert not _is_clean(literal), (
            f"clean-diff filter unexpectedly accepted secret literal "
            f"{literal!r}; the generator could now produce a string "
            "that breaks P1 by accident."
        )
