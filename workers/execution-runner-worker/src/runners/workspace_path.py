"""Deterministic, traversal-safe workspace path builder for the execution runner.

The execution-runner places task workspaces under a fixed layout::

    {RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}/

This layout is the contract between the runner workflow and the prompt
template. To avoid drift, both ``runners/remote_ssh.py`` and
``runners/remote_ssh_docker.py`` are expected to derive the workspace path
**only** through
:func:`build_workspace_path`.

The helper is intentionally tiny and dependency-free — Python stdlib only —
because it sits on the hot path of every execution-runner activity invocation
and is exercised by the property test in
``platform/tests/property/test_runner_workspace_path.py``.

Validation rules:

* ``issue_key`` must match ``^[A-Z][A-Z0-9_]*-\\d+$`` — a Jira-style key
  (e.g. ``PAY-4211``, ``OPS_CORE-12``). Anything else (path traversal vectors
  like ``..``, ``../etc``, or shell metachars like ``;``, ``&``, ``|``,
  newline, null-byte) is rejected with :class:`InvalidIssueKeyError` — the
  function never touches the filesystem with an unvalidated key.
* ``iter_n`` must be an ``int`` in ``[0, 999]``; outside that range raises
  :class:`InvalidIterError`. Booleans are rejected (``bool`` is a subclass of
  ``int`` in Python, but a workspace iteration of ``True`` / ``False`` is
  almost certainly a caller bug, not an intent).

The function returns a plain string with forward-slash separators. It does
**not** call ``os.path.join`` — the runner targets remote POSIX hosts via
SSH, where backslash separators on a Windows control-plane would corrupt
the path.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "InvalidIssueKeyError",
    "InvalidIterError",
    "ISSUE_KEY_PATTERN",
    "MAX_ITER",
    "MIN_ITER",
    "build_workspace_path",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Compiled regex that ``issue_key`` MUST satisfy. Matches Jira-style project
#: keys followed by a numeric issue id, e.g. ``PAY-4211`` or ``OPS_CORE-12``.
ISSUE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

#: Inclusive lower bound for the iteration counter.
MIN_ITER: Final[int] = 0

#: Inclusive upper bound for the iteration counter. Three digits is enough
#: headroom for any plausible retry/iteration count and keeps the rendered
#: ``iter-{N}`` segment short and shell-safe.
MAX_ITER: Final[int] = 999


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidIssueKeyError(ValueError):
    """Raised when ``issue_key`` does not match :data:`ISSUE_KEY_PATTERN`.

    The offending value is exposed via :attr:`issue_key` so callers can
    surface a useful audit payload without re-parsing the message.
    """

    def __init__(self, issue_key: object) -> None:
        self.issue_key = issue_key
        super().__init__(
            f"issue_key={issue_key!r} does not match "
            f"{ISSUE_KEY_PATTERN.pattern!r}"
        )


class InvalidIterError(ValueError):
    """Raised when ``iter_n`` is not an ``int`` in ``[MIN_ITER, MAX_ITER]``."""

    def __init__(self, iter_n: object) -> None:
        self.iter_n = iter_n
        super().__init__(
            f"iter_n={iter_n!r} is not an int in [{MIN_ITER}, {MAX_ITER}]"
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def build_workspace_path(base: str, issue_key: str, iter_n: int) -> str:
    """Return the canonical workspace path for ``(base, issue_key, iter_n)``.

    Args:
        base: Workspace root, typically ``settings.runner_base_path``
            (the ``RUNNER_BASE_PATH`` env var with ``SSH_BASE_PATH`` legacy
            alias). Trailing forward slashes are stripped so that the output
            is deterministic regardless of whether callers pass
            ``/var/ai-runner`` or ``/var/ai-runner/``.
        issue_key: Jira-style task key. MUST match
            :data:`ISSUE_KEY_PATTERN` — see module docstring.
        iter_n: Iteration counter, ``0 <= iter_n <= 999``. Booleans are
            rejected (a ``bool`` iteration is almost always a caller bug).

    Returns:
        ``"{base}/{issue_key}/iter-{iter_n}"`` with forward-slash separators.

    Raises:
        InvalidIssueKeyError: ``issue_key`` is not a string or does not
            match :data:`ISSUE_KEY_PATTERN`.
        InvalidIterError: ``iter_n`` is not an ``int`` (or is a ``bool``)
            or falls outside ``[MIN_ITER, MAX_ITER]``.

    Example:
        >>> build_workspace_path("/var/ai-runner", "PAY-4211", 0)
        '/var/ai-runner/PAY-4211/iter-0'
        >>> build_workspace_path("/var/ai-runner/", "OPS_CORE-12", 3)
        '/var/ai-runner/OPS_CORE-12/iter-3'
    """
    # --- issue_key guard (path-traversal safety) --------------------------
    # ``re.fullmatch`` returns ``None`` when no match; ``isinstance`` guard
    # short-circuits non-str inputs (``re.fullmatch`` would raise a
    # ``TypeError`` otherwise, but a typed exception is friendlier for
    # downstream audit payloads).
    if not isinstance(issue_key, str) or ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        raise InvalidIssueKeyError(issue_key)

    # --- iter_n guard ------------------------------------------------------
    # Reject booleans up-front — ``isinstance(True, int)`` is ``True`` in
    # Python and would otherwise quietly render as ``iter-1`` / ``iter-0``.
    if isinstance(iter_n, bool) or not isinstance(iter_n, int):
        raise InvalidIterError(iter_n)
    if iter_n < MIN_ITER or iter_n > MAX_ITER:
        raise InvalidIterError(iter_n)

    # --- base normalisation ------------------------------------------------
    # Strip trailing forward slashes so that the helper is deterministic for
    # callers that pass either ``/var/ai-runner`` or ``/var/ai-runner/``.
    # ``base`` is a trusted env-derived value (settings.runner_base_path);
    # the helper does not validate it further.
    normalised_base = base.rstrip("/") if isinstance(base, str) else base

    return f"{normalised_base}/{issue_key}/iter-{iter_n}"
