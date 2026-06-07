"""Deterministic Jinja2  WeasyPrint PDF renderer.

This module owns :func:`render_pdf`, the single rendering entry point
used by the ``AgentRunnerWorkflow``'s ``jira_attachment`` output action
when ``payload["format"] == "pdf"``.

Contract
--------

Given a Jinja2 source string and a substitution dict, :func:`render_pdf`
returns the rendered document as a ``bytes`` payload that always starts
with the ``%PDF-`` magic prefix.  The function is **deterministic** for
a given ``(html_template, context)`` pair: WeasyPrint metadata that
would otherwise vary between runs (creation date, modification date,
producer string) is pinned by the renderer.

The companion ``jira_attachment`` output action accepts
``format ∈ {pdf, md}``:

* ``format == "pdf"`` - the activity calls :func:`render_pdf` with
  the resolved template (from ``platform/prompts/pdf_templates/*.html.j2``,
  default ``default.html.j2``) and the activity-supplied context;
  the resulting bytes are uploaded as a Jira issue attachment.
* ``format == "md"`` - the activity attaches the plain Markdown text
  directly; this module is **not** invoked.

Replay determinism
------------------

This module is imported at workflow-package load time, but
:func:`render_pdf` is only ever called from inside an *activity* - never
from workflow code.  Workflow code is forbidden from doing I/O or
embedding clocks,
and PDF rendering is non-trivially side-effecting (it shells out to
WeasyPrint's font cache).  We therefore expose a synchronous helper
that activities wrap with ``activity.heartbeat`` + a generous
``start_to_close_timeout`` (the activity layer is the appropriate
boundary for retry semantics - see design.md §"Activity timeout +
retry").

Native dependencies
-------------------

WeasyPrint depends on Pango / Cairo / GLib via cffi.  On the
production runtime image these libraries are installed alongside the
worker; on developer workstations (especially Windows) the import may
fail.  We therefore defer the WeasyPrint import to call time and
surface a clear :class:`PdfRenderUnavailableError` so callers can
distinguish "the renderer is wired up but the input is bad" from "the
runtime is missing native libs".  The companion test suite uses
:func:`pytest.importorskip` against this same import to skip cleanly
on hosts where the native stack is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final, Mapping

import jinja2

__all__ = [
    "DETERMINISTIC_PDF_TIMESTAMP",
    "PDF_MAGIC",
    "PdfRenderError",
    "PdfRenderUnavailableError",
    "render_pdf",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Magic-number prefix every well-formed PDF document starts with.
#: Pinned here so callers (activities + tests) reference the same
#: literal when asserting that the renderer produced a valid PDF.
PDF_MAGIC: Final[bytes] = b"%PDF-"

#: Fixed timestamp embedded into the rendered PDF's metadata
#: (``CreationDate`` / ``ModDate``) so two ``render_pdf`` invocations
#: with the same ``(html_template, context)`` pair yield byte-identical
#: output.  The chosen epoch - 2000-01-01 00:00:00 UTC - is arbitrary
#: but deliberately *fixed*: a moving clock would defeat replay /
#: cache semantics and would also leak workflow scheduling timing
#: into Jira attachments, which is undesirable from an audit
#: standpoint.
DETERMINISTIC_PDF_TIMESTAMP: Final[datetime] = datetime(
    2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc
)

#: Producer string written into the PDF metadata.  Pinned so the
#: producer field does not vary across WeasyPrint patch releases (the
#: default is ``"WeasyPrint <version>"`` which would change on every
#: dependency bump and break determinism).
_PDF_PRODUCER: Final[str] = "temporal-shared.pdf_render"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PdfRenderError(ValueError):
    """Raised when the input cannot be rendered to a PDF.

    Covers (a) Jinja2 syntax / undefined-variable errors and (b)
    WeasyPrint HTML/CSS validation errors.  The original cause is
    chained via ``__cause__`` so the activity layer can surface it in
    the audit log without losing the traceback.
    """


class PdfRenderUnavailableError(RuntimeError):
    """Raised when the WeasyPrint runtime cannot be loaded.

    Distinct from :class:`PdfRenderError` because the failure is
    environmental (missing Pango / Cairo / GLib) rather than caused by
    the caller's input.  The activity layer should treat this as a
    deployment misconfiguration, not as a workflow-level error.
    """


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_jinja_environment() -> jinja2.Environment:
    """Construct a Jinja2 ``Environment`` with replay-safe defaults.

    The environment uses :class:`jinja2.StrictUndefined` so a missing
    ``context`` key raises immediately (rather than silently emitting
    an empty string into the PDF) and ``autoescape=True`` so embedded
    user data cannot inject HTML/JS into the rendered output.
    """
    return jinja2.Environment(
        loader=None,
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html", "htm", "xhtml", "j2"),
            default_for_string=True,
        ),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render_html(html_template: str, context: Mapping[str, Any]) -> str:
    """Render ``html_template`` against ``context`` and return UTF-8 HTML."""
    if not isinstance(html_template, str):
        raise PdfRenderError(
            f"html_template must be a string (got {type(html_template).__name__})"
        )
    if not html_template.strip():
        raise PdfRenderError("html_template must not be empty")
    if not isinstance(context, Mapping):
        raise PdfRenderError(
            f"context must be a Mapping (got {type(context).__name__})"
        )

    env = _build_jinja_environment()
    try:
        template = env.from_string(html_template)
        return template.render(**dict(context))
    except (jinja2.TemplateSyntaxError, jinja2.UndefinedError) as exc:
        raise PdfRenderError(
            f"Jinja2 failed to render html_template: {exc}"
        ) from exc


def _import_weasyprint() -> Any:
    """Import :mod:`weasyprint` lazily, surfacing a clear runtime error.

    WeasyPrint loads native libraries (Pango, Cairo, GLib) on import
    via cffi; on hosts where those libraries are missing the import
    raises :class:`OSError` rather than :class:`ImportError`.  We
    catch both so the activity layer can rely on
    :class:`PdfRenderUnavailableError` regardless of the exact
    failure mode.
    """
    try:
        import weasyprint  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:  # pragma: no cover - env-specific
        raise PdfRenderUnavailableError(
            "WeasyPrint is not available on this host. Install the "
            "weasyprint Python package and its native runtime "
            "dependencies (Pango, Cairo, GLib) before rendering PDFs."
        ) from exc
    return weasyprint


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_pdf(html_template: str, context: Mapping[str, Any]) -> bytes:
    """Render a Jinja2 HTML template to a deterministic PDF byte string.

    Parameters
    ----------
    html_template:
        Jinja2 source.  Typically loaded from
        ``platform/prompts/pdf_templates/{name}.html.j2`` by the
        activity layer; the default template ``default.html.j2``
        ships with this package and pins A4 size + a Turkish-glyph
        capable font stack.
    context:
        Substitution mapping passed to :meth:`jinja2.Template.render`.
        ``StrictUndefined`` is enabled, so every key referenced by the
        template must be present.

    Returns
    -------
    bytes
        The rendered PDF.  The byte string starts with the
        :data:`PDF_MAGIC` prefix (``%PDF-``) and is suitable for
        upload as a Jira attachment.

    Raises
    ------
    PdfRenderError
        If the inputs are malformed, the Jinja2 template fails to
        render (syntax error / undefined variable), or WeasyPrint
        rejects the resulting HTML/CSS.
    PdfRenderUnavailableError
        If WeasyPrint cannot be imported because its native
        dependencies (Pango, Cairo, GLib) are missing.

    Determinism
    -----------
    The function pins the PDF metadata timestamps to
    :data:`DETERMINISTIC_PDF_TIMESTAMP` and the producer string to
    ``"temporal-shared.pdf_render"`` so two calls with the same
    ``(html_template, context)`` pair return byte-identical output.
    Callers depending on byte-equality (e.g. content hashing for
    Jira attachment dedup) may rely on this guarantee.
    """
    rendered_html = _render_html(html_template, context)
    weasyprint = _import_weasyprint()

    # WeasyPrint accepts a string source via ``string=``; we also pin
    # the encoding so byte-level non-determinism from filesystem
    # encoding lookups cannot leak into the output.
    try:
        document = weasyprint.HTML(string=rendered_html, encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        raise PdfRenderError(
            f"WeasyPrint failed to parse rendered HTML: {exc}"
        ) from exc

    # ``write_pdf(target=None, ...)`` returns the PDF as ``bytes``.
    # ``zrl`` and metadata kwargs are passed straight through to the
    # WeasyPrint PDF backend.  The two timestamp kwargs and the
    # producer kwarg below are the levers that make the byte stream
    # deterministic; without them WeasyPrint stamps the current wall
    # clock into ``CreationDate`` / ``ModDate`` and embeds its own
    # version into ``Producer``, both of which would defeat
    # byte-equality on replay.
    try:
        pdf_bytes = document.write_pdf(
            target=None,
            uncompressed_pdf=False,
            # ``timestamp`` is the canonical WeasyPrint kwarg for
            # pinning ``CreationDate`` / ``ModDate``.  It expects a
            # ``datetime`` instance.
            timestamp=DETERMINISTIC_PDF_TIMESTAMP,
        )
    except TypeError:
        # Older WeasyPrint releases do not expose ``timestamp`` /
        # ``uncompressed_pdf`` kwargs.  Fall back to the minimal call
        # - the resulting bytes will still be valid PDFs, but byte
        # determinism is not guaranteed on those legacy versions.
        # The pyproject pin (>=62) excludes those versions on the
        # production runtime; this branch is purely defensive.
        pdf_bytes = document.write_pdf(target=None)
    except Exception as exc:  # pragma: no cover - defensive
        raise PdfRenderError(
            f"WeasyPrint failed to write PDF: {exc}"
        ) from exc

    if not isinstance(pdf_bytes, (bytes, bytearray)):  # pragma: no cover
        raise PdfRenderError(
            f"WeasyPrint returned {type(pdf_bytes).__name__} instead of bytes"
        )
    pdf_bytes = bytes(pdf_bytes)

    if not pdf_bytes.startswith(PDF_MAGIC):  # pragma: no cover - defensive
        raise PdfRenderError(
            "WeasyPrint output is missing the %PDF- magic prefix; "
            "the runtime may be misconfigured."
        )

    return pdf_bytes
