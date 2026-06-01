"""Unit tests for ``src.prompts.pr_renderer`` (platform-mimari-ops 6.3).

The renderer is a pure function: every input is passed in, no I/O is
performed. The tests exercise the four section emitters
(:func:`_render_header`, :func:`_render_diff_section`,
:func:`_render_sandbox_section`, :func:`_render_v15_section`) through
the public :func:`render_pr_description` surface, plus the
:func:`extract_v15_status` helper that the router calls before
invoking the renderer.

Test cases cover:

* Empty diff renders a "no textual diff" notice.
* Long diffs are truncated past 8 KiB.
* Sandbox history empty → "no sandbox runs" notice; non-empty →
  Markdown table with one row per :class:`SandboxRunSummary`.
* Pipe / newline characters in sandbox excerpts are escaped so the
  Markdown table layout survives.
* V15 section reports IDs as in-sync, missing, or unverifiable
  depending on the :class:`V15SyncStatus` shape.
* :func:`extract_v15_status` extracts every backlog ID and reports
  the missing-from-MIMARI subset deterministically.
* The renderer is deterministic — identical inputs produce
  byte-identical Markdown.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# Bootstrap sys.path so the tests can be run via ``pytest`` directly
# from the service root without requiring ``pip install -e``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for lib_dir in (
    _WORKSPACE_ROOT / "libs" / "audit_logger" / "src",
    _WORKSPACE_ROOT / "libs" / "prompts" / "src",
):
    if lib_dir.is_dir() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))


from src.prompts.pr_renderer import (  # noqa: E402
    PR_DESCRIPTION_HEADER,
    SandboxRunSummary,
    V15SyncStatus,
    extract_v15_status,
    render_pr_description,
)


# ---------------------------------------------------------------------------
# Header / diff section
# ---------------------------------------------------------------------------


class TestHeaderAndDiff:
    def test_header_includes_path_and_requirement_anchor(self) -> None:
        out = render_pr_description(
            path="platform/prompts/foo.md",
            diff="@@ -1 +1 @@\n-old\n+new\n",
        )
        # Stable lead-in for greppable PR titles in admin lists.
        assert out.startswith(f"{PR_DESCRIPTION_HEADER}: `platform/prompts/foo.md`")
        # Anchors the reviewer at the spec requirement.
        assert "Requirement 2.2" in out

    def test_diff_section_renders_fenced_diff_block(self) -> None:
        diff_body = "@@ -1 +1 @@\n-old line\n+new line\n"
        out = render_pr_description(
            path="prompts/x.md",
            diff=diff_body,
        )
        assert "## Diff vs `main`" in out
        assert "```diff" in out
        # The actual diff bytes round-trip verbatim (case-sensitive).
        assert "+new line" in out

    def test_empty_diff_renders_empty_change_notice(self) -> None:
        out = render_pr_description(path="prompts/x.md", diff="")
        assert "(no textual diff — empty change)" in out

    def test_long_diff_is_truncated_with_ellipsis(self) -> None:
        # 9000-char body forces the 8000-char truncation branch.
        big_diff = "+" + ("x" * 9000)
        out = render_pr_description(path="prompts/x.md", diff=big_diff)
        assert "…(truncated)…" in out
        # The fenced block boundary is still emitted after the cut.
        assert out.count("```") >= 2


# ---------------------------------------------------------------------------
# Sandbox section
# ---------------------------------------------------------------------------


class TestSandboxSection:
    def test_empty_history_emits_notice(self) -> None:
        out = render_pr_description(path="prompts/x.md", diff="")
        assert "## Sandbox results" in out
        assert "No sandbox runs were recorded" in out

    def test_single_run_renders_table_row(self) -> None:
        run = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            sample_input_excerpt="hello world",
            response_excerpt="hi there",
            token_in=42,
            token_out=17,
            cost_usd=Decimal("0.0123"),
        )
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            sandbox_history=[run],
        )
        # Header + separator row both present.
        assert "| Invoked at | Sample input |" in out
        # Tokens rendered as ``in / out``.
        assert "42 / 17" in out
        # Decimal preserved verbatim — no float rounding.
        assert "0.0123" in out
        # cost_tag invariant called out.
        assert "cost_tag='sandbox'" in out

    def test_pipe_in_excerpt_is_escaped(self) -> None:
        run = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            # A literal pipe would otherwise terminate the table cell.
            sample_input_excerpt="a|b",
            response_excerpt="ok",
            token_in=1,
            token_out=1,
            cost_usd=Decimal("0"),
        )
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            sandbox_history=[run],
        )
        assert "a&#124;b" in out
        # And the raw ``|`` only appears as table separators, never
        # inside a cell — count the un-escaped pipes; they should
        # match the well-formed table shape.
        assert "a|b" not in out

    def test_newline_in_excerpt_is_collapsed(self) -> None:
        run = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            sample_input_excerpt="line1\nline2",
            response_excerpt="r1\nr2",
            token_in=1,
            token_out=1,
            cost_usd=Decimal("0"),
        )
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            sandbox_history=[run],
        )
        # The renderer flattens newlines to spaces; the original
        # multi-line excerpts must not survive verbatim or the row
        # would split across multiple Markdown lines.
        assert "line1\nline2" not in out
        assert "line1 line2" in out

    def test_long_excerpt_is_truncated(self) -> None:
        run = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            sample_input_excerpt="x" * 200,
            response_excerpt="y" * 200,
            token_in=1,
            token_out=1,
            cost_usd=Decimal("0"),
        )
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            sandbox_history=[run],
        )
        # The truncation helper appends an ellipsis past 120 chars.
        assert "…" in out

    def test_multiple_runs_preserve_input_order(self) -> None:
        first = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            sample_input_excerpt="alpha",
            response_excerpt="A",
            token_in=1,
            token_out=1,
            cost_usd=Decimal("0"),
        )
        second = SandboxRunSummary(
            invoked_at="2025-01-02T12:00:00Z",
            sample_input_excerpt="beta",
            response_excerpt="B",
            token_in=2,
            token_out=2,
            cost_usd=Decimal("0.5"),
        )
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            sandbox_history=[first, second],
        )
        # ``alpha`` row appears before ``beta`` row.
        idx_alpha = out.index("alpha")
        idx_beta = out.index("beta")
        assert idx_alpha < idx_beta


# ---------------------------------------------------------------------------
# V15 section
# ---------------------------------------------------------------------------


class TestV15Section:
    def test_in_sync_emits_check_mark(self) -> None:
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            v15_status=V15SyncStatus(
                all_ids=("V2", "Y10"),
                missing_in_mimari=(),
                mimari_available=True,
            ),
        )
        assert "All backlog IDs above are present" in out
        assert "✅" in out
        assert "`V2`" in out and "`Y10`" in out

    def test_missing_ids_emit_warning(self) -> None:
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            v15_status=V15SyncStatus(
                all_ids=("V2", "Y10"),
                missing_in_mimari=("Y10",),
                mimari_available=True,
            ),
        )
        assert "⚠️" in out
        assert "`Y10`" in out
        assert "missing" in out.lower()

    def test_mimari_unavailable_emits_soft_warning(self) -> None:
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            v15_status=V15SyncStatus(
                all_ids=("V2",),
                missing_in_mimari=(),
                mimari_available=False,
            ),
        )
        # Soft warning explains why the gate could not be evaluated
        # locally and points at the CI gate.
        assert "MIMARI.md` was not available" in out
        assert "tests/test_taskprompt_mimari_sync.py" in out

    def test_no_ids_in_body_renders_trivial_message(self) -> None:
        out = render_pr_description(
            path="prompts/x.md",
            diff="",
            v15_status=V15SyncStatus(),
        )
        assert "No backlog IDs detected" in out

    def test_v15_none_renders_not_available_notice(self) -> None:
        out = render_pr_description(path="prompts/x.md", diff="")
        # When the caller passes ``v15_status=None`` (the default),
        # the section keeps its slot but prints a notice.
        assert "## V15 cross-reference" in out
        assert "(V15 sync info not available" in out


# ---------------------------------------------------------------------------
# extract_v15_status
# ---------------------------------------------------------------------------


class TestExtractV15Status:
    def test_extracts_all_letter_series(self) -> None:
        body = (
            "Refers to V2 and Y10. Also B5 and N17. T13 too. "
            "Lowercase v2 must NOT be picked up."
        )
        status = extract_v15_status(body=body, mimari_text="V2 Y10 B5 N17 T13")
        assert "V2" in status.all_ids
        assert "Y10" in status.all_ids
        assert "B5" in status.all_ids
        assert "N17" in status.all_ids
        assert "T13" in status.all_ids
        # Lowercase must not match — the regex is case-sensitive.
        assert "v2" not in status.all_ids

    def test_unique_and_sorted(self) -> None:
        # Body mentions V2 twice; status carries it once.
        body = "V2 first, V2 again. Also Y10."
        status = extract_v15_status(body=body, mimari_text="V2 Y10")
        # Sorted deterministically (string-sort).
        assert status.all_ids == ("V2", "Y10")
        assert status.missing_in_mimari == ()

    def test_missing_in_mimari_subset(self) -> None:
        body = "V2 and Y10 and Y11."
        status = extract_v15_status(body=body, mimari_text="V2 only here")
        assert status.in_sync() is False
        assert "Y10" in status.missing_in_mimari
        assert "Y11" in status.missing_in_mimari
        assert "V2" not in status.missing_in_mimari

    def test_mimari_none_marks_unavailable(self) -> None:
        status = extract_v15_status(body="V2", mimari_text=None)
        assert status.mimari_available is False
        # Missing list is empty when we cannot make a confident claim.
        assert status.missing_in_mimari == ()
        assert status.in_sync() is False  # in_sync requires availability

    def test_in_sync_true_when_all_ids_present(self) -> None:
        status = extract_v15_status(
            body="V2 V15 N20",
            mimari_text="(V2) (V15) (N20)",
        )
        assert status.in_sync() is True

    def test_id_regex_rejects_three_digit_suffix(self) -> None:
        # The MIMARI series uses 1-2 digit suffixes; ``V123`` would
        # be a typo / unrelated identifier.
        status = extract_v15_status(body="V123 says hi", mimari_text="")
        assert status.all_ids == ()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_produce_byte_identical_output(self) -> None:
        run = SandboxRunSummary(
            invoked_at="2025-01-01T12:00:00Z",
            sample_input_excerpt="hello",
            response_excerpt="world",
            token_in=10,
            token_out=20,
            cost_usd=Decimal("0.0007"),
        )
        v15 = V15SyncStatus(
            all_ids=("V2", "Y10"),
            missing_in_mimari=(),
            mimari_available=True,
        )

        out_a = render_pr_description(
            path="prompts/x.md",
            diff="@@ -1 +1 @@\n-a\n+b\n",
            sandbox_history=[run],
            v15_status=v15,
        )
        out_b = render_pr_description(
            path="prompts/x.md",
            diff="@@ -1 +1 @@\n-a\n+b\n",
            sandbox_history=[run],
            v15_status=v15,
        )
        # Byte-identical — the renderer holds no clock / random state.
        assert out_a == out_b
        # Trailing newline keeps committed strings tidy.
        assert out_a.endswith("\n")
