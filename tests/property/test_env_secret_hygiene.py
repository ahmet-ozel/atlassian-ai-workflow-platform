"""invariant for `.env.example` secret hygiene.


invariant: No real secrets in any ``.env.example``.

Every ``.env.example`` file (root +
component-level) MUST contain only placeholders, dev-only credentials
or structurally-bounded values:

For every ``.env.example`` file ``e`` in the workspace and for
every assignment line ``KEY=VALUE`` in ``e``, *at least one* of the
following SHALL hold:

1. ``VALUE`` is empty.
2. ``VALUE`` matches a placeholder allowlist regex set
 (placeholder tokens, URLs, booleans, integers, file paths,
 kebab/snake identifiers, ``vault:`` references, ``host:port`` pairs,
 or comment-only lines).
3. ``VALUE`` is a known dev-only credential explicitly allowed by
 this project (``ai_dev_only``, ``miniosecret_dev_only``,
 ``dev-token-not-for-prod``).

For every assignment line, ``VALUE`` SHALL NOT match any of the
denylist patterns: a 32+ character base64-looking blob, a JWT-looking
prefix (``eyJ``), an ``sk-`` or ``glpat-`` provider-key prefix or a
UUID. Equivalently, every line passes both an allowlist match and a
denylist non-match.

The test is parameterised over ``(file, line_number, key, value)``
tuples discovered by globbing the workspace; each tuple becomes a
distinct pytest test ID, so when a regression slips a real secret into
an example file the failing case names the offending file, line and
key directly rather than producing a Hypothesis shrink trace.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# ``conftest.py`` lives one directory up; pytest auto-loads it, but we
# add ``tests/`` to ``sys.path`` defensively so this file works under
# direct ``python -m pytest tests/property`` invocations.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import COMPONENT_MANIFEST, WORKSPACE_ROOT  # noqa: E402


# ---------------------------------------------------------------------------
# Discovery: every ``.env.example`` shipped by the project
# ---------------------------------------------------------------------------


def _discover_env_example_files() -> tuple[Path, ...]:
    """Return the workspace's set of ``.env.example`` files.

 The set is the union of:

 * The workspace root ``.env.example``.
 * Every Component's local ``.env.example`` as
 declared in:data:`COMPONENT_MANIFEST`.

 The manifest is used (rather than a recursive glob) so the test
 fails loudly when a Component's example file goes missing - that
 failure mode is already covered by invariant, but enumerating
 via the manifest makes the secret-hygiene test resilient to
 accidental new ``.env.example`` files appearing under, for
 example, ``atlassian_mcp_bitbucket/``.
 """

    files: list[Path] = []
    root_example = WORKSPACE_ROOT / ".env.example"
    if root_example.exists():
        files.append(root_example)
    for component in COMPONENT_MANIFEST:
        candidate = WORKSPACE_ROOT / component.path / ".env.example"
        if candidate.exists():
            files.append(candidate)
    return tuple(files)


# ---------------------------------------------------------------------------
# Parsing: ``KEY=VALUE`` lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvLine:
    """A single ``KEY=VALUE`` assignment line in an ``.env.example`` file.

 Carries enough provenance to produce a clear pytest test ID and an
 informative assertion message (path relative to workspace root,
 1-based line number, key, raw value).
 """

    file_relpath: str
    line_number: int
    key: str
    value: str


# ``KEY=VALUE`` matches the dotenv subset used by the project:
# the key starts with a letter or underscore and may contain letters,
# digits, underscores; everything after the first ``=`` is the value
# (Compose's `.env` parser uses the same convention).
_ASSIGNMENT_RE: re.Pattern[str] = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)


def _parse_env_file(path: Path) -> tuple[EnvLine, ...]:
    """Yield every ``KEY=VALUE`` line in ``path``.

 Blank lines and comment-only lines (those whose first non-whitespace
 character is ``#``) are skipped - they cannot carry a secret.
 Trailing newline characters are stripped from the raw value but no
 inline-comment trimming is performed: the project does not write
 inline ``#...`` after assignments, and stripping them here would
 weaken the denylist (a real secret could otherwise hide behind a
 fake comment).
 """

    rel = str(path.relative_to(WORKSPACE_ROOT)).replace("\\", "/")
    text = path.read_text(encoding="utf-8")
    lines: list[EnvLine] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        # Drop the trailing carriage return that ``splitlines`` already
        # consumed on POSIX but keep the line's content otherwise.
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(raw_line)
        if not match:
            # Malformed assignment lines are flagged by the parsing
            # parametrize itself so the test maintainer notices the
            # syntax error rather than silently passing.
            lines.append(EnvLine(rel, idx, "<UNPARSEABLE>", raw_line))
            continue
        lines.append(
            EnvLine(
                file_relpath=rel,
                line_number=idx,
                key=match.group("key"),
                value=match.group("value"),
            )
        )
    return tuple(lines)


# ---------------------------------------------------------------------------
# Allowlist + denylist for example-file secret hygiene
# ---------------------------------------------------------------------------


#: Dev-only credentials explicitly allowed for local examples. These are
#: literal string matches (not regex) so every appearance is a known,
#: reviewed value rather than a structural pattern.
_KNOWN_DEV_CREDENTIALS: frozenset[str] = frozenset(
    {
        "ai_dev_only",
        "miniosecret_dev_only",
        "dev-token-not-for-prod",
    }
)


# Allowlist regex set. A value matches the allowlist when it is empty,
# is a known dev credential, or matches at least one of these regexes.
# The patterns are bounded (no ``.*``) so they cannot be padded with a
# real secret and still match.
_ALLOWLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Placeholder tokens used in example configuration.
    re.compile(r"^change-me(-[A-Za-z0-9_-]+)?$"),
    re.compile(r"^<set-by-vault>$"),
    re.compile(r"^dev-token-not-for-prod$"),
    # ``vault:`` reference path.
    re.compile(r"^vault:[A-Za-z0-9_/.\-]+$"),
    # URL with any scheme (http, https, postgresql, redis, etc.).
    # Bounded to a reasonable URL character set so it cannot swallow
    # arbitrary opaque blobs that happen to contain ``://``.
    re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[A-Za-z0-9_:./%@\-]+$"),
    # Booleans (case-insensitive - both ``true``/``false`` and
    # ``True``/``False`` styles are accepted by Compose).
    re.compile(r"^(sectioni:true|false)$"),
    # Pure integers (ports, retry counts, timeouts).
    re.compile(r"^[0-9]+$"),
    # Absolute POSIX file paths (e.g. ``/var/lib/...``).
    re.compile(r"^/[A-Za-z0-9_./\-]+$"),
    # Kebab / snake / dot identifiers - accepts both lowercase
    # (``mock``, ``automation-service``, ``agent-runner``) and the
    # uppercase log-level / env-name style (``INFO``, ``DEBUG``) and
    # version-style identifiers (``qwen2.5-coder``).
    re.compile(r"^[A-Za-z][A-Za-z0-9._\-]*$"),
    # ``host:port`` pairs (e.g. ``temporal:7233``, ``minio:9000``).
    re.compile(r"^[A-Za-z][A-Za-z0-9.\-]*:[0-9]+$"),
)


# Denylist regex set. A value MUST NOT match any of these patterns.
_DENYLIST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 32+ character base64-looking blob (HMAC keys, encoded tokens).
    (
        "base64-looking blob (32+ chars)",
        re.compile(r"^[A-Za-z0-9+/]{32,}={0,2}$"),
    ),
    # JWT prefix - the unpadded base64 encoding of ``{"`` is ``eyJ``,
    # which every JWT header starts with.
    (
        "JWT-looking value (eyJ...)",
        re.compile(r"^eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$"),
    ),
    # OpenAI-style API keys.
    (
        "OpenAI-style API key (sk-...)",
        re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$"),
    ),
    # GitLab Personal Access Tokens.
    (
        "GitLab PAT (glpat-...)",
        re.compile(r"^glpat-[A-Za-z0-9_\-]{16,}$"),
    ),
    # UUID v1-v5 (no braces, lowercase or uppercase hex).
    (
        "UUID",
        re.compile(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        ),
    ),
)


def _matches_allowlist(value: str) -> bool:
    """Return True iff ``value`` is empty, a known dev cred, or matches
 one of the allowlist regexes.
 """

    if value == "":
        return True
    if value in _KNOWN_DEV_CREDENTIALS:
        return True
    return any(pattern.match(value) for pattern in _ALLOWLIST_PATTERNS)


def _denylist_violation(value: str) -> str | None:
    """Return the human-readable name of the denylist pattern that
 ``value`` matches, or ``None`` if the value is clean.
 """

    for label, pattern in _DENYLIST_PATTERNS:
        if pattern.match(value):
            return label
    return None


# ---------------------------------------------------------------------------
# Parametrise: one test case per (file, line) ``KEY=VALUE`` assignment
# ---------------------------------------------------------------------------


def _collect_all_env_lines() -> tuple[EnvLine, ...]:
    """Flatten every assignment line across every discovered file.

 Performed at import time so pytest's collection phase can present
 one test case per assignment with a stable, descriptive ID.
 """

    out: list[EnvLine] = []
    for path in _discover_env_example_files():
        out.extend(_parse_env_file(path))
    return tuple(out)


_ALL_ENV_LINES: tuple[EnvLine, ...] = _collect_all_env_lines()


def _line_id(line: EnvLine) -> str:
    """Produce a deterministic, humane pytest test ID for an assignment.

 Format: ``<rel/path>:<line>:<KEY>``. The path is workspace-relative
 so the failing test ID points directly at the offending file.
 """

    return f"{line.file_relpath}:{line.line_number}:{line.key}"


# ---------------------------------------------------------------------------
# Sanity checks on discovery (fail fast if the workspace shape changes)
# ---------------------------------------------------------------------------


def test_env_example_discovery_covers_root_and_components() -> None:
    """The discovery walks the workspace as expected.

 The project ships exactly nine ``.env.example`` files (one root +
 one per Component in the manifest). If a Component's example file
 goes missing this surfaces here as a count mismatch with a precise
 error message; invariant (path coverage) catches the same case
 from a different angle.
 """

    discovered = _discover_env_example_files()
    expected_count = 1 + len(COMPONENT_MANIFEST)
    assert len(discovered) == expected_count, (
        f"Expected {expected_count}.env.example files (root + "
        f"{len(COMPONENT_MANIFEST)} components); discovered "
        f"{len(discovered)}: "
        f"{[str(p.relative_to(WORKSPACE_ROOT)) for p in discovered]}"
    )


def test_every_env_example_has_at_least_one_assignment() -> None:
    """Every discovered ``.env.example`` parses to ≥1 ``KEY=VALUE`` line.

 Empty or comment-only example files would silently pass the
 per-line parametrise (zero cases generated), so we guard against
 that pathological shape here.
 """

    by_file: dict[str, int] = {}
    for line in _ALL_ENV_LINES:
        by_file[line.file_relpath] = by_file.get(line.file_relpath, 0) + 1

    discovered_relpaths = [
        str(p.relative_to(WORKSPACE_ROOT)).replace("\\", "/")
        for p in _discover_env_example_files()
    ]
    empty_files = [rel for rel in discovered_relpaths if by_file.get(rel, 0) == 0]
    assert not empty_files, (
        f".env.example files contain no parseable KEY=VALUE lines: "
        f"{empty_files}"
    )


# ---------------------------------------------------------------------------
# invariant - secret hygiene per assignment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    _ALL_ENV_LINES,
    ids=[_line_id(line) for line in _ALL_ENV_LINES],
)
def test_env_example_value_is_placeholder_not_secret(line: EnvLine) -> None:
    """invariant - every ``KEY=VALUE`` value is a placeholder, not a secret.



 Each assignment line must:

 1. Parse cleanly into ``KEY=VALUE`` form.
 2. Have a value that is empty, a known dev credential, or matches
 at least one allowlist regex.
 3. Not match any denylist regex (base64 blob, JWT, ``sk-``,
 ``glpat-`` or UUID).
 """

    # Catch any lines the parser flagged as malformed during collection.
    assert line.key != "<UNPARSEABLE>", (
        f"Malformed assignment line in {line.file_relpath}:{line.line_number}: "
        f"{line.value!r}. Expected ``KEY=VALUE`` form."
    )

    assert _matches_allowlist(line.value), (
        f"Value for {line.key} in {line.file_relpath}:{line.line_number} "
        f"does not match any allowlist pattern and is not a known "
        f"dev-only credential: {line.value!r}. "
        f"Per the operational rule, ``.env.example`` values must be "
        f"placeholders (e.g. ``change-me``, ``<set-by-vault>``, empty), "
        f"a structurally-bounded value (URL, integer, kebab identifier, "
        f"host:port), or one of the known dev credentials "
        f"({sorted(_KNOWN_DEV_CREDENTIALS)})."
    )

    violation = _denylist_violation(line.value)
    assert violation is None, (
        f"Value for {line.key} in {line.file_relpath}:{line.line_number} "
        f"matches denylist pattern '{violation}': {line.value!r}. "
        f"Per the operational rule, ``.env.example`` MUST NOT carry "
        f"real secrets - replace this value with a placeholder before "
        f"committing."
    )


# ---------------------------------------------------------------------------
# invariant -
# ---------------------------------------------------------------------------
#
#
# The parametrised file-level tests above pin the static contract:
# every shipped ``.env.example`` line is structurally a placeholder.
# invariant below complements that with a *generative* check on the
# allowlist / denylist regex pair itself: for an arbitrary
# ``KEY=VALUE`` line drawn from the credential-bearing patterns
# enumerated by (``api_token``, ``password``,
# ``secret``, plus the ``Authorization: Basic`` and ``Bearer...``
# header echoes), the value SHALL trip at least one denylist pattern
# *or* fail the allowlist outright. Symmetrically, for an arbitrary
# placeholder-shaped value the line SHALL pass.
#
# Why this matters: a regression in:data:`_ALLOWLIST_PATTERNS` /
#:data:`_DENYLIST_PATTERNS` (e.g. someone tightening the kebab
# identifier regex and accidentally accepting an OpenAI key prefix)
# would only surface here if a real secret happened to land in a
# committed ``.env.example``. Hypothesis fuzzes the regex pair
# directly so the regression fails the suite the moment the patterns
# drift, regardless of repo state.

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

import string  # noqa: E402 -- placed near use site for locality


# Strategies for generating values that must trip the denylist. Each
# strategy is bounded so Hypothesis explores the search space quickly
# and never produces arbitrarily long blobs.
_property9_base64_blob = st.text(
    alphabet=string.ascii_letters + string.digits + "+/",
    min_size=32,
    max_size=64,
).map(lambda s: s + "==")  # base64 padding makes the denylist match.

_property9_jwt = st.tuples(
    st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=8, max_size=24),
    st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=8, max_size=24),
).map(lambda parts: f"eyJ{parts[0]}.{parts[0]}.{parts[1]}")

_property9_openai_key = st.text(
    alphabet=string.ascii_letters + string.digits + "_-",
    min_size=16,
    max_size=48,
).map(lambda s: f"sk-{s}")

_property9_glpat = st.text(
    alphabet=string.ascii_letters + string.digits + "_-",
    min_size=16,
    max_size=48,
).map(lambda s: f"glpat-{s}")

_property9_uuid = st.tuples(
    st.text(alphabet="0123456789abcdef", min_size=8, max_size=8),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=4),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=4),
    st.text(alphabet="0123456789abcdef", min_size=4, max_size=4),
    st.text(alphabet="0123456789abcdef", min_size=12, max_size=12),
).map(lambda parts: "-".join(parts))


_property9_secret_value = st.one_of(
    _property9_base64_blob,
    _property9_jwt,
    _property9_openai_key,
    _property9_glpat,
    _property9_uuid,
)


# Strategy for benign placeholder shapes - every draw MUST pass the
# allowlist *and* miss the denylist.
_property9_placeholder_value = st.one_of(
    st.just(""),  # empty placeholder.
    st.sampled_from(sorted(_KNOWN_DEV_CREDENTIALS)),
    st.sampled_from(["change-me", "change-me-please", "<set-by-vault>"]),
    # ``vault:`` ref - bounded path of slash-separated identifiers.
    st.lists(
        st.text(
            alphabet=string.ascii_letters + string.digits + "_-",
            min_size=2,
            max_size=8,
        ),
        min_size=1,
        max_size=4,
    ).map(lambda parts: f"vault:{'/'.join(parts)}"),
    # Boolean.
    st.sampled_from(["true", "false", "TRUE", "False"]),
    # Integer (port / timeout / retries).
    st.integers(min_value=0, max_value=65535).map(str),
    # Kebab / snake identifier.
    st.text(
        alphabet=string.ascii_lowercase + "-_",
        min_size=3,
        max_size=20,
    ).filter(lambda s: bool(s) and s[0].isalpha()),
    # ``host:port`` pair.
    st.tuples(
        st.text(
            alphabet=string.ascii_lowercase + string.digits + "-.",
            min_size=2,
            max_size=12,
        ).filter(lambda s: bool(s) and s[0].isalpha()),
        st.integers(min_value=1, max_value=65535),
    ).map(lambda hp: f"{hp[0]}:{hp[1]}"),
)


@given(value=_property9_secret_value)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property9_denylist_rejects_known_secret_shapes(value: str) -> None:
    """invariant - every plausible secret shape is rejected.



 For an arbitrary value drawn from one of the five known
 secret-shape strategies (base64 blob, JWT, OpenAI key, GitLab
 PAT, UUID), the env-secret-hygiene gate SHALL reject the line:

 * either ``_matches_allowlist(value)`` returns ``False``;
 * or ``_denylist_violation(value)`` returns a non-``None`` label.

 The disjunction is what enforces the contract - a value can pass
 the allowlist (e.g. a UUID looks like a kebab identifier under a
 permissive regex) yet still fail because it hits the denylist.
 Either way, the line is flagged for the operator with a precise
 reason - failed test reports the file/line/key,
 here generalised to value/reason).
 """

    # The "secret shapes" cover the credential family from
    # plus high-entropy blob shapes that commonly
    # appear in production credentials.
    matches_allowlist = _matches_allowlist(value)
    violation = _denylist_violation(value)

    assert (not matches_allowlist) or (violation is not None), (
        f"invariant violated: secret-shaped value passed both gates. "
        f"value={value!r}, matches_allowlist={matches_allowlist}, "
        f"denylist_violation={violation!r}. Per the operational rule "
        f"a real-looking credential MUST be rejected by the env "
        f"secret-hygiene gate."
    )


@given(value=_property9_placeholder_value)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property9_allowlist_accepts_placeholder_shapes(value: str) -> None:
    """invariant - every placeholder shape passes the gate.



 The complement of the rejection invariant: an arbitrary
 placeholder-shaped value (empty, known dev credential,
 ``vault:`` ref, boolean, integer, kebab identifier, host:port)
 MUST pass the allowlist *and* miss the denylist. This guards
 against a future tightening of the regex pair from accidentally
 rejecting legitimate placeholder shapes shipped by the project.
 """

    matches_allowlist = _matches_allowlist(value)
    violation = _denylist_violation(value)

    assert matches_allowlist, (
        f"invariant violated: placeholder-shaped value rejected by "
        f"allowlist. value={value!r}. Per the operational rule the env "
        f"secret-hygiene gate MUST accept legitimate placeholders."
    )
    assert violation is None, (
        f"invariant violated: placeholder-shaped value tripped "
        f"denylist {violation!r}. value={value!r}. Per the operational rule "
        f"6.2 the env secret-hygiene gate MUST NOT mistake a "
        f"placeholder for a real secret."
    )
