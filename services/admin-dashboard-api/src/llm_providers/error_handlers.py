"""Custom :class:`fastapi.exceptions.RequestValidationError` handler.

Pydantic v2's default 422 body shape is ``{"detail": [{"type": ..., "loc": [...], ...}, ...]}``;
the spec mandates four narrower shapes
(``validation_failed`` / ``unsupported_provider_type`` /
``extra_fields_not_allowed``) so the UI can render an actionable error
without parsing Pydantic's internal taxonomy.  This handler walks the
:attr:`RequestValidationError.errors` list once and projects the result
into one of those four shapes.

Mapping rules (kept aligned with the design's "Error taxonomy" table):

* ``extra_forbidden`` (Pydantic) → ``extra_fields_not_allowed`` with
  the offending ``fields`` list (R8.4, R2.8).
* ``literal_error`` on ``provider_type`` → ``unsupported_provider_type``
  with the offending value and the ``PROVIDER_TYPES`` allow-list (R2.6).
* ``missing`` → ``validation_failed`` with the ``missing_fields`` list
  (R2.5).
* Anything else → ``validation_failed`` with a single ``{field, reason}``
  pair (covers numeric bounds, ``HttpUrl`` scheme errors, etc.).

The handler returns a deterministic JSON body and a fixed HTTP 422 so
the UI and the property suite both see one shape per failure category.
"""

from __future__ import annotations

from typing import Any, Sequence

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas import PROVIDER_TYPES


__all__ = ["register_validation_error_handler"]


def register_validation_error_handler(app: FastAPI) -> None:
    """Install the validation handler on *app*.

    Mounted once at app construction time; the handler short-circuits
    every ``RequestValidationError`` raised inside the router stack
    (Pydantic body / query / path validation).
    """

    app.add_exception_handler(
        RequestValidationError, _llm_provider_validation_handler
    )


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


async def _llm_provider_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Project a Pydantic ``RequestValidationError`` into the spec body."""

    body = _classify(exc.errors())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body
    )


def _classify(errors: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Walk *errors* and pick the highest-priority spec body shape.

    Priority order — first match wins so the UI sees the most actionable
    diagnostic for a single mistake rather than the longest list of
    secondary complaints:

    1. ``unsupported_provider_type`` (discriminator literal_error) —
       the operator picked a provider we do not support; the rest of
       the body is irrelevant.
    2. ``extra_fields_not_allowed`` — the operator sent prompt-shaping
       fields on a test endpoint (R8.4) or unknown fields on update.
    3. ``validation_failed`` with ``missing_fields`` — required-field
       omissions across the discriminated union (R2.5).
    4. ``validation_failed`` with ``{field, reason}`` — single
       constraint failure (numeric bounds, URL scheme, etc.).
    """

    unsupported = _check_unsupported_provider_type(errors)
    if unsupported is not None:
        return unsupported

    extra_fields = _collect_extra_fields(errors)
    if extra_fields:
        return {
            "error": "extra_fields_not_allowed",
            "fields": extra_fields,
        }

    missing = _collect_missing_fields(errors)
    if missing:
        return {"error": "validation_failed", "missing_fields": missing}

    # Fall through — surface the first remaining error as a single
    # ``{field, reason}`` pair so the UI can highlight one input at a
    # time. Pydantic guarantees ``errors`` is non-empty when this
    # function runs (the handler is only invoked on a real failure).
    first = errors[0]
    return {
        "error": "validation_failed",
        "field": _format_loc(first.get("loc", ())),
        "reason": str(first.get("msg", "invalid value")),
    }


def _check_unsupported_provider_type(
    errors: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the ``unsupported_provider_type`` body when applicable."""

    for err in errors:
        loc = err.get("loc", ())
        err_type = err.get("type", "")
        if (
            err_type
            in ("literal_error", "union_tag_invalid", "union_tag_not_found")
            and loc
            and loc[-1] == "provider_type"
        ):
            received = err.get("input")
            if received is None:
                ctx = err.get("ctx") or {}
                received = ctx.get("tag") or ctx.get("expected")
            return {
                "error": "unsupported_provider_type",
                "provider_type": received if received is not None else "",
                "supported": list(PROVIDER_TYPES),
            }
    return None


def _collect_extra_fields(errors: Sequence[dict[str, Any]]) -> list[str]:
    """Return the list of fields rejected by ``extra="forbid"``."""

    fields: list[str] = []
    for err in errors:
        if err.get("type") == "extra_forbidden":
            loc = err.get("loc", ())
            if loc:
                fields.append(_format_loc(loc))
    return fields


def _collect_missing_fields(errors: Sequence[dict[str, Any]]) -> list[str]:
    """Return the list of ``missing`` required fields."""

    fields: list[str] = []
    for err in errors:
        if err.get("type") == "missing":
            loc = err.get("loc", ())
            if loc:
                fields.append(_format_loc(loc))
    return fields


#: Discriminator values that show up as a synthetic ``loc`` segment
#: when a discriminated-union variant rejects a field. The error
#: handler strips them so the surfaced field name matches the JSON
#: key the operator sent (``"prompt"`` rather than ``"openai.prompt"``).
_DISCRIMINATOR_VALUES = frozenset({"vllm", "openai", "anthropic", "gemini"})


def _format_loc(loc: Sequence[Any]) -> str:
    """Render a Pydantic ``loc`` tuple as a dotted field path.

    Strips the leading ``"body"`` segment FastAPI prepends *and*
    the discriminator value (``"openai"`` / ``"anthropic"`` / …)
    Pydantic injects inside a discriminated union so the surfaced
    name matches the JSON key the operator actually sent.
    """

    parts = [
        str(p)
        for p in loc
        if p != "body" and str(p) not in _DISCRIMINATOR_VALUES
    ]
    return ".".join(parts) if parts else ""
