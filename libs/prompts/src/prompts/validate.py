"""Prompt template format validator.

Validates the brace-balanced ``str.format``-style placeholders used
in prompt Markdown bodies before they are loaded into the runtime
:class:`prompts.loader.PromptLoader` cache or committed through the
prompt PR flow (:mod:`admin_dashboard_api.routers.prompts_git`).

The validator enforces three rules:

1. Single literal ``{`` or ``}`` characters are not allowed; callers
   must escape them with ``{{`` / ``}}`` if they want literal braces
   in the rendered output.
2. Every ``{name}`` placeholder must reference one of the well-known
   template variables exposed by :class:`prompts.types.PromptVars`
   (i.e. one of :data:`prompts.types.TEMPLATE_VARIABLE_NAMES`);
   unknown placeholders are rejected so misspellings or stale
   variable names are caught at boot/CI time instead of at LLM
   render time.
3. Format specifications (``{name:spec}``) and conversion flags
   (``{name!r}``) are tolerated as long as ``name`` is a known
   variable; format-spec syntax errors raise
   :class:`PromptTemplateError`.

The function is a pure validator: it returns ``None`` on success and
raises :class:`PromptTemplateError` on any violation.
:meth:`prompts.loader.PromptLoader._read` is expected to call this
validator for every prompt file at boot so an invalid template
prevents the service from starting (or, in the prompt PR flow,
prevents the draft commit).

Public exports
--------------

* :class:`PromptTemplateError` - re-exported from
  :mod:`prompts.errors` so existing call sites can import the symbol
  from either module without confusion.
* :func:`validate_template_format` - the validator itself.
* :data:`KNOWN_TEMPLATE_VARS` - alias of
  :data:`prompts.types.TEMPLATE_VARIABLE_NAMES`, kept for callers
  that want a validator-local handle on the accepted placeholder
  names.
"""

from __future__ import annotations

import string
from typing import Final

from .errors import PromptTemplateError
from .types import TEMPLATE_VARIABLE_NAMES

__all__ = [
    "KNOWN_TEMPLATE_VARS",
    "PromptTemplateError",
    "validate_template_format",
]


#: The accepted placeholder names. Single source of truth lives on
#: :data:`prompts.types.TEMPLATE_VARIABLE_NAMES`; this alias exists so
#: validator-only consumers can grab the set without having to depend
#: on :mod:`prompts.types` directly.
KNOWN_TEMPLATE_VARS: Final[frozenset[str]] = TEMPLATE_VARIABLE_NAMES


# Reuse the standard library's brace parser so our notion of "balanced
# braces" matches what ``str.format`` will accept at render time.
_FORMATTER: Final[string.Formatter] = string.Formatter()


def validate_template_format(body: str) -> None:
    """Validate a prompt body's brace placeholders.

    Args:
        body: The full prompt Markdown text as it will be passed to
            ``str.format(**vars)`` at render time.

    Raises:
        PromptTemplateError: If ``body`` contains an unbalanced or
            unescaped literal ``{`` / ``}``, or references a
            placeholder name outside :data:`KNOWN_TEMPLATE_VARS`.
    """

    try:
        parsed = list(_FORMATTER.parse(body))
    except ValueError as exc:
        # ``string.Formatter.parse`` raises ``ValueError`` on a bare
        # ``{`` or ``}`` (i.e. a single brace that is neither part of
        # ``{name}`` nor an escaped ``{{`` / ``}}`` sequence).
        raise PromptTemplateError(
            f"unbalanced or unescaped brace in prompt template: {exc}"
        ) from exc

    for literal_text, field_name, format_spec, conversion in parsed:
        # ``literal_text`` already has ``{{`` / ``}}`` collapsed to
        # ``{`` / ``}`` by the parser, so any remaining unbalanced
        # brace would have raised above; nothing to validate here.
        _ = literal_text
        # ``conversion`` syntax (``!r``/``!s``/``!a``) is validated by
        # ``string.Formatter.parse`` itself; nothing further to check.
        _ = conversion

        if field_name is None:
            # Pure literal segment with no placeholder. ``format_spec``
            # and ``conversion`` are guaranteed to be ``None`` here.
            continue

        # Reject auto-numbered (``{}``) and positional (``{0}``)
        # placeholders: prompt rendering uses keyword arguments only,
        # so positional references are always a bug.
        if field_name == "" or field_name.isdigit():
            raise PromptTemplateError(
                "positional placeholder is not allowed in prompt template; "
                "use a named variable from "
                f"{sorted(KNOWN_TEMPLATE_VARS)!r}"
            )

        # Strip attribute / item access (``{vars.attr}`` /
        # ``{vars[0]}``) so we validate the *root* identifier against
        # the known set. ``string.Formatter`` exposes the raw field
        # text; the root is whatever precedes the first ``.`` or
        # ``[``.
        root = _root_name(field_name)
        if root not in KNOWN_TEMPLATE_VARS:
            raise PromptTemplateError(f"unknown placeholder: {{{field_name}}}")

        # Validate any nested ``{...}`` inside a format spec
        # (``{name:{width}}``). ``string.Formatter`` does not recurse
        # into format specs automatically, so we re-parse manually.
        if format_spec:
            try:
                nested = list(_FORMATTER.parse(format_spec))
            except ValueError as exc:
                raise PromptTemplateError(
                    "unbalanced or unescaped brace in format spec for "
                    f"{{{field_name}}}: {exc}"
                ) from exc
            for _lit, nested_field, _spec, _conv in nested:
                if nested_field is None:
                    continue
                nested_root = _root_name(nested_field)
                if nested_root and nested_root not in KNOWN_TEMPLATE_VARS:
                    raise PromptTemplateError(
                        f"unknown placeholder: {{{nested_field}}}"
                    )


def _root_name(field_name: str) -> str:
    """Return the root identifier of a ``str.format`` field reference.

    ``"department_repos"``                     ``"department_repos"``
    ``"department_repos[0]"``                  ``"department_repos"``
    ``"department_repos.something"``           ``"department_repos"``
    """

    root = field_name
    for sep in (".", "["):
        idx = root.find(sep)
        if idx != -1:
            root = root[:idx]
    return root
