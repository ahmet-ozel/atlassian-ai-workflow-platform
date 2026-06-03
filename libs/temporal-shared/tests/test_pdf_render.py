"""Unit tests for ``temporal_shared.pdf_render``.

Covers deterministic PDF rendering and ``jira_attachment`` format
handling for ``pdf`` and ``md`` outputs.

The test suite is split into two halves:

* **Pure-Python contract** — Jinja2 input validation, error
  classification, public constant pinning.  These tests run on every
  host and do not require WeasyPrint's native runtime.
* **End-to-end PDF rendering** — exercised behind
  :func:`pytest.importorskip` so a developer workstation that has
  not yet installed the Pango / Cairo / GLib system packages still
  gets a green ``pytest libs/temporal-shared`` run.  The CI image
  ships these libraries, so the e2e half is exercised there.

"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from temporal_shared.pdf_render import (
    DETERMINISTIC_PDF_TIMESTAMP,
    PDF_MAGIC,
    PdfRenderError,
    PdfRenderUnavailableError,
    render_pdf,
)


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """Public constants are pinned by the requirements / design doc."""

    def test_pdf_magic_is_ascii_pdf_prefix(self) -> None:
        """

        Every well-formed PDF starts with ``%PDF-``; the constant is
        exposed so tests and activity-layer assertions reference the
        same literal.
        """
        assert PDF_MAGIC == b"%PDF-"

    def test_deterministic_timestamp_is_fixed_epoch(self) -> None:
        """

        The timestamp pin is what makes ``render_pdf`` byte-deterministic.
        It must be a timezone-aware ``datetime`` so WeasyPrint does not
        silently fall back to the local clock.
        """
        assert isinstance(DETERMINISTIC_PDF_TIMESTAMP, datetime)
        assert DETERMINISTIC_PDF_TIMESTAMP.tzinfo is not None
        assert DETERMINISTIC_PDF_TIMESTAMP == datetime(
            2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc
        )

    def test_default_template_ships_with_repo(self) -> None:
        """

        The ``jira_attachment`` activity loads the default template
        from ``platform/prompts/pdf_templates/default.html.j2``.  We
        check the file exists, is non-empty, and pins A4 + a Turkish
        glyph–capable font stack.
        """
        # Walk up from the test file to the workspace root, then into
        # the prompts tree.  ``__file__`` is platform/libs/temporal-shared/
        # tests/test_pdf_render.py — three parents lands us at the
        # workspace root regardless of the local checkout layout.
        here = Path(__file__).resolve()
        workspace_root = here.parents[3]  # platform/
        template = workspace_root / "prompts" / "pdf_templates" / "default.html.j2"
        assert template.exists(), f"missing default template at {template}"
        body = template.read_text(encoding="utf-8")
        assert body.strip(), "default template is empty"
        # A4 page size is an explicit the PDF rendering contract.
        assert "size: A4" in body
        # The font stack must include a Latin Extended-A capable family
        # (Turkish glyphs live there).  We check for the primary
        # WeasyPrint-runtime family ("DejaVu Sans") plus a fallback to
        # the generic ``sans-serif`` keyword.
        assert "DejaVu Sans" in body
        assert "sans-serif" in body


# ---------------------------------------------------------------------------
# Pure-Python input validation (no WeasyPrint native deps required)
# ---------------------------------------------------------------------------


class TestInputValidation:
    """Errors before the WeasyPrint call are raised as ``PdfRenderError``."""

    def test_empty_template_is_rejected(self) -> None:
        """

        An empty template would produce an empty PDF that Jira would
        reject as a malformed attachment.  We surface the error
        upstream of WeasyPrint so the activity layer can audit it
        before the import cost is paid.
        """
        with pytest.raises(PdfRenderError, match="must not be empty"):
            render_pdf("   ", {})

    def test_non_string_template_is_rejected(self) -> None:
        with pytest.raises(PdfRenderError, match="must be a string"):
            render_pdf(b"<html></html>", {})  # type: ignore[arg-type]

    def test_non_mapping_context_is_rejected(self) -> None:
        with pytest.raises(PdfRenderError, match="must be a Mapping"):
            render_pdf("<html></html>", ["title", "x"])  # type: ignore[arg-type]

    def test_jinja2_syntax_error_surfaces_as_pdf_render_error(self) -> None:
        """

        Jinja2's :class:`jinja2.TemplateSyntaxError` is wrapped in
        :class:`PdfRenderError` so the activity layer only has to
        catch one error class.  The original cause is preserved via
        ``__cause__``.
        """
        with pytest.raises(PdfRenderError, match="Jinja2 failed"):
            render_pdf("{% for x in %}", {})

    def test_undefined_variable_is_rejected(self) -> None:
        """

        :class:`jinja2.StrictUndefined` is on by design — a missing
        ``context`` key would otherwise silently emit an empty string
        into the PDF, masking activity-layer bugs.
        """
        with pytest.raises(PdfRenderError, match="Jinja2 failed"):
            render_pdf("<p>{{ missing }}</p>", {})


# ---------------------------------------------------------------------------
# End-to-end PDF rendering (skipped when WeasyPrint native deps missing)
# ---------------------------------------------------------------------------

# WeasyPrint loads native libraries (Pango, Cairo, GLib) on import via
# cffi.  On developer workstations — especially Windows — those
# libraries are often missing, so we gate the e2e half of the suite on
# a successful import.  ``pytest.importorskip`` raises an ``ImportError``
# only; WeasyPrint's failure mode is :class:`OSError`, which we have
# to catch ourselves.
try:
    import weasyprint as _weasyprint  # type: ignore[import-not-found]  # noqa: F401

    _WEASYPRINT_AVAILABLE = True
    _WEASYPRINT_SKIP_REASON = ""
except (ImportError, OSError) as _exc:  # pragma: no cover — env-specific
    _WEASYPRINT_AVAILABLE = False
    _WEASYPRINT_SKIP_REASON = (
        "WeasyPrint native runtime unavailable on this host: "
        f"{type(_exc).__name__}: {_exc}"
    )


_MINIMAL_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{{ lang }}">
<head>
  <meta charset="utf-8" />
  <title>{{ title }}</title>
  <style>@page { size: A4; margin: 18mm; }</style>
</head>
<body>
  <h1>{{ title }}</h1>
  <p>{{ body }}</p>
</body>
</html>
"""


