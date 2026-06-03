"""Prompt loader / renderer exception hierarchy.

These exceptions are raised by :mod:`prompts.loader`,
:mod:`prompts.validate` and the prompt-git router when a prompt body
or render call violates the prompt contract.

The base class :class:`PromptError` is provided so callers that only
care about prompt-related failures (eg. the assistant-service chat
handler, which converts them to user-visible SSE errors) can catch
one type. The :class:`PromptTemplateError` and
:class:`PromptNotFoundError` subclasses preserve the precise reason
for audit.
"""

from __future__ import annotations


class PromptError(Exception):
    """Base class for every prompt-loader related failure."""


class PromptNotFoundError(PromptError, FileNotFoundError):
    """Raised when ``PromptLoader.load(name)`` cannot resolve ``name``.

    Inherits from :class:`FileNotFoundError` so callers using the
    standard library exception will also catch it; this is purely an
    ergonomic decision for migration code paths.
    """


class PromptTemplateError(PromptError, ValueError):
    """Raised when a prompt template is malformed.

    Triggers:

    * ``body.format(**vars)`` raises :class:`KeyError` — the prompt
      references a placeholder that is not part of the
      :class:`prompts.types.PromptVars` contract.
    * Unbalanced ``{`` or ``}`` in the body — the user forgot to
      escape a literal brace as ``{{`` / ``}}``.

    Inherits from :class:`ValueError` so generic exception handlers
    that already special-case ``ValueError`` (eg. FastAPI request
    validation) can catch it without explicit imports.
    """
