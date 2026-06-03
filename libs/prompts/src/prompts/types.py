"""Prompt template variable contract — :class:`PromptVars` value object.

The schema defines the prompt variables used by chat rendering:

    THE Prompt_Loader SHALL prompt template variable injection'ı şu
    zorunlu alanlarla yapar: ``{department_id}``, ``{department_repos}``,
    ``{capabilities}``, ``{default_language}``, ``{bot_username}``.

This module ships the **contract** half of the rendering layer: a
frozen, hashable dataclass that pins the exact set of placeholders
every prompt body may reference, plus the ``inject_template_vars``
helper that wraps ``str.format(**asdict(vars))`` so callers never
hand-roll the substitution call.

Rationale
---------

* ``frozen=True`` — :class:`PromptVars` flows through hot-reload,
  audit and SSE pipelines as a value object. Freezing it prevents an
  accidental in-place mutation between the moment the prompt is
  rendered and the moment its ``prompt_version`` lands in the audit
  payload.
* ``slots=True`` — keeps the per-render allocation cheap; the chat
  handler instantiates one ``PromptVars`` per SSE message.
* Immutable container types (``tuple``, ``frozenset``) on the two
  collection fields — ``frozen=True`` only protects the dataclass'
  *attribute bindings*, not nested mutables. Using immutable
  collections makes the whole value transitively hashable so it can
  appear in caches and logs without surprises.
* ``Literal["tr", "en"]`` on ``default_language`` mirrors the
  ``departments.json`` schema field; a typo at the call site is
  caught at type-check time rather than only when the LLM produces
  an unexpected reply.
* No defaults — every placeholder is
  mandatory. Forcing callers to populate each field explicitly is
  the cheapest way to keep the rendered system prompt deterministic
  across departments.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final, Literal

__all__ = [
    "PromptLanguage",
    "PromptVars",
    "TEMPLATE_VARIABLE_NAMES",
    "inject_template_vars",
    "_PromptEntry",
]


# ---------------------------------------------------------------------------
# Enum-like literals
# ---------------------------------------------------------------------------

#: The two languages a prompt may default to. Mirrors the
#: ``default_language`` field on ``departments.json`` and the
#: ``Literal["tr", "en"]`` annotation on :class:`PromptVars` — kept as
#: a named alias so the rendering pipeline and any downstream
#: validators can reference the same vocabulary.
PromptLanguage = Literal["tr", "en"]


#: The exact set of mandatory template variables. Used by
#: :func:`inject_template_vars` (and
#: :func:`validate_template_format`) to reject prompt bodies that
#: reference an unknown placeholder. Kept as a ``frozenset`` so it
#: cannot drift at runtime.
TEMPLATE_VARIABLE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "department_id",
        "department_repos",
        "capabilities",
        "default_language",
        "bot_username",
    }
)


# ---------------------------------------------------------------------------
# PromptVars
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptVars:
    """Frozen value object carrying the five mandatory template vars.

    A :class:`PromptVars` instance is the only thing the rendering
    layer accepts — :class:`PromptLoader.render` calls
    :func:`inject_template_vars` with this dataclass, and
    :func:`validate_template_format` cross-checks every
    placeholder discovered in a prompt body against
    :data:`TEMPLATE_VARIABLE_NAMES` (which is derived from the field
    names declared here). The contract is intentionally narrow so a
    new placeholder requires a code change in this single module.

    Args:
        department_id: Stable id of the department the prompt is
            being rendered for (eg. ``"payment"``). Mirrors the
            ``departments.json`` ``id`` column.
        department_repos: The repositories owned by the department,
            in declaration order. Stored as a ``tuple`` so the value
            is hashable and cannot be mutated between render and
            audit.
        capabilities: The capability set granted to the department
            (subset of ``{"jira", "bitbucket", "confluence",
            "execution", "web_search"}``). Stored as a
            ``frozenset`` so prompt bodies that ``join`` or iterate
            the value see a deterministic — though intentionally
            unordered — collection.
        default_language: ``"tr"`` or ``"en"``. Drives the LLM's
            reply locale; a typo here would silently switch the
            user's experience, so the type system pins the closed
            vocabulary.
        bot_username: The bot account username surfaced in
            user-facing prompts (eg. ``"bot.payment"``). Used by the
            assistant chat system prompt to refer to itself in the
            third person when guiding the user toward Task Creator
            .
    """

    department_id: str
    department_repos: tuple[str, ...]
    capabilities: frozenset[str]
    default_language: PromptLanguage
    bot_username: str


# ---------------------------------------------------------------------------
# Rendering helper
# ---------------------------------------------------------------------------


def inject_template_vars(body: str, vars: PromptVars) -> str:
    """Substitute the five mandatory placeholders into ``body``.

    Thin wrapper around ``body.format(**dataclasses.asdict(vars))``
    that exists for two reasons:

    1. **Single render entry-point.** :class:`PromptLoader` calls
       this helper instead of ``str.format`` directly, so the
       validator and any future template engine
       swap only have to touch one site.
    2. **Audit clarity.** Callers always pass a typed
       :class:`PromptVars` rather than an arbitrary mapping, which
       keeps the payload that lands on the
       ``audit_events.payload.prompt_vars`` field shaped exactly the
       expected way.

    The helper does **not** raise a custom error type yet — that is
    ``PromptTemplateError`` lives in ``prompts.validate``. Until then, a missing placeholder
    propagates the underlying ``KeyError`` from ``str.format``, which
    is the behaviour the loader expects to catch and convert.

    Args:
        body: Raw prompt body — typically the contents of a
            ``prompts/<name>.md`` file. Curly-brace literals must be
            escaped as ``{{`` / ``}}``;
            ``validate_template_format`` enforces this at boot.
        vars: The fully-populated :class:`PromptVars` value object.

    Returns:
        The rendered prompt body with every ``{<name>}`` placeholder
        replaced by the corresponding attribute on ``vars``.

    Raises:
        KeyError: If ``body`` references a placeholder name that is
            not a field on :class:`PromptVars`. The caller
            converts this to ``PromptTemplateError`` so the CI gate
            fails fast.
    """

    return body.format(**asdict(vars))


# ---------------------------------------------------------------------------
# Cache row — internal to PromptLoader
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PromptEntry:
    """Internal cache row used by :class:`prompts.loader.PromptLoader`.

    Captures the three pieces of information ``PromptLoader`` keeps
    per cached prompt:

    * ``body`` — the raw markdown read from disk.
    * ``mtime`` — last-modification timestamp used by the 30-second
      hot-reload poll.
    * ``git_hash`` — short commit hash that produced the body, written
      to the audit row as ``prompt_version``. Falls
      back to ``"unknown"`` when ``git`` is unavailable.

    The leading underscore marks it as package-private; callers
    outside :mod:`prompts` should never construct one directly.
    """

    body: str
    mtime: float
    git_hash: str
