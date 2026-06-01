"""Pure helper functions for infrastructural decisions.

All functions in this module are **pure** — they perform no I/O and have
no side effects.  They encode simple truth tables and coercion rules
documented in MIMARI.md and design.md.

Functions:
    should_cleanup: Determines whether workspace cleanup should occur
        based on the cleanup policy and the process exit code.
    coerce_draft_true: Coerces any truthy/falsy value to boolean True,
        enforcing the "always draft" PR rule (MIMARI §1 Kural 10).
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "CleanupPolicy",
    "should_cleanup",
    "coerce_draft_true",
]

CleanupPolicy = Literal["always", "on_success", "never"]


def should_cleanup(policy: CleanupPolicy, exit_code: int) -> bool:
    """Determine whether workspace cleanup should be performed.

    Truth table (design.md §Property 12.4):

    +--------------+----------------+----------------+
    | policy       | exit_code == 0 | exit_code != 0 |
    +==============+================+================+
    | ``always``   | True           | True           |
    +--------------+----------------+----------------+
    | ``on_success``| True          | False          |
    +--------------+----------------+----------------+
    | ``never``    | False          | False          |
    +--------------+----------------+----------------+

    Args:
        policy: One of ``"always"``, ``"on_success"``, or ``"never"``.
        exit_code: The integer exit code of the executed process.

    Returns:
        True if cleanup should be performed, False otherwise.

    Raises:
        ValueError: If *policy* is not one of the three allowed values.

    Examples::

        >>> should_cleanup("always", 0)
        True
        >>> should_cleanup("always", 1)
        True
        >>> should_cleanup("on_success", 0)
        True
        >>> should_cleanup("on_success", 1)
        False
        >>> should_cleanup("never", 0)
        False
        >>> should_cleanup("never", 1)
        False
    """
    if policy == "always":
        return True
    if policy == "on_success":
        return exit_code == 0
    if policy == "never":
        return False
    raise ValueError(
        f"Invalid cleanup policy {policy!r}: "
        f"must be one of 'always', 'on_success', 'never'"
    )


def coerce_draft_true(value: object) -> bool:
    """Coerce any input value to ``True``.

    This function enforces MIMARI §1 Kural 10: all PRs opened by the bot
    MUST be draft PRs.  Regardless of what the caller or LLM output
    specifies for the ``draft`` field, this function always returns True.

    The function exists as a named, testable unit so that the invariant
    "draft is always True" can be property-tested independently of the
    PR-opening activity.

    Args:
        value: Any value (True, False, None, 0, 1, "true", "false", etc.).

    Returns:
        Always ``True``.

    Examples::

        >>> coerce_draft_true(True)
        True
        >>> coerce_draft_true(False)
        True
        >>> coerce_draft_true(None)
        True
        >>> coerce_draft_true(0)
        True
        >>> coerce_draft_true("false")
        True
    """
    return True
