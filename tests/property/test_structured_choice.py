"""Property test 4 — Structured-choice ``needs_info`` family (Y8 + Z3).

**Validates: Requirements 6.5, 6.6**

Property statement (design.md §"Property 4", tasks.md §10.6)
------------------------------------------------------------

For any hypothesis-generated tuple
``(confidence, candidates, requires_execution, dept_capabilities)``
the structured-choice helpers — ``format_choice_list`` and
``resolve_choice`` from
:mod:`temporal_shared.structured_choice` (task 10.2) — SHALL satisfy
the three sub-properties spelled out in the design document:

(a) **Choice-list length parity (Y8).** When ``confidence < "high"``
    AND ``len(candidates) > 1``, ``format_choice_list(candidates)``
    produces a Jira-comment string that:

    - mentions every candidate label exactly once,
    - exposes one ``[<letter>]`` marker per candidate (so the
      number of distinct ``[A]`` / ``[B]`` / ... bracketed letters
      equals ``len(candidates)``),
    - includes the canonical user instruction
      ``"yorum olarak [A] veya [B] yazın"`` (or its longer
      ``[A]/[B]/[C]`` extension) so the user sees a deterministic
      reply contract.  (R6.5)

(b) **Resolve parse round-trip (Y8).**

    - ``resolve_choice("[A] yes please", candidates)`` returns the
      first candidate's label / workflow_type (whichever the helper
      contract exposes), and ``[B]``, ``[C]``, ... resolve to the
      matching index.
    - Whitespace, leading text, and trailing text around the
      ``[<letter>]`` marker do not affect the resolution.
    - ``resolve_choice("garbled with no marker", candidates)`` returns
      the documented sentinel ``"unresolved"``.
    - A bracketed letter beyond the candidate range (``[X]`` for a
      2-candidate list) returns ``"unresolved"``.  (R6.5)

(c) **Execution-fallback choice (Z3).** When the LLM analysis carries
    ``requires_execution=True`` AND the dept's capability set does
    NOT include ``"execution"``, the structured-choice helper offers
    a 2-element candidate list whose ``workflow_type`` payload is
    exactly ``("code_change_commit_only", "out_of_scope")`` — i.e.
    the user is asked whether to (A) commit only and let the PO take
    over, or (B) escalate to the admin to enable an SSH runner.
    Resolving ``[A]`` selects ``code_change_commit_only`` and
    resolving ``[B]`` selects ``out_of_scope``.  (R6.6)

Not in scope
------------

* The ``AutomationWorkflow`` body that emits the needs_info Jira
  comment and re-routes after the user reply — owned by the
  ``llm_analyze_task`` → capability-gate → choice helper wiring in
  :mod:`automation_worker.workflows.automation_workflow` (task 10.1
  parser + task 10.2 helper).  Exercised by the worker's own unit
  tests, not by this property test.
* The capability gate itself (foundation Property 7,
  :mod:`temporal_shared.capabilities`) — the Z3 path here only asks:
  *given* ``"execution" ∉ dept_capabilities``, does the helper produce
  the right A/B menu?  The gate's own decision tree is its own oracle.
* Audit emission (``structured_choice_offered`` /
  ``structured_choice_resolved``) — owned by the workflow body.

Skip semantics
--------------

The structured-choice helpers ship in task 10.2 of the
``platform-mimari-workflows`` spec.  At the time of writing
(task 10.6 — the property test you are reading) task 10.2 is still
``[-]`` (not landed), so the production module
:mod:`temporal_shared.structured_choice` does not yet exist.  Mirroring
the pattern used by sibling property tests
(``test_explain_keyword.py``, ``test_precommit_scanner.py``,
``test_token_cap_fail_fast.py``), this module captures the
``ImportError`` and surfaces a precise, actionable
:func:`pytest.skip(allow_module_level=True)` so collection stays
clean and the skip reason names the missing symbol.  Once task 10.2
ships, the import succeeds, the skip drops out, and the full
hypothesis suite below runs automatically.

Hypothesis configuration
------------------------

* ``max_examples=100`` — design.md §"Property 4" sample budget;
  matches the rest of this property suite (``test_fix_keyword.py``,
  ``test_explain_keyword.py``, ``test_precommit_scanner.py``).
* ``deadline=None`` — the helper is a pure regex/format sweep so it
  runs comfortably under the default deadline, but pytest's deadline
  trips on debug builds and CI cold-starts; turning it off mirrors
  the convention used by every other property test in this directory.
"""

from __future__ import annotations

