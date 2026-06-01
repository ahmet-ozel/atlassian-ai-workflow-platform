"""prompts: git-aware file-backed prompt loader.

Re-exports the public API of the package so callers can simply do::

    from prompts import PromptLoader, PromptVars, PromptTemplateError
    from prompts import inject_template_vars, validate_template_format

The package mirrors the design in
``.kiro/specs/platform-mimari-ops/design.md`` §`PromptLoader` and
satisfies Requirements 2.5 (hot-reload), 2.6 (``prompt_version`` =
git short hash), 2.7 (template variable injection) and 2.9 (template
format escape).
"""

from .errors import (
    PromptError,
    PromptNotFoundError,
    PromptTemplateError,
)
from .loader import PromptLoader
from .types import (
    TEMPLATE_VARIABLE_NAMES,
    PromptLanguage,
    PromptVars,
    inject_template_vars,
)
from .validate import KNOWN_TEMPLATE_VARS, validate_template_format

__all__ = [
    "KNOWN_TEMPLATE_VARS",
    "PromptError",
    "PromptLanguage",
    "PromptLoader",
    "PromptNotFoundError",
    "PromptTemplateError",
    "PromptVars",
    "TEMPLATE_VARIABLE_NAMES",
    "inject_template_vars",
    "validate_template_format",
]
