"""Behavioral test for Standalone Mode preconditions.

Every Component declared in :data:`COMPONENT_MANIFEST` must satisfy the
filesystem-level invariants that make Standalone Mode viable from inside
the Component's own directory:

1. **README.md exists** under ``<c.path>/`` and contains *some* heading
   whose normalized text equals ``standalone build & run``.
   Normalization strips leading ``#`` markers and surrounding
   whitespace and lowercases the result, so any of
   ``## Standalone build & run``, ``### Standalone Build & Run``,
   ``# standalone build & run`` etc. are accepted.
2. **.dockerignore exists** under ``<c.path>/`` so the build context
   stays small and never bleeds in ``.env`` files.
3. **No ``COPY ../...`` directives** escape the Component's own
   directory in its ``Dockerfile``. ``COPY --from=<stage>`` lines
   reference earlier build stages (not the parent filesystem) and are
   therefore exempt; only "naked" ``COPY`` directives whose source
   path contains a ``..`` segment fail this check.
4. **Root ``.gitignore`` matches ``.env``**, i.e. it contains a
   pattern such as ``*.env`` or ``/.env`` that prevents real
   ``.env`` files from being committed.

The fourth invariant is *workspace-wide* (independent of the
Component sample) but is naturally validated alongside the
per-Component checks because a missing root-level guard would defeat
Standalone Mode for every Component at once.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by other property tests).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import COMPONENT_MANIFEST, WORKSPACE_ROOT, ComponentSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers - README heading normalization, COPY scanner, .gitignore check
# ---------------------------------------------------------------------------


_REQUIRED_HEADING_NORMALIZED: str = "standalone build & run"

# ATX headings: any number of leading ``#`` markers followed by at
# least one space, then the heading text. Setext headings (``===``
# / ``---`` underlines) are not used by the project's READMEs and
# are intentionally not handled here.
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$")


def _normalize_heading(raw: str) -> str:
    """Return the lower-case, whitespace-collapsed heading text.

    The input is the captured *content* of an ATX heading (i.e. the
    text after the leading ``#`` markers, with optional trailing
    ``#`` characters already stripped by the regex). We additionally
    collapse runs of internal whitespace so a heading written as
    ``Standalone   build  &   run`` still normalizes to the canonical
    ``standalone build & run`` token.
    """

    return re.sub(r"\s+", " ", raw.strip()).lower()


def _has_standalone_heading(readme_text: str) -> bool:
    """True when at least one heading normalizes to the required token."""

    for line in readme_text.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if _normalize_heading(match.group(2)) == _REQUIRED_HEADING_NORMALIZED:
            return True
    return False


# A ``COPY`` directive that is NOT a ``COPY --from=<stage>`` (i.e. one
# that copies from the build context) and whose body contains a ``..``
# segment. The project's Dockerfiles are line-oriented so a single-
# line regex is sufficient; multi-line ``COPY`` continuations are not
# used. ``--from=`` may carry an arbitrary stage name including digits
# and hyphens, which is why we match it loosely.
_COPY_NAKED_DOTDOT_RE = re.compile(
    r"^\s*COPY\s+(?!--from=)(?P<rest>.*\.\..*?)$",
    re.IGNORECASE,
)

# Matches the standalone token ``..`` between path separators or at
# string boundaries. Used to confirm that a ``..`` occurrence really
# is a parent-directory escape (e.g. ``../foo``) rather than a token
# embedded inside another identifier (e.g. ``foo..bar`` - not a valid
# path segment but we want the test to be conservative).
_PARENT_ESCAPE_RE = re.compile(r"(?:^|[\s/=])\.\.(?:[\s/]|$)")


def _scan_dockerfile_for_parent_copies(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, line)`` pairs for COPY directives escaping ``..``.

    Only ``COPY`` lines that do NOT carry a ``--from=`` flag are
    considered; ``COPY --from=builder ...`` references the previous
    build stage's filesystem (not the parent of the build context) and
    is permitted.
    """

    findings: list[tuple[int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        match = _COPY_NAKED_DOTDOT_RE.match(raw)
        if match is None:
            continue
        # Defensive second pass: only flag the line if the ``..`` lies
        # in a position that actually escapes the build context. Tokens
        # like ``foo..bar`` (not valid paths) are conservatively
        # ignored because they do not constitute a real escape.
        if _PARENT_ESCAPE_RE.search(match.group("rest")):
            findings.append((line_no, raw.rstrip()))
    return findings


# Patterns that, on their own, neutralise ``.env`` commits at the
# repository root. ``*.env`` matches every ``*.env`` file in any
# directory; ``.env`` (or the leading-slash variant ``/.env``) matches
# the root-level file specifically. Any one of these is sufficient.
_GITIGNORE_ENV_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\*\.env\s*$"),
    re.compile(r"^\s*/?\.env\s*$"),
)


def _gitignore_blocks_env(gitignore_text: str) -> bool:
    """True when the ``.gitignore`` text matches a recognised .env rule."""

    for raw in gitignore_text.splitlines():
        # Skip negations (``!.env.example`` etc.) and comments - they
        # are exemptions, not the protective rules we're looking for.
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        for pattern in _GITIGNORE_ENV_PATTERNS:
            if pattern.match(raw):
                return True
    return False


# ---------------------------------------------------------------------------
# Standalone mode preconditions
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_standalone_mode_preconditions(component: ComponentSpec) -> None:
    """Standalone Mode preconditions for every Component."""

    component_root: Path = WORKSPACE_ROOT / component.path

    # ------------------------------------------------------------------
    # Clause 1: README.md exists with the required heading.
    # ------------------------------------------------------------------
    readme_path = component_root / "README.md"
    assert readme_path.is_file(), (
        f"{component.name}: missing README.md at "
        f"{readme_path.relative_to(WORKSPACE_ROOT)} "
        f"(standalone README heading required)"
    )
    readme_text = readme_path.read_text(encoding="utf-8")
    assert _has_standalone_heading(readme_text), (
        f"{component.name}: README.md must contain a heading whose "
        f"normalized text equals {_REQUIRED_HEADING_NORMALIZED!r} "
        f"(standalone README heading required); inspected "
        f"{readme_path.relative_to(WORKSPACE_ROOT)}"
    )

    # ------------------------------------------------------------------
    # Clause 2: .dockerignore exists.
    # ------------------------------------------------------------------
    dockerignore_path = component_root / ".dockerignore"
    assert dockerignore_path.is_file(), (
        f"{component.name}: missing .dockerignore at "
        f"{dockerignore_path.relative_to(WORKSPACE_ROOT)} "
        f"(.dockerignore required)"
    )

    # ------------------------------------------------------------------
    # Clause 3: no naked ``COPY ../...`` lines escape the Component's
    # own directory. ``COPY --from=<stage>`` lines
    # reference earlier build stages and are explicitly allowed.
    # ------------------------------------------------------------------
    dockerfile_path = component_root / "Dockerfile"
    assert dockerfile_path.is_file(), (
        f"{component.name}: missing Dockerfile at "
        f"{dockerfile_path.relative_to(WORKSPACE_ROOT)} "
        f"(Dockerfile required)"
    )
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    parent_copies = _scan_dockerfile_for_parent_copies(dockerfile_text)
    assert not parent_copies, (
        f"{component.name}: Dockerfile contains COPY directive(s) that "
        f"escape the Component directory via '..': "
        f"{parent_copies}"
    )

    # ------------------------------------------------------------------
    # Clause 4: root .gitignore matches the .env pattern.
    # ------------------------------------------------------------------
    root_gitignore = WORKSPACE_ROOT / ".gitignore"
    assert root_gitignore.is_file(), (
        f"missing root .gitignore at "
        f"{root_gitignore.relative_to(WORKSPACE_ROOT)} (.env guard required)"
    )
    gitignore_text = root_gitignore.read_text(encoding="utf-8")
    assert _gitignore_blocks_env(gitignore_text), (
        f"root .gitignore must contain a pattern such as '*.env' or "
        f"'/.env' to block real .env commits; inspected "
        f"{root_gitignore.relative_to(WORKSPACE_ROOT)}"
    )