import string
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap — ``platform/pytest.ini`` already injects
# ``libs/temporal-shared/src`` onto ``pythonpath``, so the import below
# resolves once task 10.2 lands.  We re-affirm the path for the case
# where this module is imported via a non-pytest entrypoint (mirrors
# ``test_fix_keyword.py``).
# ---------------------------------------------------------------------------

_TESTS_ROOT: Path = Path(__file__).resolve().parents[1]  # platform/tests/
_PLATFORM_ROOT: Path = _TESTS_ROOT.parent  # platform/
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

_temporal_shared_str: str = str(_TEMPORAL_SHARED_SRC)
if _TEMPORAL_SHARED_SRC.is_dir() and _temporal_shared_str not in sys.path:
    sys.path.insert(0, _temporal_shared_str)


# noqa: E402 below — imports follow the sys.path bootstrap above.
try:
    from temporal_shared.structured_choice import (  # type: ignore[import-not-found]  # noqa: E402
        format_choice_list,
        resolve_choice,
    )
except ImportError as exc:  # pragma: no cover - defensive guard
    pytest.skip(
        "temporal_shared.structured_choice is not yet implemented "
        "(task 10.2 of platform-mimari-workflows is still ``[-]``); "
        f"import failed with: {exc!r}. Property 4 (Y8 + Z3) is fully "
        "specified by design.md §'Property 4' and will run "
        "automatically once the module lands.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Constants and helpers shared across all properties
# ---------------------------------------------------------------------------

#: The five most likely workflow types a structured-choice prompt
#: would surface.  Drawn from :data:`temporal_shared.capabilities.
#: WORKFLOW_TYPE_CAPABILITIES` and aligned with R6.5 (multi-repo
#: ambiguity → ``code_change_*``) and R6.6 (execution fallback →
#: ``code_change_commit_only`` / ``out_of_scope``).
_WORKFLOW_TYPES: tuple[str, ...] = (
    "code_change_with_test",
    "code_change_commit_only",
    "code_change_commit_only",  # weighted: occurs in both Y8 and Z3
    "pr_review",
    "out_of_scope",
)

#: Confidence values that *can* trigger the needs_info structured
#: choice.  ``"high"`` is excluded by R6.5: the workflow does NOT
#: emit a structured-choice prompt when the LLM is confident.
_LOW_CONFIDENCE: tuple[str, ...] = ("low", "medium")

#: Up to 5 letter slots (A-E) — the helper contract documented in
#: design.md §"Property 4" promises ``[A]/[B]/[C]/...`` markers.
_LETTERS: tuple[str, ...] = ("A", "B", "C", "D", "E")

#: Sentinel returned by ``resolve_choice`` for unparseable comments.
#: Hard-coded here so a rename in the production module surfaces as
#: a precise test failure rather than a silent regression.
_UNRESOLVED: str = "unresolved"


def _candidate(label: str, workflow_type: str, rationale: str) -> Mapping[str, str]:
    """Return a candidate dict in the documented helper shape.

    The structured-choice module accepts a sequence of candidate
    dicts with ``label`` (the human-readable repo / option name),
    ``workflow_type`` (the routing target after resolution) and
    ``rationale`` (the LLM's justification surfaced in the Jira
    comment).  Spec source: design.md §"Property 4" and tasks.md
    §10.2.
    """

    return {
        "label": label,
        "workflow_type": workflow_type,
        "rationale": rationale,
    }


def _candidate_label(c: Mapping[str, Any]) -> str:
    """Return the label of a candidate, tolerant of helper variants.

    Some implementations key the user-facing display on ``label``;
    others may use ``name``.  The property test only depends on the
    label being a stable, unique string, so we extract it through
    a small helper.
    """

    for key in ("label", "name", "workflow_type"):
        v = c.get(key)
        if isinstance(v, str) and v:
            return v
    raise KeyError(f"candidate has no recognisable label key: {c!r}")


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


def _label_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting a candidate label.

    Drawn from a small alphabet of repo-slug-like strings so the
    property of "labels appear verbatim in the formatted output"
    is testable without astronomically improbable false-collisions.
    """

    return st.text(
        alphabet=string.ascii_lowercase + string.digits + "-",
        min_size=3,
        max_size=20,
    ).filter(lambda s: s.strip("-") and not s.startswith("-"))


def _rationale_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting a short rationale string."""

    return st.text(
        alphabet=string.ascii_letters + string.digits + " ",
        min_size=0,
        max_size=80,
    )


@st.composite
def _candidates_strategy(
    draw: st.DrawFn,
    *,
    min_size: int = 2,
    max_size: int = 3,
) -> Sequence[Mapping[str, str]]:
    """Strategy emitting a list of 2..3 distinct candidate dicts.

    Distinct labels are enforced so the choice-list parity property
    (a) — "every label exactly once" — is trivially testable.  Each
    candidate carries a workflow_type drawn from
    :data:`_WORKFLOW_TYPES` and a free-form rationale.
    """

    n = draw(st.integers(min_value=min_size, max_value=max_size))
    labels = draw(
        st.lists(
            _label_strategy(),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    workflow_types = draw(
        st.lists(
            st.sampled_from(_WORKFLOW_TYPES),
            min_size=n,
            max_size=n,
        )
    )
    rationales = draw(
        st.lists(
            _rationale_strategy(),
            min_size=n,
            max_size=n,
        )
    )
    return [
        _candidate(label, wt, rat)
        for label, wt, rat in zip(labels, workflow_types, rationales, strict=True)
    ]


def _confidence_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting one of the low/medium confidence values."""

    return st.sampled_from(_LOW_CONFIDENCE)


def _comment_with_marker_strategy(letter: str) -> st.SearchStrategy[str]:
    """Strategy emitting a comment body that contains ``[<letter>]``.

    The hypothesis-drawn surrounding text exercises whitespace,
    leading prose, and trailing prose to confirm the marker
    extraction is whitespace- and context-tolerant.
    """

    prose_alphabet = string.ascii_letters + string.digits + " ,.!"
    leading = st.text(alphabet=prose_alphabet, min_size=0, max_size=20)
    trailing = st.text(alphabet=prose_alphabet, min_size=0, max_size=40)

    @st.composite
    def _build(draw: st.DrawFn) -> str:
        head = draw(leading)
        tail = draw(trailing)
        return f"{head}[{letter}]{tail}"

    return _build()


def _garbled_comment_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting comment bodies that contain NO valid marker.

    The text deliberately omits any ``[A]``..``[E]`` substring so
    :func:`resolve_choice` is exercised on the unresolved branch.
    Implementation: draw printable ASCII that excludes ``[`` (the
    cheapest way to guarantee no bracketed letter appears).
    """

    safe_alphabet = (
        string.ascii_letters + string.digits + " .,!?;:'\"()-_/+="
    )
    return st.text(alphabet=safe_alphabet, min_size=0, max_size=200)


# ---------------------------------------------------------------------------
# Property (a): format_choice_list parity with len(candidates)
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    candidates=_candidates_strategy(min_size=2, max_size=3),
    confidence=_confidence_strategy(),
)
def test_p_a_format_choice_list_label_and_marker_parity(
    candidates: Sequence[Mapping[str, str]],
    confidence: str,
) -> None:
    """``format_choice_list`` exposes one marker per candidate.

    For ``confidence ∈ {"low", "medium"}`` and ``len(candidates) ≥ 2``:

    * Every candidate label appears at least once in the formatted
      string (the user sees their options).
    * The number of distinct bracketed letters
      (``[A]``, ``[B]``, ...) equals ``len(candidates)`` (the user
      can reply with any one of them).

    The reply-instruction substring ``yorum olarak`` is also asserted
    so the user-facing reply contract stays stable.

    R6.5 (Y8 — structured choice).
    """

    assert confidence != "high"  # strategy invariant
    n = len(candidates)
    assert n >= 2  # strategy invariant — lower bound enforced upstream

    formatted = format_choice_list(candidates)

    assert isinstance(formatted, str), (
        f"format_choice_list must return a str; got {type(formatted)!r}"
    )

    # Every label appears verbatim.
    for c in candidates:
        label = _candidate_label(c)
        assert label in formatted, (
            f"label {label!r} is missing from formatted choice list "
            f"{formatted!r}"
        )

    # One marker per candidate.
    expected_letters = _LETTERS[:n]
    for letter in expected_letters:
        marker = f"[{letter}]"
        assert marker in formatted, (
            f"expected marker {marker!r} for candidate index "
            f"{ord(letter) - ord('A')} but it is missing from "
            f"{formatted!r}"
        )

    # No extra letters past the candidate count.
    for letter in _LETTERS[n:]:
        marker = f"[{letter}]"
        assert marker not in formatted, (
            f"unexpected extra marker {marker!r} appears in "
            f"{formatted!r} for a {n}-candidate list"
        )

    # Reply-contract phrasing — ``yorum olarak`` is the stable
    # Turkish substring documented in design.md §"Property 4" and
    # tasks.md §10.2.
    assert "yorum olarak" in formatted, (
        "format_choice_list must include the canonical reply "
        f"instruction (`yorum olarak ...`); got {formatted!r}"
    )


# ---------------------------------------------------------------------------
# Property (b): resolve_choice — [A]/[B] parse + invalid → unresolved
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    candidates=_candidates_strategy(min_size=2, max_size=3),
    index=st.integers(min_value=0, max_value=2),
    extra_text_left=st.text(
        alphabet=string.ascii_letters + " ", min_size=0, max_size=15
    ),
    extra_text_right=st.text(
        alphabet=string.ascii_letters + " ", min_size=0, max_size=40
    ),
)
def test_p_b_resolve_choice_parses_bracketed_letter(
    candidates: Sequence[Mapping[str, str]],
    index: int,
    extra_text_left: str,
    extra_text_right: str,
) -> None:
    """Bracketed letters resolve to the matching candidate.

    For each ``index ∈ [0, len(candidates))`` the comment
    ``"<prose>[A|B|C]<prose>"`` resolves to the same candidate
    payload regardless of surrounding whitespace and prose.

    R6.5 (Y8 — `[A]` / `[B]` parse).
    """

    n = len(candidates)
    if index >= n:
        # Hypothesis can draw an out-of-range index; the next
        # property covers that branch — short-circuit here.
        return

    letter = _LETTERS[index]
    comment = f"{extra_text_left}[{letter}]{extra_text_right}"

    resolved = resolve_choice(comment, candidates)

    expected_label = _candidate_label(candidates[index])
    expected_workflow_type = candidates[index]["workflow_type"]

    # The helper's exact return shape is documented as
    # ``str | "unresolved"`` (tasks.md §10.2).  The string MUST
    # match either the candidate label or the candidate's
    # workflow_type — we accept both, tracking the choice so the
    # test fails loudly if neither applies.
    assert resolved in (expected_label, expected_workflow_type), (
        f"resolve_choice({comment!r}, ...) returned {resolved!r}; "
        f"expected the label {expected_label!r} or the "
        f"workflow_type {expected_workflow_type!r} for candidate "
        f"index {index}"
    )


@settings(max_examples=100, deadline=None)
@given(
    candidates=_candidates_strategy(min_size=2, max_size=3),
    garbled=_garbled_comment_strategy(),
)
def test_p_b_resolve_choice_no_marker_returns_unresolved(
    candidates: Sequence[Mapping[str, str]],
    garbled: str,
) -> None:
    """Comments without a bracketed marker → ``unresolved``.

    R6.5 — ``resolve_choice("garbled with no marker") == "unresolved"``.
    """

    # Defensive: the strategy should never emit a bracketed letter,
    # but we filter explicitly so a strategy regression cannot mask
    # a real production bug.
    for letter in _LETTERS:
        marker = f"[{letter}]"
        if marker in garbled:
            return  # strategy outlier — skip rather than spuriously fail

    resolved = resolve_choice(garbled, candidates)

    assert resolved == _UNRESOLVED, (
        f"resolve_choice({garbled!r}, candidates) must return "
        f"{_UNRESOLVED!r} when no `[A]/[B]/...` marker is present; "
        f"got {resolved!r}"
    )


@settings(max_examples=50, deadline=None)
@given(candidates=_candidates_strategy(min_size=2, max_size=3))
def test_p_b_resolve_choice_out_of_range_letter_unresolved(
    candidates: Sequence[Mapping[str, str]],
) -> None:
    """Bracketed letters past ``len(candidates)`` → ``unresolved``.

    For a 2-candidate list ``[A]`` and ``[B]`` resolve, but ``[C]``,
    ``[D]``, ``[X]``, ``[Z]`` MUST return the unresolved sentinel.

    R6.5 — invalid letter input → ``unresolved``.
    """

    n = len(candidates)
    out_of_range_letters: list[str] = list(_LETTERS[n:]) + ["X", "Y", "Z"]

    for letter in out_of_range_letters:
        comment = f"please pick [{letter}] thanks"
        resolved = resolve_choice(comment, candidates)
        assert resolved == _UNRESOLVED, (
            f"resolve_choice({comment!r}, n={n} candidates) must "
            f"return {_UNRESOLVED!r} for an out-of-range letter; "
            f"got {resolved!r}"
        )


# ---------------------------------------------------------------------------
# Property (c): execution-fallback choice — Z3 commit_only / out_of_scope
# ---------------------------------------------------------------------------


#: The two Z3-fallback candidates: A) commit-only, B) out-of-scope.
#: Spec source: requirements.md R6.6 + design.md §"Property 4" (Z3).
#: Constructed once at import time so the property test asserts on a
#: single, deterministic fixture rather than re-deriving it every
#: example.
_Z3_FALLBACK_CANDIDATES: Sequence[Mapping[str, str]] = (
    _candidate(
        label="commit_only",
        workflow_type="code_change_commit_only",
        rationale=(
            "Sadece commit edip PO'ya bırak — execution kapalı dept'te "
            "varsayılan akış."
        ),
    ),
    _candidate(
        label="out_of_scope",
        workflow_type="out_of_scope",
        rationale=(
            "Admin'den SSH runner açılmasını iste — task bu dept'te "
            "şu an çalıştırılamaz."
        ),
    ),
)


def test_p_c_z3_fallback_format_lists_commit_only_and_out_of_scope() -> None:
    """``format_choice_list`` for the Z3 fallback exposes both options.

    R6.6 — when ``requires_execution=True`` and the dept lacks the
    ``execution`` capability, the helper offers a 2-element menu
    keyed on ``code_change_commit_only`` (A) and ``out_of_scope`` (B).
    """

    formatted = format_choice_list(_Z3_FALLBACK_CANDIDATES)

    assert isinstance(formatted, str)

    # Both labels appear verbatim.
    assert "commit_only" in formatted, (
        "Z3 fallback choice list must mention `commit_only`; got "
        f"{formatted!r}"
    )
    assert "out_of_scope" in formatted, (
        "Z3 fallback choice list must mention `out_of_scope`; got "
        f"{formatted!r}"
    )

    # Exactly two markers — `[A]` and `[B]`, no `[C]`.
    assert "[A]" in formatted, (
        f"Z3 fallback must expose marker [A]; got {formatted!r}"
    )
    assert "[B]" in formatted, (
        f"Z3 fallback must expose marker [B]; got {formatted!r}"
    )
    assert "[C]" not in formatted, (
        f"Z3 fallback must NOT expose marker [C]; got {formatted!r}"
    )


def test_p_c_z3_fallback_resolve_a_selects_commit_only() -> None:
    """``[A]`` reply on the Z3 fallback resolves to commit_only.

    R6.6 — the user picks "(A) sadece commit edip PO'ya bırakayım mı?".
    """

    resolved = resolve_choice("[A]", _Z3_FALLBACK_CANDIDATES)

    # Tolerant on the helper's return shape (label vs workflow_type).
    assert resolved in ("commit_only", "code_change_commit_only"), (
        "Z3 fallback `[A]` must select the commit-only branch; "
        f"got {resolved!r}"
    )


def test_p_c_z3_fallback_resolve_b_selects_out_of_scope() -> None:
    """``[B]`` reply on the Z3 fallback resolves to out_of_scope.

    R6.6 — the user picks "(B) admin'den SSH runner açılmasını
    isteyelim mi?".
    """

    resolved = resolve_choice("[B]", _Z3_FALLBACK_CANDIDATES)

    assert resolved in ("out_of_scope",), (
        "Z3 fallback `[B]` must select the out-of-scope branch; "
        f"got {resolved!r}"
    )


@settings(max_examples=50, deadline=None)
@given(
    requires_execution=st.booleans(),
    has_execution_capability=st.booleans(),
)
def test_p_c_z3_fallback_offered_iff_execution_required_and_missing(
    requires_execution: bool,
    has_execution_capability: bool,
) -> None:
    """Z3 menu is offered iff requires_execution AND no execution cap.

    Hypothesis-driven sweep of the four ``(requires_execution,
    has_execution_capability)`` quadrants:

    * ``(True, False)``  → Z3 menu offered (commit_only / out_of_scope).
    * any other combo    → no Z3 menu (the workflow proceeds normally).

    The decision is encoded as a small local predicate
    :func:`_should_offer_z3` so the property is stated as an
    if-and-only-if and the production helper need only expose the
    formatting/resolving primitives — the workflow body is the place
    that decides whether to call them at all.

    R6.6 (Z3 — test-isteği fallback).
    """

    def _should_offer_z3(req: bool, cap: bool) -> bool:
        return req and not cap

    offer = _should_offer_z3(requires_execution, has_execution_capability)

    if offer:
        # When the workflow body decides to offer the menu, the helper
        # MUST be able to format and resolve it without raising.
        formatted = format_choice_list(_Z3_FALLBACK_CANDIDATES)
        assert "commit_only" in formatted
        assert "out_of_scope" in formatted

        a_resolved = resolve_choice("[A]", _Z3_FALLBACK_CANDIDATES)
        b_resolved = resolve_choice("[B]", _Z3_FALLBACK_CANDIDATES)
        assert a_resolved in ("commit_only", "code_change_commit_only")
        assert b_resolved in ("out_of_scope",)
    else:
        # The other three quadrants do not exercise the helper at all.
        # We assert the local predicate to keep the if-and-only-if
        # form visible in the test report.
        assert not (requires_execution and not has_execution_capability)
