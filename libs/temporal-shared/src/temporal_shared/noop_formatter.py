"""Pure Jira-comment formatter for the ``noop_test`` smoke flow.

This module hosts a single **pure** helper function used by the
``noop_test_post_result`` activity wired into
:class:`automation_worker.workflows.automation_workflow.AutomationWorkflow`
when it awaits a ``noop_test`` :class:`ExecutionRunWorkflow` child
(see ``platform-mimari-workflows`` requirements.md §R6.8 / design.md
§"Workflow Type Routing", task 10.4):

* :func:`format_noop_result_comment` — composes the Jira-comment body
  reporting the runner's exit code and stdout snippet for a
  ``noop_test`` execution.

The ``noop_test`` workflow type is the smoke-test path for newly-onboarded
departments: a Jira issue triggers
``AutomationWorkflow → ExecutionRunWorkflow → SSH runner → echo "ok"
→ Jira comment``.  No LLM is involved, so the success / failure
comment is composed by this pure formatter rather than by a prompt.
The activity that calls into this helper performs the side effect
(Jira comment write) but the body it posts is determined entirely by
the formatter, so the wording is locked down by unit tests in
``platform/libs/temporal-shared/tests/test_noop_formatter.py``.

Purity contract
---------------
:func:`format_noop_result_comment` is **pure**:

* No I/O — the runner exit code and stdout snapshot are passed in by
  the caller.
* No clocks — the comment text does not embed a timestamp; correlation
  with the runner execution happens through the workflow id (which the
  activity attaches separately if needed).
* No randomness, no UUIDs, no globals.

This makes the helper safe to call from anywhere the runtime imposes
replay determinism (Temporal workflow body, Hypothesis property test,
activity body).  The matching AST replay-determinism property test
(``tests/property/test_workflow_determinism_static.py``, task 2.7)
will fire if a future edit introduces a forbidden import here.

Why a dedicated module?
-----------------------
The Jira-comment shape for ``noop_test`` is the only piece of
``noop_test``-specific copy in the platform: every other workflow type
uses LLM-generated wording.  Keeping the helper in its own short
module — rather than tucked inside
``temporal_shared.confluence`` or another adjacent file — means the
pinned wording, the truncation cap, and the requirement reference
sit together where the unit tests can find them, and a future
internationalisation effort can swap the literal strings here without
touching unrelated formatters.

Validates: Requirements 6.8.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    # public formatter
    "format_noop_result_comment",
    # public constants (visible so tests can pin them)
    "NOOP_STDOUT_TRUNCATE_CHARS",
    "NOOP_TRUNCATION_MARKER",
    "NOOP_SUCCESS_PREFIX",
    "NOOP_FAILURE_PREFIX",
    "NOOP_EXIT_CODE_UNKNOWN",
]


# ---------------------------------------------------------------------------
# Public constants — pinned by the requirement and unit tests
# ---------------------------------------------------------------------------

#: Maximum number of characters of captured stdout reproduced verbatim
#: in the Jira comment.  Anything longer is truncated with the
#: :data:`NOOP_TRUNCATION_MARKER` appended so the full text remains
#: discoverable via the runner's MinIO artifact URI (which the activity
#: surfaces alongside the comment when applicable).
#:
#: 1024 characters is generous enough to preserve any reasonable
#: ``echo "ok"``-style smoke output while still keeping the Jira
#: comment readable on small screens.  Pinned by the user-supplied
#: task description for task 10.4 ("stdout truncated above 1024 chars
#: (to keep Jira comments readable)").
NOOP_STDOUT_TRUNCATE_CHARS: Final[int] = 1024

#: Marker appended to truncated stdout snippets so a Jira reader knows
#: the captured body was longer than the inline preview.  Trailing
#: whitespace is intentional so the marker reads naturally inside a
#: code block.
NOOP_TRUNCATION_MARKER: Final[str] = "… [truncated]"

#: Comment prefix for a successful ``noop_test`` run (``exit_code == 0``).
#: Carries the ✅ emoji so a Jira reader can scan the issue history
#: visually.  The literal Turkish wording is pinned by the unit
#: tests; do not edit without updating those tests.
NOOP_SUCCESS_PREFIX: Final[str] = "✅ noop_test sonucu"

#: Comment prefix for a failed ``noop_test`` run (``exit_code != 0``
#: or ``exit_code is None``).  ❌ emoji mirrors the ✅ from
#: :data:`NOOP_SUCCESS_PREFIX` so the colour coding is consistent.
NOOP_FAILURE_PREFIX: Final[str] = "❌ noop_test sonucu"

#: Sentinel rendered in the comment when the runner did not surface an
#: exit code (e.g. the child workflow timed out before the SSH command
#: finished).  Kept distinct from the integer ``0`` / ``1`` paths so a
#: reader can tell "we never got an exit code" apart from "we got
#: zero".
NOOP_EXIT_CODE_UNKNOWN: Final[str] = "n/a"


# ---------------------------------------------------------------------------
# format_noop_result_comment  (Requirement 6.8, task 10.4)
# ---------------------------------------------------------------------------


def format_noop_result_comment(
    *,
    exit_code: int | None,
    stdout: str | None,
) -> str:
    """Compose the Jira-comment body reporting a ``noop_test`` outcome.

    Pure string composition — the function performs no I/O and never
    reads a clock.  The activity that writes the comment passes in the
    runner-supplied ``exit_code`` and captured ``stdout``; this helper
    decides whether the prefix is success / failure and applies the
    1024-character truncation cap so the comment renders cleanly in
    Jira's preview.

    Output format
    -------------
    The comment body is one of two shapes::

        ✅ noop_test sonucu: exit_code=0, çıktı:
        ```
        ok
        ```

        ❌ noop_test sonucu: exit_code=1, çıktı:
        ```
        connection refused
        ```

    Special cases:

    * ``stdout`` is ``None`` or an empty string → ``çıktı: <yok>`` (no
      code block).
    * ``stdout`` longer than :data:`NOOP_STDOUT_TRUNCATE_CHARS` →
      truncated to the cap and the :data:`NOOP_TRUNCATION_MARKER`
      appended inside the code block.
    * ``exit_code`` is ``None`` → rendered as
      :data:`NOOP_EXIT_CODE_UNKNOWN` and the failure prefix is used
      (a missing exit code is never a success).

    Parameters
    ----------
    exit_code:
        Runner-reported process exit code.  ``0`` is success; any
        other integer is failure; ``None`` means the runner did not
        surface an exit code (also failure for the purpose of the
        comment prefix).
    stdout:
        Captured stdout text.  ``None`` and ``""`` both render as
        "no output".  Non-string values are rejected so the activity
        cannot accidentally pass through a bytes payload.

    Returns
    -------
    str
        Markdown-formatted Jira-comment body ready to be passed to
        the ``jira_add_comment`` MCP tool.  The string contains no
        trailing newline.

    Raises
    ------
    TypeError
        If ``exit_code`` is not ``None`` or :class:`int`, or if
        ``stdout`` is not ``None`` or :class:`str`.

    Examples
    --------
    >>> format_noop_result_comment(exit_code=0, stdout="ok\\n")
    '✅ noop_test sonucu: exit_code=0, çıktı:\\n```\\nok\\n\\n```'
    >>> format_noop_result_comment(exit_code=1, stdout="boom")
    '❌ noop_test sonucu: exit_code=1, çıktı:\\n```\\nboom\\n```'
    >>> format_noop_result_comment(exit_code=0, stdout=None)
    '✅ noop_test sonucu: exit_code=0, çıktı: <yok>'
    >>> format_noop_result_comment(exit_code=None, stdout="ok")
    '❌ noop_test sonucu: exit_code=n/a, çıktı:\\n```\\nok\\n```'
    """
    # Validate types upfront so a wrong call site fails fast rather
    # than producing a malformed comment.  ``bool`` is rejected for
    # ``exit_code`` because ``isinstance(True, int)`` is True in
    # Python and we do not want ``exit_code=True`` to render as
    # ``exit_code=True``.
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise TypeError(
            "exit_code must be int or None (got "
            f"{type(exit_code).__name__})"
        )
    if stdout is not None and not isinstance(stdout, str):
        raise TypeError(
            f"stdout must be str or None (got {type(stdout).__name__})"
        )

    # Choose prefix.  ``None`` / non-zero → failure; ``0`` only →
    # success.  This matches the standard POSIX convention and is
    # the same logic the unit tests pin.
    if exit_code == 0:
        prefix = NOOP_SUCCESS_PREFIX
        rendered_exit_code = "0"
    else:
        prefix = NOOP_FAILURE_PREFIX
        rendered_exit_code = (
            NOOP_EXIT_CODE_UNKNOWN if exit_code is None else str(exit_code)
        )

    # Render the stdout block.  We use a fenced code block so newlines
    # in the captured output render verbatim in Jira's markdown preview
    # without the activity having to escape them.
    if not stdout:
        # Both ``None`` and ``""`` collapse to the "no output" form.
        return f"{prefix}: exit_code={rendered_exit_code}, çıktı: <yok>"

    truncated = _truncate_stdout(stdout)
    return (
        f"{prefix}: exit_code={rendered_exit_code}, çıktı:\n"
        f"```\n{truncated}\n```"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _truncate_stdout(stdout: str) -> str:
    """Apply the 1024-character cap with a trailing marker on overflow.

    Returns *stdout* unchanged when it is at or below the cap.
    Otherwise returns the first :data:`NOOP_STDOUT_TRUNCATE_CHARS`
    characters with :data:`NOOP_TRUNCATION_MARKER` appended on a new
    line so the marker remains visible inside the fenced code block.

    The function operates on **characters**, not bytes — Turkish
    characters in the captured output count as one each, which is the
    correct unit for Jira's rendered preview width.
    """
    if len(stdout) <= NOOP_STDOUT_TRUNCATE_CHARS:
        return stdout
    head = stdout[:NOOP_STDOUT_TRUNCATE_CHARS]
    return f"{head}\n{NOOP_TRUNCATION_MARKER}"
