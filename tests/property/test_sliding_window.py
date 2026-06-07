"""Property-based tests for sliding window deterministic compression.

Hypothesis-driven verification of the sliding-window compressor.

Behavior
--------

For any hypothesis-generated ``(messages, n)`` pair where
``messages`` is a random :class:`messages.Message` list of length
``∈ [0, 100]`` and ``n ∈ [1, 50]``, ``sliding_window.compress(
messages, n=n, summarizer=mock_summarizer)`` MUST satisfy:

    (a) ``len(messages) <= n``  output ``== messages`` (no-op).
    (b) ``len(messages) > n``  ``len(output) == n + 1``
        (1 summary message + ``n`` recent messages).
    (c) The last ``n`` elements of the output equal ``messages[-n:]``
        verbatim - original ordering preserved.
    (d) The first element of a compressed output has
        ``role == "system"`` and its ``text`` contains the substring
        ``"[Önceki konuşma özeti]"``.
    (e) Determinism: a deterministic ``summarizer`` produces the same
        ``compress`` output for the same input on every call.
    (f) Empty input  empty output (no-op edge case).

Surface under test
------------------

The compressor lives at
``platform/services/assistant-service/src/chat/sliding_window.py``
and exposes::

    def compress(
        messages: list[Message],
        *,
        n: int,
        summarizer: Callable[[list[Message]], str],
    ) -> list[Message]: ...

The :class:`messages.Message` dataclass is the shared chat-protocol
type from ``libs/messages/src/messages/chat.py``; both the
property test and the production handler import the same class so a
schema drift in either direction surfaces here as an attribute error
rather than a silent contract divergence.

Determinism guarantee
---------------------

The determinism invariant requires that the compressor is fully deterministic
*given a deterministic summariser*. We cannot assert determinism
across summariser implementations (a real LLM-backed summariser is
nondeterministic by construction); the design pins the requirement to
the deterministic case, and we satisfy that with two summariser
strategies:

* a ``"constant"`` summariser that always returns the same string,
  isolating the compressor from any input dependence;
* a ``"length"`` summariser that maps the older-messages list to its
  ``str(len(...))`` so the summary value is a pure function of input
  length - still deterministic but exercising the "summary depends on
  input" branch.

Both branches MUST produce identical outputs across two independent
``compress`` invocations on the same input; the property assertion
covers both.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path bootstrap - make ``src.chat.sliding_window`` and ``messages``
# importable.
#
# Mirrors the pattern used by the sister property tests
# (``test_session_credential.py``, ``test_audit_one_to_one.py``) and
# the assistant-service unit tests (``services/assistant-service/
# tests/unit/test_handler.py``): we expose
# - the assistant-service root so ``from src.chat.sliding_window
# import compress`` resolves;
# - the ``libs/messages/src`` directory so ``from messages import
# Message`` resolves without a pip install.
# The workspace ``pytest.ini`` already adds the ``libs/*/src`` paths
# for the repo-level test session, but we add them defensively here so
# the file imports cleanly under ``python -m pytest <this-file>``
# from any cwd.
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
_ASSISTANT_SERVICE_ROOT: Path = (
    _WORKSPACE_ROOT / "services" / "assistant-service"
)
_LIB_SRC_DIRS: tuple[Path, ...] = (
    _WORKSPACE_ROOT / "libs" / "messages" / "src",
)

for _p in (_ASSISTANT_SERVICE_ROOT, *_LIB_SRC_DIRS):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)


from messages import Message  # noqa: E402

# Importing ``compress`` lives behind a try/except so a clean
# ``ModuleNotFoundError`` surfaces as a
# pytest collection skip with a precise reason string rather than an
# opaque traceback. Once the module is available, this becomes a normal import.
try:  # pragma: no cover - guard branch collapses once module is available
    from src.chat.sliding_window import compress  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover
    compress = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Constants from the compression contract
# ---------------------------------------------------------------------------

#: Substring the summary message MUST contain
#: (``f"[Önceki konuşma özeti] {summary}"``). The tests assert this
#: substring's presence in the first element
#: of any compressed output.
SUMMARY_PREFIX: str = "[Önceki konuşma özeti]"

#: Closed roles defined by ``messages.MessageRole``. We sample from
#: these so the generated history mixes ``system`` / ``user`` /
#: ``assistant`` / ``tool`` entries, ensuring the compressor's
#: "preserve last n verbatim" property holds across every role.
_MESSAGE_ROLES: tuple[str, ...] = ("system", "user", "assistant", "tool")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Message text alphabet: ASCII printable plus the Turkish characters
# that appear in the production summary prefix (``Önceki konuşma
# özeti``). Excluding control characters keeps the generated values
# safe for inclusion in a future JSON serialisation; the Turkish
# coverage means a buggy compressor cannot accidentally pass by
# stripping non-ASCII bytes.
_text_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        max_codepoint=0x017F,
        blacklist_categories=("Cs",),  # surrogates
    ),
    min_size=0,
    max_size=80,
)


@st.composite
def _message_strategy(draw: st.DrawFn) -> Message:
    """Generate a single :class:`messages.Message` of any role.

    The ``tool_call_id`` is drawn only when ``role == "tool"`` so the
    generated value mirrors the dataclass's "tool messages reply to a
    specific call" semantics described in
    ``libs/messages/src/messages/chat.py``. Other roles always carry
    ``tool_call_id=None`` to keep the generated history close to what
    a real ``ChatHandler.stream`` would produce.
    """

    role = draw(st.sampled_from(_MESSAGE_ROLES))
    text = draw(_text_strategy)
    if role == "tool":
        tool_call_id = draw(
            st.text(
                alphabet=st.characters(
                    min_codepoint=0x30, max_codepoint=0x7A
                ),
                min_size=1,
                max_size=20,
            )
        )
    else:
        tool_call_id = None
    return Message(role=role, text=text, tool_call_id=tool_call_id)


_messages_strategy: st.SearchStrategy[list[Message]] = st.lists(
    _message_strategy(), min_size=0, max_size=100
)

#: ``n`` ∈ [1, 50]. The default
#: production value is 20 (``DEFAULT_SLIDING_WINDOW_N`` in
#: ``src/chat/handler.py``); the strategy spans both below and above
#: that to exercise the no-op vs compress branches symmetrically.
_n_strategy: st.SearchStrategy[int] = st.integers(min_value=1, max_value=50)


# ---------------------------------------------------------------------------
# Mock summariser strategy
# ---------------------------------------------------------------------------


def _constant_summariser(_messages: Sequence[Message]) -> str:
    """Deterministic summariser ignoring its input.

    Isolates the compressor's behaviour from any input-dependent
    summary so a regression in the "older / recent" split shows up
    even when the summary text is held constant.
    """

    return "ozet"


def _length_summariser(messages: Sequence[Message]) -> str:
    """Deterministic summariser whose output depends on input length.

    Exercises the path where ``compress`` forwards the dropped older
    messages to the summariser; if the compressor accidentally fed in
    the recent slice instead, the summary length would differ from
    ``len(messages) - n`` and the determinism assertion below would
    still hold (because the same wrong slice would be passed both
    times) but the no-op vs compress switch would still be detectable
    via the ``len(output) == n + 1`` invariant.
    """

    return f"older_count={len(messages)}"


_summariser_strategy: st.SearchStrategy[Callable[[Sequence[Message]], str]] = (
    st.sampled_from((_constant_summariser, _length_summariser))
)


# ---------------------------------------------------------------------------
# Module-level skip - covers the case where the implementation is unavailable.
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.skipif(
    compress is None,
    reason=(
        "src.chat.sliding_window.compress is not importable; "
        f"import failed with: {_IMPORT_ERROR!r}."
    ),
)


# ---------------------------------------------------------------------------
# Full invariant set
# ---------------------------------------------------------------------------


@given(
    messages=_messages_strategy,
    n=_n_strategy,
    summariser=_summariser_strategy,
)
@settings(deadline=None, max_examples=200)
def test_sliding_window_compress_preserves_last_n_and_is_deterministic(
    messages: list[Message],
    n: int,
    summariser: Callable[[Sequence[Message]], str],
) -> None:
    """Output shape, ordering and determinism."""

    assert compress is not None  # for type-checker; pytestmark guards runtime

    # First invocation establishes the canonical output.
    result = compress(messages, n=n, summarizer=summariser)
    result_list = list(result)

    # ----- (a) no-op when the history fits in the window -----
    if len(messages) <= n:
        assert result_list == list(messages), (
            f"compress({messages!r}, n={n}) returned {result_list!r}; "
            f"the no-op invariant requires output == messages when "
            f"len(messages) <= n."
        )
    else:
        # ----- (b) output length == n + 1 (one summary + n recent) -----
        assert len(result_list) == n + 1, (
            f"compress({messages!r}, n={n}) produced {len(result_list)} "
            f"messages; the compressed-length invariant requires exactly n + 1 = "
            f"{n + 1} when len(messages) > n."
        )

        # ----- (c) the last n elements equal ``messages[-n:]`` -----
        # Cast both sides to ``list`` so a tuple-vs-list return type
        # difference does not leak into the assertion message - the
        # property is about *content*, not container type.
        recent_actual = result_list[-n:]
        recent_expected = list(messages[-n:])
        assert recent_actual == recent_expected, (
            f"compress({messages!r}, n={n}) tail differs from "
            f"messages[-n:]; the tail-preservation invariant requires verbatim "
            f"preservation. Got {recent_actual!r}, expected "
            f"{recent_expected!r}."
        )

        # ----- (d) first element is a system summary -----
        first = result_list[0]
        assert first.role == "system", (
            f"compress({messages!r}, n={n}) first element has role "
            f"{first.role!r}; the summary-role invariant requires role == 'system'."
        )
        assert SUMMARY_PREFIX in first.text, (
            f"compress({messages!r}, n={n}) summary text {first.text!r} "
            f"does not contain the required substring "
            f"{SUMMARY_PREFIX!r}."
        )

    # ----- (e) determinism: a second invocation yields the same output -----
    # The summariser strategy only ever picks deterministic callables,
    # so any difference between ``result`` and ``result_again`` is a
    # bug in ``compress`` itself.
    result_again = list(compress(messages, n=n, summarizer=summariser))
    assert result_list == result_again, (
        f"compress is non-deterministic: two calls with the same "
        f"input produced different outputs.\n"
        f"  first:  {result_list!r}\n"
        f"  second: {result_again!r}\n"
        f"the determinism invariant requires identical outputs given a "
        f"deterministic summariser."
    )


# ---------------------------------------------------------------------------
# Concrete edge case for empty input
# ---------------------------------------------------------------------------


def test_compress_empty_messages_returns_empty() -> None:
    """Empty messages list maps to empty output.

    Pinned as a deterministic example so a regression that special-
    cases ``[]`` to e.g. ``[Message("system", "[Önceki...]")]`` is
    caught even if Hypothesis happens to skip the ``len == 0`` corner
    on a given seed.
    """

    assert compress is not None

    for n in (1, 5, 20, 50):
        out = compress([], n=n, summarizer=_constant_summariser)
        assert list(out) == [], (
            f"compress([], n={n}) returned {out!r}; the empty-input invariant "
            f"requires the empty list."
        )


# ---------------------------------------------------------------------------
# Concrete regression anchor - boundary at len(messages) == n
# ---------------------------------------------------------------------------


def test_compress_at_window_boundary_is_noop() -> None:
    """``len(messages) == n`` falls under the (a) no-op clause.

    The compressor uses ``<=`` (not ``<``) for the no-op guard, so
    a history that exactly fills the window MUST be returned verbatim
    without invoking the summariser. Pinned independently of the
    Hypothesis search so a regression that flips the comparison to
    ``<`` is caught deterministically.
    """

    assert compress is not None

    history = [
        Message(role="user", text=f"u{i}", tool_call_id=None) for i in range(5)
    ]

    summariser_calls: list[int] = []

    def _tracking_summariser(older: Sequence[Message]) -> str:
        summariser_calls.append(len(older))
        return "should-not-be-called"

    out = list(
        compress(history, n=5, summarizer=_tracking_summariser)
    )

    assert out == history, (
        f"compress(history, n=len(history)) returned {out!r}; "
        "the no-op branch is required when "
        "len(messages) <= n."
    )
    assert summariser_calls == [], (
        "compress invoked the summariser on the no-op branch "
        f"(calls={summariser_calls!r}); the compressor must guard the "
        "summariser call behind len(messages) > n."
    )


# ---------------------------------------------------------------------------
# Concrete regression anchor - single-message overflow
# ---------------------------------------------------------------------------


def test_compress_single_overflow_drops_oldest_and_summarises() -> None:
    """``len(messages) == n + 1`` exercises the minimal compress path.

    With one message above the window the summariser receives a list
    of length 1 and the output's ``[1:]`` slice equals the trailing
    ``n`` messages. Anchors the core compression invariants on a fixed example
    so a regression that off-by-ones the slice (``older =
    messages[:-(n-1)]``) is caught deterministically.
    """

    assert compress is not None

    history = [
        Message(role="user", text=f"u{i}", tool_call_id=None) for i in range(4)
    ]
    captured: list[Sequence[Message]] = []

    def _capturing_summariser(older: Sequence[Message]) -> str:
        captured.append(tuple(older))
        return "S"

    out = list(compress(history, n=3, summarizer=_capturing_summariser))

    assert len(out) == 4, (
        f"compress(history, n=3) with len(history)=4 returned "
        f"{len(out)} messages; the compressed-length invariant requires n + 1 = 4."
    )
    assert out[1:] == history[-3:], (
        f"compress tail {out[1:]!r} != messages[-3:] {history[-3:]!r} "
        f"(tail-preservation invariant)."
    )
    assert out[0].role == "system" and SUMMARY_PREFIX in out[0].text, (
        f"summary message {out[0]!r} violates the summary-message invariant."
    )
    assert captured == [tuple(history[:1])], (
        f"summariser received {captured!r}; the older-slice invariant implies the "
        f"older slice is messages[:-n] = {history[:1]!r}."
    )
