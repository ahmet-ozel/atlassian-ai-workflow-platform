"""Property test for Dockerfile invariants.

Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 12.4
Property 6: Dockerfile invariants.

For every Component ``c`` in :data:`COMPONENT_MANIFEST`, the Dockerfile
shipped under ``<c.path>/Dockerfile`` must satisfy *all* of these
shape invariants (design §6.3 → Property 6):

1. **Multi-stage** (Req 9.2): ≥ 2 ``FROM`` directives, with the first
   stage explicitly named ``builder`` (``FROM <image> AS builder``).
2. **Base image matches runtime** (Req 9.3, 9.4):
   - ``c.runtime == "python"`` → every ``FROM`` references
     ``python:3.12-slim`` (or a ``python:3.12.x-slim`` patch variant).
   - ``c.runtime == "node"`` → every ``FROM`` references a
     ``node:20-*`` LTS variant.
3. **Non-root runtime** (Req 9.5): the runtime stage contains either
   ``useradd -u 10001 ... appuser`` or ``adduser -D -u 10001 ... appuser``
   (the Alpine equivalent), and a ``USER appuser`` directive that
   appears **after** the user-creation ``RUN`` line and **before** any
   final ``CMD`` / ``ENTRYPOINT`` directive.
4. **EXPOSE matches port mode** (Req 9.8): ``EXPOSE <c.container_port>``
   is present *iff* ``c.type ∈ {http_service, ui_component}``; for
   ``temporal_worker`` Components no ``EXPOSE`` directive may appear
   (Req 3.3, design §"Healthcheck shape").
5. **Healthcheck shape** (Req 9.6, 9.7, 12.4):
   - ``c.type == "http_service"``: ``HEALTHCHECK`` command parses to
     ``curl -fsS http://localhost:<c.container_port>/healthz``.
   - ``c.type == "temporal_worker"``: ``HEALTHCHECK`` command is a
     ``python -c "..."`` one-liner that imports
     ``temporalio.client.Client`` and calls ``Client.connect(...)``.
   - ``c.type == "ui_component"``: a ``HEALTHCHECK`` directive must
     exist but the probe path may differ from ``/healthz`` because
     Streamlit and Next.js expose framework-native health endpoints
     (``/_stcore/health`` and ``/api/health`` respectively). The
     scaffold's choice is documented in each Dockerfile's header
     comment; this test relaxes the path clause for UI Components only.

Implementation notes
--------------------

The Dockerfile grammar is intentionally line-oriented for the subset
the scaffold uses (no parser-comments, no `\\` continuations inside
the directives we inspect aside from ``RUN`` blocks). A small regex
tokenizer is sufficient and avoids pulling in ``dockerfile-parse``
(which is listed in ``tests/requirements.txt`` as optional). Multi-
line ``RUN`` blocks are flattened by stitching trailing-backslash
continuations before scanning for ``useradd`` / ``adduser`` patterns.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by ``test_path_coverage``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import COMPONENT_MANIFEST, WORKSPACE_ROOT, ComponentSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Dockerfile tokenizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Directive:
    """A single Dockerfile directive after line-continuation stitching.

    ``line_no`` is the 1-based number of the *first* physical line of
    the directive (used to assert ordering between user creation and
    ``USER appuser`` / ``CMD``).
    """

    line_no: int
    keyword: str  # upper-cased, e.g. "FROM", "RUN", "USER"
    body: str  # everything after the keyword, with continuations stitched


# Matches an instruction keyword at the start of a logical line.
# Dockerfile keywords are case-insensitive but conventionally upper.
_KEYWORD_RE = re.compile(
    r"^\s*("
    r"FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|"
    r"VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL"
    r")\s+(.*)$",
    re.IGNORECASE,
)


def _tokenize(dockerfile_text: str) -> tuple[_Directive, ...]:
    """Return the ordered list of directives in ``dockerfile_text``.

    Comment-only and blank lines are skipped. ``RUN``/``HEALTHCHECK``
    bodies that span multiple physical lines via trailing ``\\`` are
    stitched together so the scanner sees a single logical body. The
    returned ``line_no`` is the 1-based line of the directive's first
    physical line.
    """

    directives: list[_Directive] = []
    physical_lines = dockerfile_text.splitlines()

    i = 0
    while i < len(physical_lines):
        raw = physical_lines[i]
        stripped = raw.strip()
        # Skip blanks and comments (Dockerfile parser-directives like
        # ``# syntax=...`` are also ignored — they are not instructions).
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        match = _KEYWORD_RE.match(raw)
        if not match:
            # Defensive: a non-keyword, non-comment, non-blank line
            # would indicate a malformed Dockerfile. Skip rather than
            # raise so the test surfaces *which* invariant fails.
            i += 1
            continue

        keyword = match.group(1).upper()
        body = match.group(2)
        first_line_no = i + 1  # 1-based

        # Stitch trailing-backslash continuations.
        while body.rstrip().endswith("\\"):
            body = body.rstrip()[:-1].rstrip()
            i += 1
            if i >= len(physical_lines):
                break
            cont = physical_lines[i]
            # Strip leading whitespace and any leading comment lines
            # *inside* a continuation block (Docker allows ``# ...``
            # inside a RUN, but the scaffold does not use this).
            cont_stripped = cont.strip()
            if cont_stripped.startswith("#"):
                continue
            body = (body + " " + cont_stripped).strip()

        directives.append(_Directive(first_line_no, keyword, body))
        i += 1

    return tuple(directives)


# ---------------------------------------------------------------------------
# Per-clause assertion helpers
# ---------------------------------------------------------------------------


_FROM_AS_RE = re.compile(
    r"^\s*(?P<image>\S+)(?:\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_-]*))?\s*$",
    re.IGNORECASE,
)


def _assert_multi_stage(
    component: ComponentSpec, directives: tuple[_Directive, ...]
) -> list[_Directive]:
    """Property 6.1 — ≥2 ``FROM`` and the first stage is named ``builder``.

    Returns the ordered list of ``FROM`` directives so subsequent
    clauses (base-image match) can reuse the parsed image strings.
    """

    froms = [d for d in directives if d.keyword == "FROM"]
    assert len(froms) >= 2, (
        f"{component.name}: Dockerfile must have ≥ 2 FROM directives "
        f"(multi-stage build, Req 9.2); found {len(froms)}"
    )

    first_match = _FROM_AS_RE.match(froms[0].body)
    assert first_match is not None, (
        f"{component.name}: first FROM directive does not parse: "
        f"{froms[0].body!r}"
    )
    first_alias = (first_match.group("alias") or "").lower()
    assert first_alias == "builder", (
        f"{component.name}: first FROM stage must be named 'builder' "
        f"(Req 9.2); got alias={first_match.group('alias')!r}"
    )
    return froms


_PYTHON_BASE_RE = re.compile(r"^python:3\.12(?:\.\d+)?-slim$")
_NODE_BASE_RE = re.compile(r"^node:20-[A-Za-z0-9_.-]+$")


def _assert_base_image(component: ComponentSpec, froms: list[_Directive]) -> None:
    """Property 6.3 — base image matches ``c.runtime`` (Req 9.3 / 9.4)."""

    if component.runtime == "python":
        expected_re = _PYTHON_BASE_RE
        expected_human = "python:3.12-slim (or python:3.12.x-slim)"
    elif component.runtime == "node":
        expected_re = _NODE_BASE_RE
        expected_human = "node:20-* (LTS)"
    else:
        raise AssertionError(
            f"{component.name}: unknown runtime kind {component.runtime!r}"
        )

    for from_directive in froms:
        match = _FROM_AS_RE.match(from_directive.body)
        assert match is not None, (
            f"{component.name}: malformed FROM at line "
            f"{from_directive.line_no}: {from_directive.body!r}"
        )
        image = match.group("image")
        assert expected_re.match(image), (
            f"{component.name}: FROM image {image!r} at line "
            f"{from_directive.line_no} does not match {expected_human} "
            f"(Req 9.{'3' if component.runtime == 'python' else '4'})"
        )


_USERADD_RE = re.compile(r"\buseradd\b[^\\\n]*-u\s+10001\b[^\\\n]*\bappuser\b")
_ADDUSER_RE = re.compile(r"\badduser\b[^\\\n]*-D\b[^\\\n]*-u\s+10001\b[^\\\n]*\bappuser\b")
_USER_DIRECTIVE_RE = re.compile(r"^\s*appuser\s*$")


def _assert_non_root_user(
    component: ComponentSpec, directives: tuple[_Directive, ...]
) -> None:
    """Property 6.2 — non-root ``appuser`` (uid 10001) and ``USER appuser``.

    Asserts that:
    * Some ``RUN`` directive creates ``appuser`` with uid 10001 via
      ``useradd -u 10001 ... appuser`` or the Alpine
      ``adduser -D -u 10001 ... appuser`` equivalent.
    * A ``USER appuser`` directive appears *after* that ``RUN`` and
      *before* any ``CMD`` / ``ENTRYPOINT`` directive.
    """

    user_create_line: int | None = None
    for directive in directives:
        if directive.keyword != "RUN":
            continue
        if _USERADD_RE.search(directive.body) or _ADDUSER_RE.search(directive.body):
            user_create_line = directive.line_no
            break

    assert user_create_line is not None, (
        f"{component.name}: runtime stage must create non-root 'appuser' "
        f"with uid 10001 via 'useradd -u 10001 ... appuser' or "
        f"'adduser -D -u 10001 ... appuser' (Req 9.5)"
    )

    user_directive_line: int | None = None
    for directive in directives:
        if directive.keyword != "USER":
            continue
        if _USER_DIRECTIVE_RE.match(directive.body):
            user_directive_line = directive.line_no
            break

    assert user_directive_line is not None, (
        f"{component.name}: Dockerfile must contain 'USER appuser' "
        f"directive (Req 9.5)"
    )
    assert user_directive_line > user_create_line, (
        f"{component.name}: 'USER appuser' (line {user_directive_line}) "
        f"must appear AFTER user creation (line {user_create_line}); "
        f"Req 9.5"
    )

    # Must precede the final CMD/ENTRYPOINT.
    for directive in directives:
        if directive.keyword in {"CMD", "ENTRYPOINT"}:
            assert user_directive_line < directive.line_no, (
                f"{component.name}: 'USER appuser' (line "
                f"{user_directive_line}) must appear BEFORE "
                f"{directive.keyword} (line {directive.line_no}); Req 9.5"
            )


def _assert_expose(
    component: ComponentSpec, directives: tuple[_Directive, ...]
) -> None:
    """Property 6.4 — EXPOSE iff Component publishes a port (Req 9.8)."""

    expose_directives = [d for d in directives if d.keyword == "EXPOSE"]

    if component.type in {"http_service", "ui_component"}:
        assert component.container_port is not None, (
            f"{component.name}: HTTP/UI Component manifest must declare "
            f"container_port"
        )
        port_str = str(component.container_port)
        # Body may contain multiple ports or a protocol suffix
        # (``8080/tcp``); we accept either form provided the integer
        # matches the manifest port. Split on whitespace + slash to
        # extract the port token.
        ports_seen: list[str] = []
        for d in expose_directives:
            for token in d.body.split():
                ports_seen.append(token.split("/", 1)[0])
        assert port_str in ports_seen, (
            f"{component.name}: missing EXPOSE {port_str} (Req 9.8); "
            f"saw EXPOSE bodies={[d.body for d in expose_directives]!r}"
        )
    else:  # temporal_worker
        assert not expose_directives, (
            f"{component.name}: Temporal worker MUST NOT declare any "
            f"EXPOSE directive (Req 3.3 / 9.8); got "
            f"{[d.body for d in expose_directives]!r}"
        )


_HEALTHCHECK_CMD_RE = re.compile(
    r"^\s*(?:--\S+\s+)*CMD\s+(?P<cmd>.+)$", re.IGNORECASE
)


def _extract_healthcheck_cmd(directive: _Directive) -> str:
    """Strip ``[--<flag>=<v> ...] CMD`` prefix and return the raw command."""

    match = _HEALTHCHECK_CMD_RE.match(directive.body)
    assert match is not None, (
        f"HEALTHCHECK body did not parse: {directive.body!r}"
    )
    return match.group("cmd").strip()


def _assert_healthcheck(
    component: ComponentSpec, directives: tuple[_Directive, ...]
) -> None:
    """Property 6.5 — healthcheck shape per Component type.

    HTTP services use ``curl -fsS http://localhost:<port>/healthz``;
    temporal workers use a ``python -c "..."`` Temporal-client probe;
    UI components must have *some* ``HEALTHCHECK`` directive but the
    probe path may differ (see module docstring).
    """

    healthchecks = [d for d in directives if d.keyword == "HEALTHCHECK"]
    assert healthchecks, (
        f"{component.name}: Dockerfile must declare a HEALTHCHECK "
        f"(Req 9.6/9.7, 12.4)"
    )
    # Use the *last* HEALTHCHECK directive — Docker only honours one,
    # and the scaffold ships exactly one per Component.
    cmd = _extract_healthcheck_cmd(healthchecks[-1])

    if component.type == "http_service":
        port = component.container_port
        assert port is not None, (
            f"{component.name}: HTTP service must declare container_port"
        )
        # Accept ``curl ... -fsS ... http://localhost:<port>/healthz``
        # in any flag order; the scaffold uses ``-fsS`` as a single
        # token but we relax to ``-f``/``-s``/``-S`` in any combination
        # to keep the test robust against future minor cleanups.
        url_pattern = rf"http://localhost:{port}/healthz"
        assert re.search(r"\bcurl\b", cmd), (
            f"{component.name}: HTTP service HEALTHCHECK must invoke "
            f"curl (Req 9.6); got {cmd!r}"
        )
        assert re.search(r"-fsS\b|-fSs\b|-Sfs\b|-sfS\b|-fs\b.*-S\b|-f\b.*-s\b.*-S\b", cmd), (
            f"{component.name}: HTTP service HEALTHCHECK curl must use "
            f"'-fsS' flags (Req 9.6); got {cmd!r}"
        )
        assert re.search(url_pattern, cmd), (
            f"{component.name}: HEALTHCHECK URL must be "
            f"http://localhost:{port}/healthz (Req 9.6); got {cmd!r}"
        )

    elif component.type == "temporal_worker":
        # Worker probe: ``python -c "... from temporalio.client import
        # Client ... Client.connect(...) ..."`` (Req 9.7, 12.4).
        assert re.search(r"\bpython\b\s+-c\b", cmd), (
            f"{component.name}: worker HEALTHCHECK must run a "
            f"'python -c' one-liner (Req 9.7, 12.4); got {cmd!r}"
        )
        assert re.search(r"\btemporalio\.client\b", cmd) or re.search(
            r"\bfrom\s+temporalio\.client\s+import\s+Client\b", cmd
        ), (
            f"{component.name}: worker HEALTHCHECK must import the "
            f"temporalio.client.Client class (Req 9.7, 12.4); got {cmd!r}"
        )
        assert re.search(r"\bClient\.connect\b", cmd), (
            f"{component.name}: worker HEALTHCHECK must call "
            f"Client.connect(...) (Req 9.7, 12.4); got {cmd!r}"
        )

    elif component.type == "ui_component":
        # Streamlit (`/_stcore/health`) and Next.js (`/api/health`)
        # ship framework-native probes. We require *a* HEALTHCHECK
        # invoking *some* HTTP probe tool against ``localhost``; the
        # exact path varies by framework and is documented in each
        # Dockerfile's header comment.
        assert re.search(r"\b(curl|wget)\b", cmd), (
            f"{component.name}: UI HEALTHCHECK must use curl or wget "
            f"(Req 9.6); got {cmd!r}"
        )
        port = component.container_port
        assert port is not None, (
            f"{component.name}: UI Component must declare container_port"
        )
        assert re.search(rf"http://localhost:{port}/", cmd), (
            f"{component.name}: UI HEALTHCHECK must probe "
            f"http://localhost:{port}/... (Req 9.6); got {cmd!r}"
        )
    else:
        raise AssertionError(
            f"{component.name}: unknown component type {component.type!r}"
        )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_dockerfile_invariants(component: ComponentSpec) -> None:
    """Property 6 — Dockerfile shape invariants for every Component.

    Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 12.4.
    """

    dockerfile_path: Path = WORKSPACE_ROOT / component.path / "Dockerfile"
    assert dockerfile_path.is_file(), (
        f"{component.name}: missing Dockerfile at "
        f"{dockerfile_path.relative_to(WORKSPACE_ROOT)}"
    )

    text = dockerfile_path.read_text(encoding="utf-8")
    directives = _tokenize(text)

    froms = _assert_multi_stage(component, directives)
    _assert_base_image(component, froms)
    _assert_non_root_user(component, directives)
    _assert_expose(component, directives)
    _assert_healthcheck(component, directives)