@pytest.mark.skipif(
    not _WEASYPRINT_AVAILABLE, reason=_WEASYPRINT_SKIP_REASON or "weasyprint missing"
)
class TestRenderPdfEndToEnd:
    """Real WeasyPrint output exercised when the native stack is present."""

    def test_round_trip_returns_pdf_bytes(self) -> None:
        """

        The basic round trip: a syntactically valid template + simple
        context produces non-empty bytes that start with the
        :data:`PDF_MAGIC` prefix.
        """
        out = render_pdf(
            _MINIMAL_TEMPLATE,
            {"lang": "en", "title": "Round trip", "body": "Hello"},
        )
        assert isinstance(out, bytes)
        assert len(out) > 0
        assert out.startswith(PDF_MAGIC)

    def test_render_is_deterministic(self) -> None:
        """

        Two calls with byte-identical ``(template, context)`` arguments
        produce byte-identical PDFs.  This guarantees that activity
        retries (Temporal at-least-once semantics) cannot create
        diverging Jira attachments.
        """
        ctx = {"lang": "en", "title": "Deterministic", "body": "Same input"}
        first = render_pdf(_MINIMAL_TEMPLATE, ctx)
        second = render_pdf(_MINIMAL_TEMPLATE, ctx)
        assert first == second, (
            "render_pdf must be byte-deterministic so activity retries "
            "cannot upload diverging attachments to Jira."
        )

    def test_turkish_glyphs_render_without_error(self) -> None:
        """

        Rendering a template that contains the full Turkish-specific
        glyph set (``çğıöşü ÇĞİÖŞÜ``) must succeed and produce a
        non-empty PDF.  We do not introspect the resulting PDF's text
        layer (that would require a downstream PDF parser); a
        non-empty, well-formed byte stream is sufficient evidence
        that WeasyPrint accepted the UTF-8 input and the configured
        font stack covered the codepoints.
        """
        out = render_pdf(
            _MINIMAL_TEMPLATE,
            {
                "lang": "tr",
                "title": "Türkçe başlık",
                "body": "çğıöşü ÇĞİÖŞÜ",
            },
        )
        assert out.startswith(PDF_MAGIC)
        # Sanity floor: a one-page A4 PDF is comfortably > 500 bytes
        # even at maximum compression; this catches the "WeasyPrint
        # silently emitted just the magic prefix" failure mode.
        assert len(out) > 500

    def test_default_template_renders_with_full_context(self) -> None:
        """

        The packaged default template (``platform/prompts/pdf_templates/
        default.html.j2``) renders successfully when given the full
        documented context (``title``, ``subtitle``, ``body_html``,
        ``footer``, ``lang``).  This is the contract every
        ``jira_attachment`` activity relies on.
        """
        here = Path(__file__).resolve()
        workspace_root = here.parents[3]
        template_path = (
            workspace_root / "prompts" / "pdf_templates" / "default.html.j2"
        )
        template_source = template_path.read_text(encoding="utf-8")

        out = render_pdf(
            template_source,
            {
                "lang": "tr",
                "title": "Araştırma özeti — Türkçe",
                "subtitle": "Q2 2026",
                "body_html": (
                    "<p>Özet metni — çğıöşü ÇĞİÖŞÜ.</p>"
                    "<ul><li>Bir madde</li><li>İkinci madde</li></ul>"
                ),
                "footer": "🤖 AI provenance",
            },
        )
        assert out.startswith(PDF_MAGIC)
        assert len(out) > 500


# ---------------------------------------------------------------------------
# Unavailability path (always exercised, regardless of native stack)
# ---------------------------------------------------------------------------


class TestUnavailability:
    """The unavailability error path must be the only public failure mode."""

    def test_unavailable_error_is_a_runtime_error(self) -> None:
        """

        :class:`PdfRenderUnavailableError` is intentionally a
        :class:`RuntimeError` (not :class:`PdfRenderError` /
        :class:`ValueError`) so the activity layer can distinguish
        deployment / environment problems from caller-input problems.
        """
        assert issubclass(PdfRenderUnavailableError, RuntimeError)
        assert not issubclass(PdfRenderUnavailableError, PdfRenderError)

    @pytest.mark.skipif(
        _WEASYPRINT_AVAILABLE,
        reason="WeasyPrint is importable on this host; the unavailable "
        "path cannot be exercised without monkey-patching the import.",
    )
    def test_render_raises_unavailable_when_native_missing(self) -> None:
        """

        When the native runtime is missing the function raises
        :class:`PdfRenderUnavailableError` rather than letting the
        underlying :class:`OSError` bubble up unchanged.
        """
        with pytest.raises(PdfRenderUnavailableError):
            render_pdf("<html><body>x</body></html>", {})
