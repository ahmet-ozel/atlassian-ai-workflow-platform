"""Output-action size cap with MinIO redirection.

This module hosts the size-cap policy applied to every
:class:`temporal_shared.messages.OutputAction` before its payload
reaches the ``apply()`` step in
:mod:`temporal_shared.output_actions`.  Two pure helpers plus one
Turkish-prose formatter live here:

* :data:`MAX_OUTPUT_BYTES` - the hard 1 MiB cap.
* :func:`measure_payload_bytes` - JSON-encode an
  ``OutputAction.payload`` (``tuple[tuple[str, object], ...]``) and
  return the byte length used by the cap check.  Exposed so callers
  and tests can reason about the cap without rebuilding the encoding
  logic.
* :func:`redirect_oversized_payload` - the cap helper itself.  When a
  payload exceeds :data:`MAX_OUTPUT_BYTES` it invokes a caller-supplied
  ``minio_callback`` to offload the full body to
  ``ai-runs/{workflow_id}/output-{idx}.json`` and returns a fresh
  :class:`OutputAction` whose payload is replaced with a
  ``{"summary", "minio_uri", "size_bytes"}`` triple.  Below the cap the
  original action is returned unchanged.
* :func:`format_final_jira_comment` - the Turkish prose formatter
  combining the lists of completed critical steps and failed
  best-effort actions into the canonical final-comment shape.

Why a sibling module to ``output_actions``?
-------------------------------------------

The partition + apply orchestrator lives in a sibling module. The
size-cap helpers ship in :mod:`temporal_shared.output_size_cap` so the
consumer module can import them without a forward-reference dance.
Both modules are re-exported from :mod:`temporal_shared` to give call
sites a single import surface.

Purity and replay determinism
-----------------------------

Every public helper here is **pure** - no clocks, no randomness, no
UUIDs, no globals, no module-level mutable state.  The only side
effect happens through the caller-supplied ``minio_callback``.  That
callback is awaited exactly once when (and only when) the payload
exceeds the cap, mirroring the pattern used by
:class:`mcp_client.firecrawl.FirecrawlClient` for its overflow
branch.  The encoding (UTF-8 JSON with ``ensure_ascii=False`` and
``sort_keys=True``) is deterministic so two evaluations of the same
input produce identical byte strings, which is exactly what the
determinism assertion in this module's property test relies on.

The Turkish final-comment formatter is also pure: emoji literals,
fixed labels, and ``", "`` joiners.  It never localises by clock or
locale - the whole platform addresses end users in Turkish.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Final, Protocol, runtime_checkable

from .messages import OutputAction

__all__ = [
    # cap configuration
    "MAX_OUTPUT_BYTES",
    "SUMMARY_TRUNCATE_CHARS",
    "MINIO_KEY_TEMPLATE",
    # payload measurement
    "measure_payload_bytes",
    # redirect helper
    "MinioCallback",
    "redirect_oversized_payload",
    # final-comment formatter
    "format_final_jira_comment",
    # final-comment string constants (exposed for tests / callers)
    "FINAL_COMMENT_CRITICAL_PREFIX",
    "FINAL_COMMENT_BEST_EFFORT_PREFIX",
]


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Hard byte cap on a single :class:`OutputAction.payload`.
#: Exactly 1 MiB is interpreted as 2**20 bytes per the common SI/IEC
#: convention used by S3/MinIO size headers.  Anything above this cap
#: is offloaded to MinIO and replaced with a summary stub.
MAX_OUTPUT_BYTES: Final[int] = 1 * 1024 * 1024

#: Number of characters retained when summarising a JSON-encoded
#: payload for the LLM context.  256 chars keeps
#: the summary under one Jira-comment paragraph and well below the
#: token-cap (T13) without losing the leading shape of the structured
#: payload (which usually identifies the action kind in the first few
#: tens of chars).  Truncation is byte-safe because we operate on a
#: ``str`` and slice by code-point.
SUMMARY_TRUNCATE_CHARS: Final[int] = 256

#: Format string for the offloaded MinIO object key.  Exposed so tests
#: can assert that the helper builds keys against this template
#: literally and so future migrations have a single edit point.
MINIO_KEY_TEMPLATE: Final[str] = "ai-runs/{workflow_id}/output-{idx}.json"


# ---------------------------------------------------------------------------
# Final-comment string constants
# ---------------------------------------------------------------------------
#
# The requirement pins the exact Turkish prose; we expose the two
# label prefixes as constants so call sites and tests can reference
# them by name rather than by literal string match.

#: Leading prose of the "completed critical steps" line in the final
#: Jira comment.  The line is omitted when
#: ``critical_done`` is empty.  The Turkish characters and the leading
#:  emoji are written as Unicode escapes so the source file stays
#: ASCII-clean and the byte content is unambiguous regardless of the
#: editor's encoding heuristics.  At runtime the value is the real
#: glyph string ``"\u2705 Tamamlanan kritik adımlar: "``.
FINAL_COMMENT_CRITICAL_PREFIX: Final[str] = (
    "\u2705 Tamamlanan kritik ad\u0131mlar: "
)

#: Leading prose of the "failed best-effort actions" line in the final
#: Jira comment.  The line is omitted when
#: ``best_effort_failed`` is empty.  Same encoding rationale as
#: :data:`FINAL_COMMENT_CRITICAL_PREFIX`.  At runtime the value is the
#: real glyph string ``" Başarısız yan-aksiyonlar: "``.
FINAL_COMMENT_BEST_EFFORT_PREFIX: Final[str] = (
    "\u26a0\ufe0f Ba\u015far\u0131s\u0131z yan-aksiyonlar: "
)


# ---------------------------------------------------------------------------
# MinIO callback protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class MinioCallback(Protocol):
    """Async callable that writes ``body`` under ``key`` and returns a URI.

    The redirect helper invokes the callback as::

        uri = await minio_callback(key=..., body=...)

    The expected URI shape is the canonical S3 form
    ``s3://{bucket}/{key}`` (mirroring
    :mod:`mcp_client.firecrawl`); however, the helper is agnostic
    about the prefix - anything the caller's MinIO writer chooses to
    return is propagated verbatim into the replacement payload's
    ``minio_uri`` field.

    Returning a URI rather than ``None`` lets the workflow surface a
    deep-link to the offloaded artifact in the final Jira comment so
    the human reviewer can fetch the full body when needed.

    The protocol is :func:`runtime_checkable` so tests can ``isinstance``
    a stub against it without inheriting; production code passes a
    bound method or a plain ``async def`` lambda.
    """

    async def __call__(self, *, key: str, body: bytes) -> str:  # pragma: no cover - protocol
        ...


# Public type alias used in signatures.  We accept any awaitable callable
# matching the keyword shape - :class:`MinioCallback` is the
# documentation contract; the alias keeps the ``async def`` site simple.
_MinioCallable = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------------------
# Payload encoding & size measurement
# ---------------------------------------------------------------------------


def _payload_to_dict(payload: Sequence[tuple[str, object]]) -> dict[str, object]:
    """Lift the tuple-of-pairs payload into a JSON-serialisable dict.

    :class:`OutputAction.payload` is documented as
    ``tuple[tuple[str, object], ...]`` (an immutable ordered
    key-value mapping - see :mod:`temporal_shared.messages`).  The
    JSON encoder needs a ``dict``; we materialise it once per call.

    Duplicate keys in the input are resolved last-wins, mirroring
    Python's standard ``dict(pairs)`` semantics.  This is consistent
    with the implicit assumption everywhere else in the codebase that
    ``OutputAction.payload`` carries a unique key set per action.
    """
    return dict(payload)


def measure_payload_bytes(payload: Sequence[tuple[str, object]]) -> int:
    """Return the byte length of the JSON encoding of ``payload``.

    The encoding is deterministic (UTF-8, ``ensure_ascii=False``,
    ``sort_keys=True``, no extra whitespace) so the same logical
    payload always measures to the same byte length regardless of
    Python's dict insertion order quirks.  This is the byte length
    consulted by :func:`redirect_oversized_payload` against
    :data:`MAX_OUTPUT_BYTES`.

    Parameters
    ----------
    payload:
        :class:`OutputAction.payload`-shaped sequence of
        ``(key, value)`` pairs.

    Returns
    -------
    int
        Number of bytes the payload occupies once encoded as UTF-8
        JSON.

    Raises
    ------
    TypeError
        If ``payload`` cannot be JSON-encoded (e.g. contains a
        non-serialisable value).  Output actions emitted by activities
        are expected to carry only JSON-friendly leaf types - strings,
        numbers, booleans, ``None``, and nested lists/dicts thereof -
        so a ``TypeError`` here indicates a programming bug at the
        emitting activity, not a runtime condition.
    """
    encoded = _encode_payload(payload)
    return len(encoded)


def _encode_payload(payload: Sequence[tuple[str, object]]) -> bytes:
    """Encode ``payload`` to bytes with the canonical, deterministic format.

    Centralising the encoder in a single private helper guarantees
    that :func:`measure_payload_bytes` and the offload step in
    :func:`redirect_oversized_payload` operate on **identical** byte
    strings - a correctness invariant that ensures two evaluations of
    the same input produce the same redirected action.
    """
    as_dict = _payload_to_dict(payload)
    # ``ensure_ascii=False`` keeps Turkish characters intact in the
    # offloaded artifact; ``sort_keys=True`` makes the encoding
    # canonical so determinism does not rely on dict insertion order
    # (which is technically deterministic in CPython 3.7+ but the
    # spec-level invariant is stronger when we sort explicitly).
    # ``separators=(",", ":")`` strips optional whitespace so the byte
    # count corresponds to the minimum representation - the cap then
    # measures actual content rather than formatting.
    return json.dumps(
        as_dict,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _build_summary(encoded: bytes) -> str:
    """Build a short, deterministic summary of an encoded payload.

    The summary is the first :data:`SUMMARY_TRUNCATE_CHARS` Unicode
    code points of the JSON text.  Code-point slicing (rather than
    byte slicing) avoids splitting a multi-byte UTF-8 sequence in
    half - important for Turkish payloads.  When the decoded text is
    shorter than the cut-off the whole text is returned verbatim
    (this branch is unreachable from the redirect path because we
    only summarise oversized payloads, but the helper is total to
    keep tests simple).
    """
    text = encoded.decode("utf-8", errors="replace")
    if len(text) <= SUMMARY_TRUNCATE_CHARS:
        return text
    return text[:SUMMARY_TRUNCATE_CHARS]


# ---------------------------------------------------------------------------
# Redirect helper
# ---------------------------------------------------------------------------


async def redirect_oversized_payload(
    action: OutputAction,
    workflow_id: str,
    idx: int,
    minio_callback: _MinioCallable,
) -> OutputAction:
    """Offload an oversized payload to MinIO and return the rewritten action.

    Behaviour:

    1. Compute the JSON-encoded byte length of ``action.payload``.
    2. If the length is **at or below** :data:`MAX_OUTPUT_BYTES`,
       return ``action`` **unchanged** (identity branch - the helper
       is a no-op for small payloads).
    3. Otherwise, build the MinIO object key from
       :data:`MINIO_KEY_TEMPLATE`, ``await`` ``minio_callback`` once
       with the encoded body, and return a **new** :class:`OutputAction`
       whose ``kind`` and ``severity`` are preserved and whose payload
       is replaced with::

           (
               ("summary", <first 256 chars of the encoded JSON>),
               ("minio_uri", <return value of minio_callback>),
               ("size_bytes", <original encoded byte length>),
           )

       The replacement payload preserves the
       :class:`OutputAction.payload` shape contract
       (``tuple[tuple[str, object], ...]``) so downstream consumers
       (including :mod:`temporal_shared.output_actions` once it
       lands) can apply the action without special-casing it.

    The summary is intentionally small enough to fit in the LLM
    context window without re-quoting the full payload - that is the
    "LLM context'ine sadece özet konur" half of the policy.

    Parameters
    ----------
    action:
        The action whose payload is subject to the cap.  The function
        does not mutate the input; the returned value is either
        ``action`` itself (identity) or a brand-new instance.
    workflow_id:
        Temporal workflow id, formatted via
        :mod:`temporal_shared.identifiers`.  Substituted into
        :data:`MINIO_KEY_TEMPLATE` to scope the offload key to this
        run.  Must be non-empty.
    idx:
        Position of the action within the activity's
        ``output_actions`` tuple.  Substituted into the key template
        so each oversized output gets a unique offload key even when
        a single activity emits several oversized payloads.  Must be
        a non-negative integer.
    minio_callback:
        Async callable matching :class:`MinioCallback`.  Called
        **at most once** per invocation - never when the payload is
        below the cap, exactly once when it is above.  The callback
        is responsible for the MinIO write itself (which is I/O and
        therefore must live outside this pure helper).

    Returns
    -------
    OutputAction
        Either the input ``action`` (identity) or a new action
        carrying the ``summary``/``minio_uri``/``size_bytes`` payload.

    Raises
    ------
    TypeError
        If ``action`` is not an :class:`OutputAction`, ``workflow_id``
        is not a string, ``idx`` is not an integer, or
        ``minio_callback`` is not callable.
    ValueError
        If ``workflow_id`` is empty or ``idx`` is negative.

    Notes
    -----
    The function is :func:`async def` (rather than wrapping the
    callback in a sync runner) because the natural caller -
    :class:`AgentRunnerWorkflow`'s ``apply()`` step - is itself
    awaiting the offload as part of its activity workflow.  Keeping
    the helper async means the caller can ``await`` it inline and
    Temporal's event-history tracks the boundary correctly.
    """
    # ----- argument validation -----
    if not isinstance(action, OutputAction):
        raise TypeError(
            f"action must be an OutputAction (got {type(action).__name__})"
        )
    if not isinstance(workflow_id, str):
        raise TypeError(
            "workflow_id must be a string "
            f"(got {type(workflow_id).__name__})"
        )
    if not workflow_id:
        raise ValueError("workflow_id must not be empty")
    if not isinstance(idx, int) or isinstance(idx, bool):
        # ``bool`` is a subclass of ``int`` in Python; reject it
        # explicitly so a stray ``True``/``False`` does not silently
        # collide with index 0 or 1 in the offload key.
        raise TypeError(f"idx must be an int (got {type(idx).__name__})")
    if idx < 0:
        raise ValueError(f"idx must be non-negative (got {idx})")
    if not callable(minio_callback):
        raise TypeError(
            "minio_callback must be an async callable "
            f"(got {type(minio_callback).__name__})"
        )

    # ----- cap check -----
    encoded = _encode_payload(action.payload)
    size_bytes = len(encoded)
    if size_bytes <= MAX_OUTPUT_BYTES:
        # Identity branch - small payloads pass through unchanged.
        # We deliberately return the **same** instance so callers
        # that compare with ``is`` see the no-op semantics.
        return action

    # ----- offload branch -----
    key = MINIO_KEY_TEMPLATE.format(workflow_id=workflow_id, idx=idx)
    minio_uri = await minio_callback(key=key, body=encoded)

    # The replacement payload preserves the OutputAction.payload
    # contract (immutable tuple-of-pairs) so downstream consumers
    # never see a dict here.  Order is fixed for determinism: the
    # property test asserts that the same input yields the same
    # tuple twice in a row.
    summary = _build_summary(encoded)
    replacement_payload: tuple[tuple[str, object], ...] = (
        ("summary", summary),
        ("minio_uri", minio_uri),
        ("size_bytes", size_bytes),
    )

    # ``dataclasses.replace`` would also work, but we construct a
    # fresh frozen dataclass to keep the dependency surface narrow
    # and to match the style used by other temporal_shared formatters.
    return OutputAction(
        kind=action.kind,
        severity=action.severity,
        payload=replacement_payload,
    )


# ---------------------------------------------------------------------------
# Final Jira comment formatter
# ---------------------------------------------------------------------------


def format_final_jira_comment(
    critical_done: Iterable[str],
    best_effort_failed: Iterable[tuple[str, str]],
) -> str:
    """Format the final Jira comment in Turkish.

    Shape::

         Tamamlanan kritik adımlar: a, b, c
         Başarısız yan-aksiyonlar: x (sebep1), y (sebep2)

    Behaviour:

    * When ``critical_done`` is empty, the ```` line is omitted.
    * When ``best_effort_failed`` is empty, the ```` line is omitted.
    * When **both** are empty, the function returns the empty string
      (the caller is expected to suppress the final comment in that
      case rather than post a blank update).
    * When both are populated, the ```` line precedes the ````
      line and the two are separated by a single newline.  The order
      is fixed by the comment shape.

    The label prefixes are exposed via
    :data:`FINAL_COMMENT_CRITICAL_PREFIX` and
    :data:`FINAL_COMMENT_BEST_EFFORT_PREFIX` so tests can pin both the
    line shape and the labels themselves.

    Parameters
    ----------
    critical_done:
        Iterable of human-readable names of critical actions that
        completed successfully.  Items are joined with ``", "``
        (comma-space), matching the Turkish-prose convention used
        elsewhere in the platform.
        Empty strings are filtered out (defensive: an empty name
        contributes a stray comma which would render confusingly in
        the Jira comment).
    best_effort_failed:
        Iterable of ``(name, reason)`` 2-tuples describing best-effort
        actions that failed.  Each entry renders as ``name (reason)``
        and the entries are joined with ``", "``.  Empty names are
        filtered out for the same reason as above; an empty reason is
        kept (renders as ``name ()``) because a missing reason can
        legitimately mean "the activity returned without an error
        message" and the placeholder is more honest than dropping the
        action silently.

    Returns
    -------
    str
        The formatted comment.  Never starts or ends with a newline.

    Raises
    ------
    TypeError
        If ``critical_done`` is not iterable of strings, or
        ``best_effort_failed`` is not iterable of 2-tuples of
        strings.

    Examples
    --------
    Empty inputs produce an empty string (caller suppresses the comment):

    >>> format_final_jira_comment([], [])
    ''

    Only critical steps populated  only the  line is emitted:

    >>> format_final_jira_comment(["a", "b"], []).startswith(
    ...     FINAL_COMMENT_CRITICAL_PREFIX
    ... )
    True

    Only best-effort failures populated  only the  line is emitted:

    >>> result = format_final_jira_comment([], [("x", "timeout")])
    >>> result.startswith(FINAL_COMMENT_BEST_EFFORT_PREFIX)
    True
    >>> result.endswith("x (timeout)")
    True

    Both populated  both lines,  first, separated by a single
    newline:

    >>> result = format_final_jira_comment(
    ...     ["a"], [("x", "timeout"), ("y", "rate_limited")]
    ... )
    >>> "\n" in result
    True
    >>> result.split("\n")[0].startswith(FINAL_COMMENT_CRITICAL_PREFIX)
    True
    >>> result.split("\n")[1].startswith(FINAL_COMMENT_BEST_EFFORT_PREFIX)
    True
    """
    # Materialise the iterables once so we can iterate twice
    # (length check + render) without exhausting a generator.
    critical_list = list(critical_done)
    failed_list = list(best_effort_failed)

    # ----- argument validation -----
    for i, name in enumerate(critical_list):
        if not isinstance(name, str):
            raise TypeError(
                f"critical_done[{i}] must be a string "
                f"(got {type(name).__name__})"
            )
    for i, item in enumerate(failed_list):
        # Accept any 2-element sequence (tuple, list, etc.); reject
        # anything else so a stray dict or scalar fails fast.
        if not (isinstance(item, tuple) and len(item) == 2):
            raise TypeError(
                f"best_effort_failed[{i}] must be a 2-tuple of "
                f"(name, reason); got {item!r}"
            )
        name, reason = item
        if not isinstance(name, str):
            raise TypeError(
                f"best_effort_failed[{i}].name must be a string "
                f"(got {type(name).__name__})"
            )
        if not isinstance(reason, str):
            raise TypeError(
                f"best_effort_failed[{i}].reason must be a string "
                f"(got {type(reason).__name__})"
            )

    # ----- filter empty names (defensive) -----
    critical_filtered = [name for name in critical_list if name]
    failed_filtered = [
        (name, reason) for name, reason in failed_list if name
    ]

    lines: list[str] = []
    if critical_filtered:
        lines.append(
            FINAL_COMMENT_CRITICAL_PREFIX
            + ", ".join(critical_filtered)
        )
    if failed_filtered:
        rendered = ", ".join(
            f"{name} ({reason})" for name, reason in failed_filtered
        )
        lines.append(FINAL_COMMENT_BEST_EFFORT_PREFIX + rendered)

    return "\n".join(lines)
