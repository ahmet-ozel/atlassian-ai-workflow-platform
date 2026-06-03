"""Canonical PR-description renderer for prompt change PRs.

This module replaces the placeholder ``_render_pr_description``
helper that the task-6.1 :mod:`src.routers.prompts_git` router carried
inline. The renderer is a **pure function** with no I/O, no app
state, and no global mutation — every piece of context is passed in:

* ``path``                — repository-relative POSIX path of the
  prompt that changed (eg. ``platform/prompts/assistant_chat.md``).
* ``diff``                — unified-format diff produced by
  :meth:`git_shared.GitRepo.diff` against ``main``.
* ``sandbox_history``     — zero or more :class:`SandboxRunSummary`
  rows produced by service lifecycle wiring's ``PromptSandbox.run`` calls. Empty
  when the operator has not exercised the sandbox yet (acceptable
  per design notes §`PromptSandbox` — the sandbox is opt-in).
* ``v15_status``          — :class:`V15SyncStatus` record describing
  whether every backlog ID mentioned by the prompt body also
  appears in ``architecture notes`` (behavior 2.8 / V15 CI gate). The
  renderer surfaces *informational* status only — the hard CI gate
  is owned by ``tests/test_taskprompt_mimari_sync.py`` (prompt sync wiring).

Design references
-----------------
* design notes §`PromptsGitRouter.post_pr` — ``description = diff
  summary + sandbox results + V15 sync info``.
* implementation notes §6.3 — "deterministic Markdown including diff vs main,
  sandbox results from service lifecycle wiring if available, V15 sync info".
* behaviors 2.2, 2.4, 2.8.

Determinism guarantees
----------------------
The output Markdown is produced by string concatenation in a fixed
order:

    1. Header (constant string + path).
    2. Diff vs ``main`` fenced ``diff`` block (truncated past 8 KiB
       so PR providers can render it).
    3. Sandbox results table (one row per
       :class:`SandboxRunSummary`, in input order). Skipped when the
       history is empty — replaced by a short "no sandbox runs
       recorded" notice.
    4. V15 sync section: known backlog IDs in the prompt body, plus
       any IDs that the caller flagged as missing from architecture notes.

Identical inputs therefore always produce a byte-identical output,
which makes the renderer trivially testable and lets the property
test suite (invariant 5 — audit log integrity) compare the rendered
description to a golden snapshot when needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxRunSummary:
    """One row in the sandbox-results table inside the PR description.

    Mirrors the public surface of service lifecycle wiring's ``SandboxResult`` dataclass
    so the router can pass the sandbox history through without
    re-shaping it. Kept here (rather than imported from a future
    ``sandbox`` module) so this renderer stays import-clean and can
    be tested before service lifecycle wiring lands.

    Attributes:
        invoked_at: ISO-8601 timestamp of the sandbox run. Treated as
            opaque text — the renderer never parses or sorts on it.
        sample_input_excerpt: First ~120 chars of the sample input
            the operator submitted to the sandbox. Longer inputs are
            already truncated by the caller.
        response_excerpt: First ~120 chars of the LLM response
            captured by the sandbox.
        token_in: Tokens read from the prompt + sample input.
        token_out: Tokens generated in the sandbox response.
        cost_usd: USD cost of the sandbox call. ``Decimal`` so the
            rendered table preserves the exact value the cost tracker
            recorded (no float rounding noise).
    """

    invoked_at: str
    sample_input_excerpt: str
    response_excerpt: str
    token_in: int
    token_out: int
    cost_usd: Decimal


@dataclass(frozen=True)
class V15SyncStatus:
    """V15 (prompt MD ↔ architecture notes) sync status snapshot.

    The router computes this via :func:`extract_v15_status` (or, in
    tests, builds it explicitly) and hands it to the renderer.

    Attributes:
        all_ids: Backlog IDs found in the prompt body (sorted, unique).
        missing_in_mimari: Subset of ``all_ids`` that the caller could
            not find in ``architecture notes``. Empty tuple ⇒ everything is in
            sync. The renderer surfaces this as a callout when
            non-empty so the reviewer notices before merging.
        mimari_available: ``True`` when the caller successfully read
            ``architecture notes``; ``False`` when the file was missing /
            unreadable (the renderer then prints "architecture notes not
            available — V15 sync could not be verified" instead of
            making confident claims).
    """

    all_ids: tuple[str, ...] = ()
    missing_in_mimari: tuple[str, ...] = ()
    mimari_available: bool = True

    def in_sync(self) -> bool:
        """Return True when every prompt backlog ID also lives in architecture."""

        return self.mimari_available and not self.missing_in_mimari


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Fixed lead-in line; tests assert on this verbatim so admins can
#: filter their PR list by a stable prefix.
PR_DESCRIPTION_HEADER: str = "# Prompt change"

#: Keep PR descriptions readable for huge diffs — the PR provider's
#: own diff view is the source of truth so we only need a preview
#: here. Mirrors the original placeholder helper's 8000-char limit.
_MAX_DIFF_CHARS: int = 8000

#: The V15 backlog series — every letter that may carry a 1-2 digit
#: numeric suffix in the architecture cross-reference table. Mirror of the
#: regex used by ``tests/test_taskprompt_mimari_sync.py``
#: 2.8 / V15 CI gate).
_V15_ID_RE: re.Pattern[str] = re.compile(r"\b([XYZNVWGSBEQRT]\d{1,2})\b")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def extract_v15_status(
    *,
    body: str,
    mimari_text: str | None,
) -> V15SyncStatus:
    """Compute the V15 sync status for a prompt body.

    Pure function — no I/O. Callers that want to read ``architecture notes``
    from disk should do so themselves (e.g. ``Path("architecture notes")
    .read_text()``) and pass the contents in; ``None`` means the file
    was unavailable and the renderer should soften its claims.

    Args:
        body: Full Markdown body of the prompt under review.
        mimari_text: Contents of ``architecture notes`` (or ``None`` when the
            caller could not read it).

    Returns:
        :class:`V15SyncStatus` with ``all_ids`` sorted unique, and
        ``missing_in_mimari`` empty when ``mimari_text`` is ``None``
        (since we cannot make a reliable claim either way — the
        ``mimari_available`` flag tells the renderer to print a soft
        warning instead).
    """

    found = sorted(set(_V15_ID_RE.findall(body)))
    if mimari_text is None:
        return V15SyncStatus(
            all_ids=tuple(found),
            missing_in_mimari=(),
            mimari_available=False,
        )

    missing = tuple(_id for _id in found if _id not in mimari_text)
    return V15SyncStatus(
        all_ids=tuple(found),
        missing_in_mimari=missing,
        mimari_available=True,
    )


def render_pr_description(
    *,
    path: str,
    diff: str,
    sandbox_history: Sequence[SandboxRunSummary] | Iterable[SandboxRunSummary] = (),
    v15_status: V15SyncStatus | None = None,
) -> str:
    """Render the canonical PR description Markdown.

    Args:
        path: Repository-relative POSIX path of the prompt being
            changed. Used in the title line and the file mention so
            reviewers can grep by path.
        diff: Unified-format diff vs ``main``. Truncated past 8 KiB.
            ``""`` (empty) renders as ``(no textual diff — empty
            change)`` so the section never disappears entirely; an
            empty diff is still informative ("the file's bytes did
            not change but a new commit was recorded").
        sandbox_history: Zero or more :class:`SandboxRunSummary`
            rows. The renderer iterates the sequence exactly once
            and does not sort — the caller controls the order.
        v15_status: Optional V15 sync snapshot. When ``None`` the
            renderer prints a "(V15 sync info not available)" notice
            so the section keeps its slot in the output.

    Returns:
        UTF-8 Markdown string, suitable for the ``description``
        field of a Bitbucket / GitHub pull request.
    """

    # Materialise the sandbox history once so the renderer can decide
    # whether to print a table or the empty-state notice without
    # consuming a single-shot iterator twice.
    history = tuple(sandbox_history)

    sections: list[str] = []
    sections.append(_render_header(path))
    sections.append(_render_diff_section(diff))
    sections.append(_render_sandbox_section(history))
    sections.append(_render_v15_section(v15_status))

    # Two blank lines between top-level sections; explicit ``\n`` so
    # the rendered output is byte-identical across platforms (no
    # ``os.linesep`` surprises). Trailing newline keeps `git diff`
    # output clean when the description is committed somewhere.
    return "\n\n".join(sections).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Section renderers (kept separate so each can be unit-tested in isolation)
# ---------------------------------------------------------------------------


def _render_header(path: str) -> str:
    """Render the title + provenance lead-in.

    The lead-in cites the spec / requirement so a reviewer landing on
    the PR has a stable handle for context (behavior 2.2). The
    leading line is kept *exactly* equal to ``PR_DESCRIPTION_HEADER``
    + ``: ``+ path so existing tests / dashboards that grep on the
    prefix keep working.
    """

    return (
        f"{PR_DESCRIPTION_HEADER}: `{path}`\n"
        f"\n"
        f"This PR was opened automatically by the admin-dashboard "
        f"prompt editor (operations surface behavior 2.2). The "
        f"description below is rendered deterministically from the "
        f"diff, the sandbox history, and the V15 cross-reference "
        f"table (behavior 2.8)."
    )


def _render_diff_section(diff: str) -> str:
    """Render the diff vs ``main`` fenced ``diff`` block."""

    truncated = diff.strip()
    if len(truncated) > _MAX_DIFF_CHARS:
        truncated = truncated[:_MAX_DIFF_CHARS] + "\n\n…(truncated)…"
    if not truncated:
        truncated = "(no textual diff — empty change)"

    return (
        "## Diff vs `main`\n"
        "\n"
        "```diff\n"
        f"{truncated}\n"
        "```"
    )


def _render_sandbox_section(history: Sequence[SandboxRunSummary]) -> str:
    """Render the "Sandbox results" Markdown table.

    When ``history`` is empty the section becomes a short notice so
    the reviewer is aware no sandbox run was recorded — that is a
    *flag for review*, not an error (sandbox is opt-in per design).
    """

    if not history:
        return (
            "## Sandbox results\n"
            "\n"
            "_No sandbox runs were recorded for this draft._ The "
            "operator may exercise the sandbox via "
            "`POST /admin/prompts/{path}/sandbox-test` (service lifecycle wiring) "
            "before merging this PR."
        )

    rows: list[str] = [
        "## Sandbox results",
        "",
        "| Invoked at | Sample input | Response | Tokens (in / out) | Cost (USD) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for run in history:
        rows.append(
            "| {invoked_at} | {sample} | {response} | {tin} / {tout} | {cost} |".format(
                invoked_at=_md_escape(run.invoked_at),
                sample=_md_escape(_truncate(run.sample_input_excerpt, 120)),
                response=_md_escape(_truncate(run.response_excerpt, 120)),
                tin=run.token_in,
                tout=run.token_out,
                cost=_format_decimal(run.cost_usd),
            )
        )
    rows.append("")
    rows.append(
        "_Sandbox runs are tagged `cost_tag='sandbox'` and never "
        "deduct from the dept production budget (behavior 2.4)._"
    )
    return "\n".join(rows)


def _render_v15_section(v15: V15SyncStatus | None) -> str:
    """Render the V15 (prompt ↔ architecture) sync section."""

    if v15 is None:
        return (
            "## V15 cross-reference (behavior 2.8)\n"
            "\n"
            "_(V15 sync info not available for this PR.)_"
        )

    lines: list[str] = [
        "## V15 cross-reference (behavior 2.8)",
        "",
    ]

    if not v15.all_ids:
        lines.append(
            "No backlog IDs detected in the prompt body. V15 sync "
            "trivially holds (nothing to cross-reference)."
        )
        return "\n".join(lines)

    id_list = ", ".join(f"`{_id}`" for _id in v15.all_ids)
    lines.append(f"Backlog IDs referenced by this prompt: {id_list}.")
    lines.append("")

    if not v15.mimari_available:
        lines.append(
            "⚠️ `architecture notes` was not available when this PR description "
            "was rendered — V15 sync could not be verified. The CI "
            "gate `tests/test_taskprompt_mimari_sync.py` (prompt sync wiring) "
            "remains the source of truth and will fail the build if "
            "any of the IDs above are missing from `architecture notes`."
        )
    elif v15.missing_in_mimari:
        missing = ", ".join(f"`{_id}`" for _id in v15.missing_in_mimari)
        lines.append(
            f"⚠️ The following backlog IDs are **missing** from "
            f"`architecture notes`: {missing}. The V15 CI gate will fail this "
            f"PR until each ID is documented in `architecture notes` or "
            f"removed from the prompt body."
        )
    else:
        lines.append(
            "✅ All backlog IDs above are present in `architecture notes`. "
            "V15 sync gate is satisfied."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    """Truncate ``text`` to ``max_len`` chars; append ``…`` on overflow."""

    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _md_escape(text: str) -> str:
    """Escape Markdown table-breaking characters.

    Pipes terminate cells, so we replace them with the HTML entity
    so the table layout survives. Newlines collapse to a literal
    space — table cells must stay on a single visual row.
    """

    return text.replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def _format_decimal(value: Decimal) -> str:
    """Render a :class:`Decimal` as a fixed-point string.

    Uses ``str(value)`` rather than ``f"{value:.4f}"`` so the renderer
    preserves whatever precision the caller stored in the
    ``cost_tracking`` row. Tests pin the exact output for the
    common ``Decimal('0.0123')`` case.
    """

    return str(value)


__all__ = [
    "PR_DESCRIPTION_HEADER",
    "SandboxRunSummary",
    "V15SyncStatus",
    "extract_v15_status",
    "render_pr_description",
]
