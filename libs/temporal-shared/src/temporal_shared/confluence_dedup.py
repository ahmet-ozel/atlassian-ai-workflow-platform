"""Pure skip-decision predicates for the ``confluence_doc_update`` flow.

This module hosts three **pure** decision helpers used by the
``AgentRunnerWorkflow`` before it invokes a ``confluence_update_page``
MCP tool call:

* :func:`should_skip_section_update` - returns a :class:`SkipDecision`
  reporting whether the four-tuple
  ``(workflow_id, page_id, section_path, content_hash)`` is already
  present in an in-memory hash table.
* :func:`should_skip_overwrite` - returns a :class:`SkipDecision`
  reporting whether a Confluence page was edited in the recent
  freshness window by a non-bot user.
* :func:`is_probe_page` - predicate matching the canonical
  ``_AI_PROBE_*`` sentinel title format produced by the foundation
  ``ProbeRunner``.

Why a separate module?
----------------------
``temporal_shared.confluence`` owns the **string composition**
contract for Confluence page titles and the provenance footer.  This
module owns the **skip-decision** contract for the update path.  The
two concerns share a domain (Confluence) but have unrelated input /
output shapes, so they are split into adjacent modules to keep each
file's surface area narrow and the docstrings focused.  Both modules
are re-exported from :mod:`temporal_shared` so call sites can import
either set of helpers without learning the internal layout.

Purity contract
---------------
Every public function in this module is **pure**:

* No I/O - the caller provides the hash table / freshness inputs as
  arguments, and the audit emission is the caller's responsibility
  (the :class:`SkipDecision` carries the audit action name so the
  caller knows what to log).
* No clocks - ``now`` and ``last_edit_at`` are passed explicitly.  A
  Temporal workflow caller sources ``now`` from ``workflow.now()``
  (replay-safe); a non-workflow caller may source it from
  ``datetime.now(tz=timezone.utc)``.  The helpers themselves never
  call a clock.
* No randomness, no UUIDs, no globals.

This makes the helpers safe to call directly from inside Temporal
workflow code
without tripping the AST-based replay-determinism property test
(``platform/tests/property/test_workflow_determinism_static.py``).

Returning :class:`SkipDecision` instead of a bare ``bool``
----------------------------------------------------------

The skip predicate alone is not enough for the call site: when a write
is skipped, the caller must also emit a specific audit row
(``confluence_section_dedup_skip`` or
``confluence_overwrite_protected``). Returning
:class:`SkipDecision` keeps the audit-action name colocated with the
decision so the call-site cannot drift out of sync with the
requirement text:

    decision = should_skip_section_update(...)
    if decision.skip:
        await audit.write(action=decision.audit_event, ...)
        return  # skip the Confluence update
    # proceed with the write

The audit emission still lives in the caller (an
:class:`audit_logger.AuditLogger` write is I/O); this module never
imports :mod:`audit_logger`.

"""

from __future__ import annotations

from collections.abc import Container, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

__all__ = [
    # decision type
    "SkipDecision",
    # audit action constants
    "AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP",
    "AUDIT_CONFLUENCE_OVERWRITE_PROTECTED",
    # config constants
    "DEFAULT_OVERWRITE_FRESHNESS",
    "PROBE_PAGE_TITLE_PREFIX",
    # predicates
    "should_skip_section_update",
    "should_skip_overwrite",
    "is_probe_page",
]


# ---------------------------------------------------------------------------
# Audit action constants
# ---------------------------------------------------------------------------
#
# The audit action strings are exposed as module constants (rather than embedding
# the literals inside each helper) so the call site can also reference
# them when matching audit rows in tests, and so a typo in the
# audit emission can be caught by the type checker.

#: Audit action emitted when :func:`should_skip_section_update` returns
#: ``skip=True``.
AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP: Final[str] = "confluence_section_dedup_skip"

#: Audit action emitted when :func:`should_skip_overwrite` returns
#: ``skip=True``.
AUDIT_CONFLUENCE_OVERWRITE_PROTECTED: Final[str] = "confluence_overwrite_protected"


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

#: Default freshness window for the overwrite-protection check
#: A page edited by a non-bot user within this
#: window blocks the bot's update.  Pinned to 5 minutes per the
#: requirement; exposed as a constant so tests can assert the default
#: without re-reading the requirement text.
DEFAULT_OVERWRITE_FRESHNESS: Final[timedelta] = timedelta(minutes=5)

#: Title prefix used by the foundation ``ProbeRunner`` for write-probe
#: artifacts. Mirrors
#: :data:`automation_service.probe.PROBE_ARTIFACT_PREFIX` - duplicated
#: here as a string literal so :mod:`temporal_shared` does not depend
#: on :mod:`automation_service` (the dependency direction is the
#: reverse: services consume libs).  An integration test asserts the
#: two constants stay in sync (see tests/test_confluence_dedup.py).
PROBE_PAGE_TITLE_PREFIX: Final[str] = "_AI_PROBE_"


