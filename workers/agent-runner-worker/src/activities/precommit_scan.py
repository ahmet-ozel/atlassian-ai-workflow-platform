"""Pre-commit static analysis + secret scan activity (T2 — P0).

Implements :func:`precommit_scanner` — the **commit-gating** activity
that ``AgentRunnerWorkflow`` invokes immediately before every
``bitbucket_commit_patch`` call.

Contract
--------

The activity takes a unified-diff string and returns a frozen
:class:`ScanResult` with two fields:

* ``decision: Literal["pass", "block"]`` — ``"block"`` when the diff
  contains at least one matched secret pattern; ``"pass"`` otherwise.
* ``matched_patterns: tuple[str, ...]`` — the **stable, sorted** tuple
  of pattern *names* (not the secret values) that fired against the
  diff. Names are stable identifiers (``"aws_access_key"``,
  ``"atlassian_api_token"``, ...) chosen for audit / dashboard use.

When ``decision == "block"`` the activity emits a single audit event
``precommit_secret_leak_blocked`` via the foundation
:mod:`audit_logger`. The audit
``payload`` carries only the matched **pattern names** — never the
secret values themselves; secret values are masked at the audit
boundary so the audit log does not become a secondary leak surface.
The audit emission is best-effort: a broken audit pipeline must
**not** suppress the ``"block"`` decision (the workflow still fails
the commit).

Determinism
-----------

For any ``diff`` string, ``precommit_scanner(diff)`` is a pure
function of its input: same diff → same :class:`ScanResult` (same
``decision`` and same ``matched_patterns`` tuple in the same order).
This is enforced by:

* the regex pattern table is a frozen module-level constant,
* the matched-patterns tuple is built by **iterating the table in
  insertion order** and ``sorted()``-ed by pattern name — both of
  which are deterministic,
* the activity never reads the wall clock, environment, or any other
  source of non-determinism on the scanning path.

Audit emission is a *side-effect* layered on top of the deterministic
scan; it does not affect the return value, so the determinism property
holds whether or not an :class:`audit_logger.AuditLogger` is wired in.

Implementation choice — pure-Python regex fallback
--------------------------------------------------

``gitleaks`` (binary) and ``bandit`` (Python) are optional detail-scan
tools. Those binaries are deployment artefacts of the
``agent-runner-worker`` container image and are not always available in
unit / property test environments. The activity is implemented as a
**pure-Python regex sweep** over the documented secret pattern list:

* ``aws_access_key``       — ``AKIA`` followed by 16 ``[0-9A-Z]``
* ``atlassian_api_token``  — ``ATATT3x`` followed by ``[A-Za-z0-9_-]+``
* ``bearer_token``         — ``Bearer`` + whitespace + token chars
* ``generic_password``     — ``password = "..."`` (case-insensitive,
  single or double quoted, non-empty value)

These four cover the critical scope for AWS keys, Atlassian API tokens,
Bearer headers, and hard-coded passwords. The
``gitleaks`` / ``bandit`` binaries can be layered on top as additional
findings sources without changing the :class:`ScanResult` contract —
their output normalises to additional entries in
``matched_patterns``. That extension is out of scope here.

Why no I/O on the scan path?
----------------------------

``test_precommit_scanner.py`` drives the activity with
hypothesis-generated diffs and asserts ``precommit_scanner(diff) ==
precommit_scanner(diff)`` across runs. Subprocess invocation against
``gitleaks`` would introduce shell environment, working directory, and
binary version into the determinism equation; the pure-Python
fallback keeps the test self-contained and the activity body
trivially replay-safe in Temporal terms.

Usage
-----

``AgentRunnerWorkflow`` `code_change_*` flows call this activity before
``bitbucket_commit_patch``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, Mapping, Pattern

from temporalio import activity

__all__ = [
    "ScanResult",
    "SECRET_PATTERNS",
    "PRECOMMIT_AUDIT_ACTION",
    "precommit_scanner",
    "scan_diff",
    "set_audit_logger",
    "get_audit_logger",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit constants
# ---------------------------------------------------------------------------


#: Audit ``action`` string emitted on a ``"block"`` decision. This is
#: also the value the Streamlit *Security > Secret Leak Olayları*
#: dashboard greps for.
PRECOMMIT_AUDIT_ACTION: Final[str] = "precommit_secret_leak_blocked"


# ---------------------------------------------------------------------------
# Pattern table (single source of truth)
# ---------------------------------------------------------------------------


def _compile_patterns() -> Mapping[str, Pattern[str]]:
    """Compile the secret pattern table once at import time.

    The mapping is intentionally an ordinary :class:`dict` so the
    insertion order is preserved (Python 3.7+); callers wanting an
    immutable view should consume :data:`SECRET_PATTERNS` rather than
    this private helper. Keys are stable pattern *names* used in
    audit events and ``ScanResult.matched_patterns``; values are
    pre-compiled :class:`re.Pattern` objects.
    """

    return {
        # AWS access key id — 20-char fixed prefix + 16 [0-9A-Z].
        # https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_identifiers.html
        "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
        # Atlassian API token (cloud) — fixed ``ATATT3x`` prefix + base64url-ish body.
        # https://id.atlassian.com/manage-profile/security/api-tokens
        "atlassian_api_token": re.compile(r"ATATT3x[A-Za-z0-9_\-]+"),
        # Bearer auth header — ``Bearer`` + whitespace + token chars.
        # The ``\b`` anchors prevent matching inside an unrelated word
        # like ``MyBearerWrapper``.
        "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+"),
        # Generic ``password = "..."`` assignment with a non-empty
        # quoted value. ``(?i)`` is the inline case-insensitive flag.
        # Both straight quote variants are handled; the negated class
        # ensures the value is non-empty.
        "generic_password": re.compile(
            r"""(?i)password\s*=\s*['"][^'"]+['"]"""
        ),
    }


#: Frozen view of the compiled pattern table. Iteration order is the
#: insertion order; :func:`scan_diff` further sorts the matched names
#: alphabetically so the resulting tuple is invariant under reordering
#: of this table.
SECRET_PATTERNS: Final[Mapping[str, Pattern[str]]] = _compile_patterns()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    """Outcome of a :func:`precommit_scanner` invocation.

    Attributes
    ----------
    decision:
        ``"pass"`` when no secret pattern matched the diff; ``"block"``
        when at least one pattern matched. The workflow MUST treat
        ``"block"`` as a hard failure of the commit step.
    matched_patterns:
        Stable, alphabetically-sorted tuple of pattern *names* (not
        secret values) that fired against the diff. Empty tuple iff
        ``decision == "pass"``.

    The dataclass is :class:`frozen <dataclasses.dataclass>` so the
    return value can be safely cached / compared by Hypothesis-driven
    property tests and so the audit payload references
    a value that cannot be mutated after construction.
    """

    decision: Literal["pass", "block"]
    matched_patterns: tuple[str, ...]


# ---------------------------------------------------------------------------
# Audit logger registry (mirrors the credential resolver pattern)
# ---------------------------------------------------------------------------


_audit_logger: Any | None = None


def set_audit_logger(logger: Any) -> None:
    """Register the shared :class:`audit_logger.AuditLogger` for activities.

    Called once during worker startup (in ``main.py``) after the
    Postgres / audit pipeline is wired up. Activities that need to
    emit audit events read the registry via :func:`get_audit_logger`.

    Parameters
    ----------
    logger:
        An :class:`audit_logger.AuditLogger` instance (or duck-typed
        equivalent exposing ``async write(event)``). Tests can pass a
        capturing fake whose ``write`` appends to a list.
    """

    global _audit_logger  # noqa: PLW0603 - module-level singleton on purpose
    _audit_logger = logger


def get_audit_logger() -> Any | None:
    """Retrieve the shared audit logger, or ``None`` if unset.

    Unlike :func:`get_credential_resolver` (which raises when missing
    because credential lookup is mandatory), this helper returns
    ``None`` so the scanning path stays a pure deterministic function
    of the input diff: a worker that has not wired audit yet still
    produces a correct :class:`ScanResult`, just without the audit
    write side-effect. Audit emission is a consequence of ``"block"``,
    not a precondition for it.
    """

    return _audit_logger


# ---------------------------------------------------------------------------
# Scanning core (pure)
# ---------------------------------------------------------------------------


def scan_diff(diff: str) -> ScanResult:
    """Run the deterministic regex sweep over *diff* and return the result.

    This is the **pure** core of the activity — same input, same
    output, no side effects. ``test_precommit_scanner``
    asserts:

    1. clean diff → ``ScanResult(decision="pass", matched_patterns=())``,
    2. each secret pattern → ``decision="block"`` with the matching
       name in ``matched_patterns``,
    3. ``scan_diff(d) == scan_diff(d)`` for every ``d`` (determinism),
    4. multiple distinct patterns in the same diff → all matched
       names appear in ``matched_patterns`` with no duplicates.

    Implementation notes
    --------------------
    * We iterate :data:`SECRET_PATTERNS` in **insertion order** but
      sort the matched names before returning. Sorting makes the
      tuple invariant under future reordering of the table — the
      test compares tuples for equality, so the sort keeps
      the test stable across pattern-list edits.
    * Each pattern is checked with :meth:`re.Pattern.search`; we do
      not use :meth:`re.findall` because we only need to know
      *whether* a pattern fired, not how many times. This keeps the
      complexity O(n) in the diff length per pattern and avoids
      unnecessary allocations.
    * ``str`` is the only accepted input type. A non-string argument
      raises :class:`TypeError` so the workflow surface fails fast
      rather than silently treating ``None`` as "no secrets".

    Parameters
    ----------
    diff:
        The unified-diff text the workflow is about to commit. Empty
        string is allowed and results in ``decision="pass"``.

    Returns
    -------
    ScanResult
        Frozen dataclass carrying the gate decision and the names of
        any patterns that matched. ``matched_patterns`` is sorted
        alphabetically for determinism.

    Raises
    ------
    TypeError
        If ``diff`` is not a :class:`str`.
    """

    if not isinstance(diff, str):
        raise TypeError(
            f"precommit_scanner expects str, got {type(diff).__name__}"
        )

    # Iterate the table once, collecting names of patterns that fired.
    # Using a set avoids duplicates if a pattern matches multiple
    # locations, and feeding the set into ``sorted()`` produces the
    # stable, deterministic tuple required by the scanner contract.
    matched: set[str] = set()
    for name, pattern in SECRET_PATTERNS.items():
        if pattern.search(diff) is not None:
            matched.add(name)

    if not matched:
        return ScanResult(decision="pass", matched_patterns=())

    return ScanResult(
        decision="block",
        matched_patterns=tuple(sorted(matched)),
    )


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _build_audit_event(
    *,
    matched_patterns: tuple[str, ...],
    dept_id: str | None,
    workflow_id: str | None,
    issue_key: str | None,
) -> Any:
    """Construct the ``precommit_secret_leak_blocked`` :class:`AuditEvent`.

    Lazy-imported so the activity module can be loaded in environments
    where ``audit_logger`` is not on ``sys.path`` (eg. a worker that
    runs without the foundation libs available — the deterministic
    scan still works, the audit just becomes a no-op).

    The event ``payload`` carries:

    * ``matched_patterns`` — the same tuple as ``ScanResult``, so the
      audit row and the activity return value agree by construction,
    * ``workflow_id`` and ``issue_key`` — best-effort context for
      operator triage on the *Security > Secret Leak Olayları*
      dashboard.

    Crucially, ``payload`` does **not** include the matched secret
    values themselves. Audit rows feed downstream log storage; we do
    not turn the audit pipeline into a secondary leak surface.
    """

    from audit_logger import AuditEvent  # local import — see docstring

    return AuditEvent(
        actor_id="bot.agent-runner",
        actor_role="system",
        dept_id=dept_id,
        action=PRECOMMIT_AUDIT_ACTION,
        resource=f"workflow:{workflow_id or 'unknown'}",
        result="denied",
        timestamp=datetime.now(timezone.utc),
        payload={
            "matched_patterns": list(matched_patterns),
            "issue_key": issue_key,
        },
    )


async def _emit_block_audit(
    *,
    matched_patterns: tuple[str, ...],
    dept_id: str | None,
    workflow_id: str | None,
    issue_key: str | None,
) -> None:
    """Emit the block audit; swallow errors so the gate decision survives.

    A broken audit pipeline must not turn a ``"block"`` outcome into a
    silent failure: the workflow still gets the ``ScanResult`` and
    must still fail the commit. We log the audit-write failure so the
    operator has a trail, and move on.
    """

    audit_logger = get_audit_logger()
    if audit_logger is None:
        # Worker has not wired audit yet (eg. unit / property test
        # environment). The determinism assertion holds regardless of
        # audit wiring.
        return

    try:
        event = _build_audit_event(
            matched_patterns=matched_patterns,
            dept_id=dept_id,
            workflow_id=workflow_id,
            issue_key=issue_key,
        )
    except Exception:  # noqa: BLE001 - missing audit_logger lib, etc.
        _LOG.warning(
            "precommit_scanner: failed to construct audit event",
            exc_info=True,
        )
        return

    try:
        await audit_logger.write(event)
    except Exception:  # noqa: BLE001 - best-effort
        _LOG.warning(
            "precommit_scanner: audit write failed",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Temporal activity
# ---------------------------------------------------------------------------


def _resolve_workflow_context() -> tuple[str | None, str | None]:
    """Best-effort lookup of ``workflow_id`` and ``dept_id`` from the runtime.

    When called inside a Temporal worker :func:`activity.info` returns
    the live context; outside (eg. unit tests) it raises and we
    return ``(None, None)``. The audit emission tolerates ``None`` so
    the activity stays callable from both worlds.
    """

    try:
        info = activity.info()
    except Exception:  # noqa: BLE001 - not running under Temporal
        return None, None

    workflow_id = info.workflow_id or None
    # The dept-id convention follows the rest of the worker:
    # ``automation-jira-{PROJECT}-{NUM}`` / ``automation-bb-{REPO}-pr-{PR}``
    # do not encode dept_id directly, so we leave it as ``None`` here
    # and let the workflow caller pass it via the activity argument
    # if needed. The current task signature is ``precommit_scanner(diff)``
    # only, so dept_id stays ``None`` and the audit row carries
    # ``dept_id=NULL`` — which is allowed by the audit_events schema
    # (``dept_id`` is nullable for cross-dept system events).
    return workflow_id, None


@activity.defn(name="precommit_scanner")
async def precommit_scanner(diff: str) -> ScanResult:
    """Scan *diff* for secrets and gate the commit.

    The activity is intentionally a thin wrapper around the pure
    :func:`scan_diff` core: the deterministic scan happens first, the
    audit emission is layered on top only when ``decision == "block"``,
    and the :class:`ScanResult` is returned regardless of audit
    success / failure.

    Caller contract
    ---------------
    * ``start_to_close_timeout`` is configured by the caller
      (``AgentRunnerWorkflow``); a low value (eg. 10s) is appropriate
      because the scan is CPU-bound regex work over a single diff.
    * The activity is **idempotent** — same diff always produces the
      same ``ScanResult`` — so Temporal retries on transient worker
      failures are safe.
    * On ``decision == "block"`` the workflow MUST fail the
      ``bitbucket_commit_patch`` step. The audit row produced here
      records the scanner decision; the workflow handles the user-visible
      Jira comment / needs_info reply.

    Parameters
    ----------
    diff:
        The unified-diff text the workflow is about to commit.

    Returns
    -------
    ScanResult
        Same as :func:`scan_diff` — frozen dataclass with ``decision``
        and ``matched_patterns``.
    """

    result = scan_diff(diff)

    if result.decision == "block":
        workflow_id, dept_id = _resolve_workflow_context()
        # The activity argument signature is
        # ``precommit_scanner(diff: str) -> ScanResult`` — no
        # ``issue_key`` is passed in. Operators can correlate the
        # audit row to the issue via ``workflow_id`` (which encodes
        # the issue key for Jira-triggered workflows, eg.
        # ``automation-jira-PAY-4211``).
        await _emit_block_audit(
            matched_patterns=result.matched_patterns,
            dept_id=dept_id,
            workflow_id=workflow_id,
            issue_key=None,
        )
        _LOG.info(
            "precommit_scanner: blocked commit "
            "matched_patterns=%s workflow_id=%s",
            result.matched_patterns,
            workflow_id,
        )
    else:
        _LOG.debug("precommit_scanner: pass (no secret patterns matched)")

    return result