# ---------------------------------------------------------------------------
# SkipDecision - return type for the predicates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipDecision:
    """Structured result of a skip-decision predicate.

    Attributes
    ----------
    skip:
        ``True`` iff the caller should **skip** the Confluence update.
    audit_event:
        Audit action name to emit when ``skip=True``.  Set to one of
        :data:`AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP` /
        :data:`AUDIT_CONFLUENCE_OVERWRITE_PROTECTED` when the helper
        decides to skip; ``None`` when ``skip=False`` (no audit
        emission required for the proceed branch - successful writes
        are audited by the activity itself).

    The two attributes are kept together as a frozen dataclass so the
    call site cannot accidentally emit an audit row while proceeding
    with a write (or vice-versa): ``audit_event`` is ``None`` exactly
    when ``skip`` is ``False``.  An explicit ``__post_init__`` check
    enforces this invariant at construction time.
    """

    skip: bool
    audit_event: str | None

    def __post_init__(self) -> None:
        if self.skip and not self.audit_event:
            raise ValueError(
                "SkipDecision(skip=True) requires a non-empty audit_event "
                "(callers rely on this to know which audit row to write)."
            )
        if not self.skip and self.audit_event is not None:
            raise ValueError(
                "SkipDecision(skip=False) must not carry an audit_event "
                "(the proceed branch is audited by the activity itself)."
            )


#: Singleton "proceed" decision - the helpers return this exact
#: instance when the predicate fires negative.  Reusing the singleton
#: keeps the hot path allocation-free and lets callers rely on the
#: ``is`` identity for branch-coverage assertions in tests.
_PROCEED: Final[SkipDecision] = SkipDecision(skip=False, audit_event=None)


# ---------------------------------------------------------------------------
# should_skip_section_update
# ---------------------------------------------------------------------------


def should_skip_section_update(
    workflow_id: str,
    page_id: str,
    section_path: str,
    content_hash: str,
    hash_table: Container[tuple[str, str, str, str]],
) -> SkipDecision:
    """Decide whether to skip a Confluence section update by content hash.

    Mirrors the natural idempotency key of the
    ``automation.confluence_section_hashes`` Postgres table
    (``(workflow_id, page_id, section_path, content_hash)``) but
    operates on a caller-provided in-memory ``hash_table`` so the
    decision can be made replay-safely from within
    :class:`AgentRunnerWorkflow` (workflow state, not DB).

    The DB-backed equivalent lives in
    :class:`automation_service.confluence_section_hashes.ConfluenceSectionHashRepo`
    - that helper performs an idempotent ``INSERT ... ON CONFLICT``.
    This pure helper is for the workflow path: the workflow holds
    the hash table in :class:`temporal_shared.IterationState`-adjacent
    state, passes it
    in, and the DB layer is consulted by an activity that uses this
    same predicate as part of its decision.

    Parameters
    ----------
    workflow_id:
        Workflow id of the AgentRunnerWorkflow that owns the update -
        formatted via :mod:`temporal_shared.identifiers`.  Encodes the
        department boundary.
    page_id:
        Confluence page id (string - Confluence ids are opaque
        tokens, not integers).
    section_path:
        Slash-delimited path of the section being updated
        (e.g. ``"§3.1/Implementation"``).  The Confluence update
        activity computes this from the page tree.
    content_hash:
        sha256 hex digest of the rendered section body.  The caller
        computes the digest before invoking this helper so equivalent
        payloads with different formatting hash identically.
    hash_table:
        Container of four-tuples ``(workflow_id, page_id,
        section_path, content_hash)`` already written for this
        workflow.  The function only requires ``__contains__``
        semantics, so a :class:`set`, :class:`frozenset`, or any
        :class:`~collections.abc.Container` works.  Passing a list
        also works but yields O(n) lookup.

    Returns
    -------
    SkipDecision
        ``SkipDecision(skip=True,
        audit_event=AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP)`` when the
        four-tuple is already in ``hash_table`` (skip the write and
        emit the audit row).
        ``SkipDecision(skip=False, audit_event=None)`` when the
        four-tuple is new (proceed with the Confluence update; the
        caller is responsible for inserting the four-tuple into the
        hash table after a successful write so subsequent iterations
        deduplicate against it).

    Raises
    ------
    TypeError
        If any of the five string arguments is not a :class:`str`,
        or if ``hash_table`` does not implement ``__contains__``.
    ValueError
        If any of the four key components is empty.

    Examples
    --------
    >>> should_skip_section_update(
    ...     "automation-jira-PAY-1", "p1", "§1", "abc", set()
    ... )
    SkipDecision(skip=False, audit_event=None)
    >>> should_skip_section_update(
    ...     "automation-jira-PAY-1",
    ...     "p1",
    ...     "§1",
    ...     "abc",
    ...     {("automation-jira-PAY-1", "p1", "§1", "abc")},
    ... ).skip
    True
    """
    _require_non_empty_str("workflow_id", workflow_id)
    _require_non_empty_str("page_id", page_id)
    _require_non_empty_str("section_path", section_path)
    _require_non_empty_str("content_hash", content_hash)

    # ``Container.__contains__`` is the only operation we need; we
    # explicitly do **not** require a ``Mapping`` or ``Set`` so the
    # caller can pass a frozenset for hot paths, a set for the
    # workflow's mutable state, or even a tuple for tests.
    if not hasattr(hash_table, "__contains__"):
        raise TypeError(
            "hash_table must support `in` (Container protocol); "
            f"got {type(hash_table).__name__}"
        )

    key = (workflow_id, page_id, section_path, content_hash)
    if key in hash_table:
        return SkipDecision(
            skip=True,
            audit_event=AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
        )
    return _PROCEED


# ---------------------------------------------------------------------------
# should_skip_overwrite
# ---------------------------------------------------------------------------


def should_skip_overwrite(
    last_editor_account_id: str | None,
    last_edit_at: datetime | None,
    now: datetime,
    bot_ids: Iterable[str],
    freshness: timedelta = DEFAULT_OVERWRITE_FRESHNESS,
) -> SkipDecision:
    """Decide whether a recent non-bot edit should block the bot's update.

    Returns a "skip" decision when **all** of the following hold (per
    the overwrite-protection rule):

    * ``last_editor_account_id`` is **not** in the bot account-id set
      (i.e. a human edited the page since the last bot write); and
    * ``now - last_edit_at < freshness`` (the human edit is fresh
      enough to count as ongoing collaboration).

    When either side is missing - no recorded editor, no recorded
    timestamp, or the timestamp is older than the freshness window -
    the function returns the proceed decision and the bot's update
    goes ahead.  Bot-on-bot edits never block (the bot is allowed to
    update its own page during an iteration).

    Parameters
    ----------
    last_editor_account_id:
        Atlassian ``account_id`` of the last user that edited the
        page.  ``None`` when the page has never been edited (a
        freshly created page) - that case never blocks.
    last_edit_at:
        UTC timestamp of the last edit.  ``None`` when no edit has
        ever been recorded - that case never blocks.  Naive
        :class:`datetime` instances are rejected so the freshness
        comparison cannot silently mix timezones.
    now:
        Current time supplied by the caller.  Must be timezone-aware
        and on the same wall-clock as ``last_edit_at`` (UTC by
        convention).  A workflow caller passes ``workflow.now()``.
    bot_ids:
        Iterable of bot ``account_id`` values for the department.
        Typically loaded from ``departments.json`` and frozen into a
        :class:`frozenset` before this call.  An empty iterable is
        valid (no bot accounts configured) and means every recent
        edit is treated as a non-bot edit.
    freshness:
        Window during which a non-bot edit blocks the bot update.
        Defaults to :data:`DEFAULT_OVERWRITE_FRESHNESS` (5 minutes,
        by default). Must be **positive** - a zero or negative window
        would either always-block or never-block, both of which are
        contract violations.

    Returns
    -------
    SkipDecision
        ``SkipDecision(skip=True,
        audit_event=AUDIT_CONFLUENCE_OVERWRITE_PROTECTED)`` when the
        page was recently edited by a non-bot user (skip the bot
        update; the caller emits a needs_info Jira comment and the
        audit row).
        ``SkipDecision(skip=False, audit_event=None)`` otherwise
        (proceed with the bot update).

    Raises
    ------
    TypeError
        If ``now`` is not a tz-aware :class:`datetime`, if
        ``last_edit_at`` is provided but is not a tz-aware
        :class:`datetime`, or if ``freshness`` is not a
        :class:`timedelta`.
    ValueError
        If ``freshness`` is not strictly positive.

    Examples
    --------
    >>> from datetime import datetime, timedelta, timezone
    >>> now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    >>> # Recent human edit  skip.
    >>> should_skip_overwrite(
    ...     "human-1",
    ...     now - timedelta(minutes=2),
    ...     now,
    ...     {"bot-acct"},
    ... ).skip
    True
    >>> # Recent bot edit  proceed.
    >>> should_skip_overwrite(
    ...     "bot-acct",
    ...     now - timedelta(minutes=2),
    ...     now,
    ...     {"bot-acct"},
    ... ).skip
    False
    >>> # Stale human edit  proceed.
    >>> should_skip_overwrite(
    ...     "human-1",
    ...     now - timedelta(minutes=10),
    ...     now,
    ...     {"bot-acct"},
    ... ).skip
    False
    """
    if not isinstance(freshness, timedelta):
        raise TypeError(
            f"freshness must be a datetime.timedelta (not "
            f"{type(freshness).__name__})"
        )
    if freshness <= timedelta(0):
        raise ValueError(
            f"freshness must be strictly positive (got {freshness!r})"
        )

    if not isinstance(now, datetime):
        raise TypeError(
            f"now must be a datetime.datetime (not {type(now).__name__})"
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise TypeError(
            "now must be timezone-aware (got a naive datetime); pass a "
            "UTC datetime such as datetime.now(tz=timezone.utc) or "
            "workflow.now()"
        )

    # Missing edit metadata never blocks: a freshly-created page or a
    # page whose history is unavailable should not be treated as a
    # "recent human edit".
    if last_editor_account_id is None or last_edit_at is None:
        return _PROCEED

    if not isinstance(last_edit_at, datetime):
        raise TypeError(
            "last_edit_at must be a datetime.datetime or None (not "
            f"{type(last_edit_at).__name__})"
        )
    if last_edit_at.tzinfo is None or last_edit_at.utcoffset() is None:
        raise TypeError(
            "last_edit_at must be timezone-aware (got a naive datetime)"
        )

    # Bot-on-bot edits never block.  We freeze ``bot_ids`` once so a
    # generator argument (which would otherwise be exhausted by the
    # ``in`` check) still works, and so a caller passing a list is not
    # quadratic in the size of the iterable.
    bot_id_set: frozenset[str] = frozenset(bot_ids)
    if last_editor_account_id in bot_id_set:
        return _PROCEED

    # Normalise both timestamps to UTC so the subtraction does not
    # depend on the caller's tz.  ``datetime`` arithmetic with
    # tz-aware operands is already UTC-correct, but performing the
    # conversion explicitly keeps the comparison semantics legible.
    delta = now.astimezone(timezone.utc) - last_edit_at.astimezone(timezone.utc)

    # Future-dated edits are treated as "not recent enough to block":
    # if the editor's clock is ahead of the workflow's clock the
    # delta would be negative, which the strict-less-than check
    # below correctly resolves to ``False`` (no skip).  This matches
    # Only edits in the past 5 minutes
    # block the bot update.
    if delta < freshness and delta >= timedelta(0):
        return SkipDecision(
            skip=True,
            audit_event=AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
        )
    return _PROCEED


# ---------------------------------------------------------------------------
# is_probe_page
# ---------------------------------------------------------------------------


def is_probe_page(page_title: str) -> bool:
    """Return whether *page_title* matches the foundation probe sentinel.

    The canonical probe title format
    ``_AI_PROBE_<unix_ts>_DELETE_ME`` is produced by
    :class:`automation_service.probe.ProbeRunner`.  The
    ``confluence_doc_update`` flow must filter these pages out of its
    update queue so the bot never overwrites or amends a
    write-probe artifact left behind by a credential probe.

    The check is deliberately **prefix-only** so it stays robust
    against historical formats and human-edited variants - the same
    looseness used by
    :func:`automation_service.probe.is_probe_artifact_title`.  The two
    helpers are kept in sync by an integration test that imports both
    and asserts they agree on a corpus of titles.

    Parameters
    ----------
    page_title:
        Confluence page title.  ``None`` and non-string inputs return
        ``False`` (a page with no title is by definition not a probe
        sentinel).

    Returns
    -------
    bool
        ``True`` iff ``page_title`` starts with the
        :data:`PROBE_PAGE_TITLE_PREFIX` literal ``"_AI_PROBE_"``.

    Examples
    --------
    >>> is_probe_page("_AI_PROBE_1700000000_DELETE_ME")
    True
    >>> is_probe_page("_AI_PROBE_legacy")  # legacy / human-edited
    True
    >>> is_probe_page("Quarterly Review")
    False
    >>> is_probe_page("")
    False
    >>> is_probe_page(None)  # type: ignore[arg-type]
    False
    """
    return isinstance(page_title, str) and page_title.startswith(
        PROBE_PAGE_TITLE_PREFIX
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(name: str, value: object) -> None:
    """Validate that *value* is a non-empty :class:`str`.

    Centralised so all four key components of
    :func:`should_skip_section_update` reject empty / wrong-typed
    inputs with a uniform error message.  Empty strings are rejected
    because the dedup key would otherwise collide across unrelated
    pages / sections.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"{name} must be a string (got {type(value).__name__})"
        )
    if not value:
        raise ValueError(f"{name} must not be empty")
