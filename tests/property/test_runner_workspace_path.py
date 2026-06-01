"""Property test 12 — Workspace Path Build Determinism + Path-Traversal Safety (Q13).

**Validates: Requirements 11.3, 11.4, 11.6**

Spec: ``platform-mimari-uyumluluk`` task 13.5.

Scope
-----

This test pins three invariants over
:func:`runners.workspace_path.build_workspace_path` (the helper added by
task 13.1) and the ``RUNNER_BASE_PATH > SSH_BASE_PATH > default`` alias
chain on :class:`src.config.Settings.runner_base_path` (task 13.2):

1. **Determinism (R11.3, R11.6).** For every valid input
   ``(base, issue_key, iter_n)`` the helper returns
   ``f"{base.rstrip('/')}/{issue_key}/iter-{iter_n}"`` byte-for-byte —
   independent of how many trailing slashes the caller passes on
   ``base``, and idempotent across repeated calls.

2. **Path-traversal safety (R11.3, R11.6).** Any ``issue_key`` that
   does not match ``^[A-Z][A-Z0-9_]*-\\d+$`` — including the canonical
   path-traversal vectors (``..``, ``../etc``, absolute paths,
   embedded slashes) and shell-metachar vectors (``;``, ``&``, ``|``,
   backticks, ``$``, newline, null-byte) — is rejected up-front with
   :class:`InvalidIssueKeyError`. The helper never touches its
   formatter when validation fails, so no metachar can ever appear in
   the rendered output. The same guard rejects out-of-range
   ``iter_n`` (``< 0``, ``> 999``, booleans, non-int) with
   :class:`InvalidIterError`.

3. **Settings alias priority (R11.4).** ``runner_base_path`` is read
   in the order ``RUNNER_BASE_PATH > SSH_BASE_PATH > default
   ("/var/ai-runner")``. Both Hypothesis and three example-level
   guards exercise the chain so a regression that swaps the alias
   order or drops the legacy fallback is caught immediately.

Design notes
------------

The execution-runner ships its code under the ``src.`` package name,
which collides with the ``src.`` namespace already pinned by the
``agent-runner-worker`` integration suite (see
``tests/integration/_worker_path.py``). To stay hermetic — and to
avoid forcing the rest of the property suite into ``isolate_worker``
gymnastics — we load ``workspace_path.py`` and ``config.py`` as
standalone modules under synthetic top-level names via
``importlib.util.spec_from_file_location``. This mirrors the pattern
used by ``test_burst_debounce.py`` and ``test_replay_dedup.py``
elsewhere in this directory.

The helper has no side effects, so each Hypothesis example is a pure
function call; ``deadline=None`` is set only because Hypothesis's
default 200 ms ceiling is too tight for the ``Settings`` reload path.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Module loading — the execution-runner ships under ``src.*`` which the
# integration suite already pins; load standalone copies under unique
# synthetic names so this property test stays hermetic.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "execution-runner-worker" / "src"
)


def _load_module(name: str, file_path: Path) -> Any:
    """Load *file_path* as a top-level module under the synthetic name *name*.

    Registering the module under a unique name keeps the system import
    cache from pulling in (and clobbering) ``src.runners.workspace_path``
    — which the agent-runner integration tests reserve for the *other*
    worker tree.
    """

    spec = _importlib_util.spec_from_file_location(name, file_path)
    assert spec is not None and spec.loader is not None, (
        f"Failed to build import spec for {file_path!s}"
    )
    module = _importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_workspace_path_mod = _load_module(
    "_runner_workspace_path_sut",
    _RUNNER_SRC / "runners" / "workspace_path.py",
)
_config_mod = _load_module(
    "_runner_config_sut",
    _RUNNER_SRC / "config.py",
)

build_workspace_path = _workspace_path_mod.build_workspace_path
InvalidIssueKeyError = _workspace_path_mod.InvalidIssueKeyError
InvalidIterError = _workspace_path_mod.InvalidIterError
ISSUE_KEY_PATTERN: re.Pattern[str] = _workspace_path_mod.ISSUE_KEY_PATTERN
MIN_ITER: int = _workspace_path_mod.MIN_ITER
MAX_ITER: int = _workspace_path_mod.MAX_ITER
Settings = _config_mod.Settings


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Project-key prefix: ``[A-Z][A-Z0-9_]*`` — one upper-case letter then
# any mix of upper-case letters, digits, or underscores. Length capped
# at 8 for speed; the regex pattern is what matters, not the size.
_VALID_PROJECT_PREFIX = st.builds(
    lambda head, tail: head + tail,
    st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
        min_size=0,
        max_size=8,
    ),
)

# Issue-id suffix: ``\d+`` — at least one digit; ``\d`` matches more
# than just ``[0-9]`` in Unicode, but the helper's regex uses ``re.ASCII``
# implicitly via the source pattern, so we restrict to ASCII digits.
_VALID_ISSUE_SUFFIX = st.text(
    alphabet="0123456789", min_size=1, max_size=6
)

valid_issue_keys = st.builds(
    lambda prefix, suffix: f"{prefix}-{suffix}",
    _VALID_PROJECT_PREFIX,
    _VALID_ISSUE_SUFFIX,
)

# Bases: a deterministic POSIX-style path with optional trailing slashes.
# The helper strips trailing ``/`` so a base with 0, 1, or N trailing
# slashes must produce identical output.
_PATH_SEGMENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=8,
)
valid_bases = st.builds(
    lambda segments, trailing: "/" + "/".join(segments) + ("/" * trailing),
    st.lists(_PATH_SEGMENT, min_size=1, max_size=4),
    st.integers(min_value=0, max_value=3),
)

valid_iter_n = st.integers(min_value=MIN_ITER, max_value=MAX_ITER)

# Shell-metachar / path-traversal vectors. Property 12 explicitly calls
# out ``..``, ``;``, ``&``, ``|``, newline, null-byte (R11.6); the
# helper rejects anything that does not match the canonical pattern, so
# we strategy-mix curated vectors with arbitrary text containing those
# bytes.
_DANGEROUS_CHARS = "..;&|`$\n\r\x00\\\t \"'<>(){}[]*?#~"
_INVALID_KEY_VECTORS = st.one_of(
    st.sampled_from(
        [
            "..",
            "../etc",
            "../../etc/passwd",
            "/etc/passwd",
            "PAY-",
            "-123",
            "pay-123",  # lowercase
            "PAY_123",  # missing dash before digits
            "PAY-123 ",  # trailing space
            " PAY-123",  # leading space
            "PAY-123\n",  # newline
            "PAY-123;rm -rf /",  # shell metachar
            "PAY-123|cat",
            "PAY-123&ls",
            "PAY-123$IFS",
            "PAY-123`id`",
            "PAY-123\x00",  # null-byte
            "PAY/123",  # embedded slash
            "PAY-12.3",
            "0PAY-1",  # leading digit in prefix
            "_PAY-1",  # leading underscore
            "",  # empty
        ]
    ),
    st.text(min_size=0, max_size=20).filter(
        lambda s: ISSUE_KEY_PATTERN.fullmatch(s) is None
    ),
    # Always-traversal: any string containing one of the metachars.
    st.builds(
        lambda head, mid, tail: head + mid + tail,
        st.text(alphabet=_DANGEROUS_CHARS, min_size=1, max_size=4),
        st.text(min_size=0, max_size=4),
        st.text(alphabet=_DANGEROUS_CHARS, min_size=0, max_size=4),
    ),
)

_INVALID_ITER_N = st.one_of(
    st.integers(max_value=MIN_ITER - 1),
    st.integers(min_value=MAX_ITER + 1),
    st.booleans(),  # bool is rejected even though ``isinstance(True, int)``
    st.text(min_size=1, max_size=4),
    st.floats(allow_nan=False, allow_infinity=False),
    st.none(),
)


# ---------------------------------------------------------------------------
# Property 1 — Determinism for valid inputs
# ---------------------------------------------------------------------------


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    base=valid_bases,
    issue_key=valid_issue_keys,
    iter_n=valid_iter_n,
)
def test_valid_inputs_produce_canonical_path(
    base: str, issue_key: str, iter_n: int
) -> None:
    """**Validates: Requirement 11.3, 11.6.**

    For every valid triple, output equals the canonical formula —
    ``{base.rstrip('/')}/{issue_key}/iter-{iter_n}`` — and the helper
    is idempotent (repeated calls return byte-for-byte equal strings).
    """

    expected = f"{base.rstrip('/')}/{issue_key}/iter-{iter_n}"

    first = build_workspace_path(base, issue_key, iter_n)
    second = build_workspace_path(base, issue_key, iter_n)

    assert first == expected
    # Idempotency / determinism: repeated calls match.
    assert first == second


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    base=valid_bases,
    issue_key=valid_issue_keys,
    iter_n=valid_iter_n,
)
def test_output_is_safe_for_shell_consumers(
    base: str, issue_key: str, iter_n: int
) -> None:
    """**Validates: Requirement 11.6.**

    The execution-runner builds SSH commands by interpolating this
    string. The helper's contract — and what Property 12 enforces — is
    that the *output* is always free of the shell metachars and
    path-traversal bytes the validator rejects on the input side. We
    re-check the output here as a defence-in-depth assertion: even if
    a future maintainer relaxes the input regex, this test fails the
    moment a single dangerous byte slips through to the formatted
    string.

    The valid ``base`` strategy itself only emits ``/`` and the
    POSIX-safe alphabet, and ``issue_key`` is regex-validated, so the
    output *is* guaranteed metachar-free; we assert that here.
    """

    output = build_workspace_path(base, issue_key, iter_n)

    forbidden_substrings = ("..", ";", "&", "|", "\n", "\r", "\x00", "`")
    for token in forbidden_substrings:
        assert token not in output, (
            f"output={output!r} contains forbidden token {token!r} "
            f"(base={base!r}, issue_key={issue_key!r}, iter_n={iter_n!r})"
        )

    # The output MUST always end with the rendered iter segment.
    assert output.endswith(f"/{issue_key}/iter-{iter_n}")


# ---------------------------------------------------------------------------
# Property 2 — Path-traversal safety on issue_key
# ---------------------------------------------------------------------------


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    base=valid_bases,
    invalid_key=_INVALID_KEY_VECTORS,
    iter_n=valid_iter_n,
)
def test_invalid_issue_keys_are_rejected(
    base: str, invalid_key: Any, iter_n: int
) -> None:
    """**Validates: Requirement 11.3, 11.6.**

    Any ``issue_key`` outside the canonical pattern raises
    :class:`InvalidIssueKeyError`. The exception preserves the
    offending value for audit. No ``InvalidIterError`` is raised
    because ``iter_n`` is valid — order of validation must surface the
    issue-key violation when both could otherwise apply.
    """

    with pytest.raises(InvalidIssueKeyError) as exc_info:
        build_workspace_path(base, invalid_key, iter_n)

    # The exception carries the offending value verbatim so callers
    # can emit an audit payload without re-parsing the message.
    assert exc_info.value.issue_key == invalid_key


# ---------------------------------------------------------------------------
# Property 3 — iter_n range guard
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    base=valid_bases,
    issue_key=valid_issue_keys,
    invalid_iter=_INVALID_ITER_N,
)
def test_invalid_iter_n_is_rejected(
    base: str, issue_key: str, invalid_iter: Any
) -> None:
    """**Validates: Requirement 11.3, 11.6.**

    ``iter_n`` MUST be a non-bool ``int`` in ``[MIN_ITER, MAX_ITER]``.
    Booleans, floats, ``None``, strings, and out-of-range integers are
    all rejected with :class:`InvalidIterError`. The exception
    preserves the offending value for audit.
    """

    with pytest.raises(InvalidIterError) as exc_info:
        build_workspace_path(base, issue_key, invalid_iter)

    assert exc_info.value.iter_n == invalid_iter


# ---------------------------------------------------------------------------
# Property 4 — Trailing-slash idempotency
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    base=valid_bases,
    issue_key=valid_issue_keys,
    iter_n=valid_iter_n,
    extra_slashes=st.integers(min_value=0, max_value=5),
)
def test_trailing_slash_is_normalised(
    base: str, issue_key: str, iter_n: int, extra_slashes: int
) -> None:
    """**Validates: Requirement 11.3, 11.6.**

    ``base`` may carry any number of trailing forward slashes; the
    output is invariant under that suffix. This is the determinism
    contract that lets ``RUNNER_BASE_PATH=/var/ai-runner`` and
    ``RUNNER_BASE_PATH=/var/ai-runner/`` produce identical workspaces.
    """

    base_no_slash = base.rstrip("/")
    base_with_extras = base_no_slash + ("/" * extra_slashes)

    no_slash_output = build_workspace_path(base_no_slash, issue_key, iter_n)
    with_extras_output = build_workspace_path(
        base_with_extras, issue_key, iter_n
    )

    assert no_slash_output == with_extras_output


# ---------------------------------------------------------------------------
# Property 5 — Settings alias priority
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_runner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop both env vars the alias chain reads so each example is hermetic."""

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)


def _build_settings() -> Any:
    """Construct ``Settings`` with ``.env`` loading disabled.

    Process env vars (set by the calling test via monkeypatch) are
    still honoured; only the per-worker ``.env`` file is bypassed so
    the test is hermetic regardless of the developer's local working
    tree. Mirrors the helper in
    ``workers/execution-runner-worker/tests/unit/test_settings_runner_base_path.py``.
    """

    return Settings(_env_file=None)


# Generator for env values: any non-empty string without ``=`` and
# without NUL — both would cause ``os.environ`` itself to refuse the
# write. We only care that whatever the env carries round-trips into
# ``settings.runner_base_path``.
_ENV_VALUE = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters="\x00=",
    ),
    min_size=1,
    max_size=32,
)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture, HealthCheck.too_slow),
)
@given(
    canonical=_ENV_VALUE,
    legacy=_ENV_VALUE,
)
def test_canonical_env_wins_over_legacy_alias(
    canonical: str,
    legacy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Validates: Requirement 11.4.**

    When ``RUNNER_BASE_PATH`` is set, it MUST win regardless of
    whether ``SSH_BASE_PATH`` is also set. ``AliasChoices`` declares
    ``RUNNER_BASE_PATH`` first, so the canonical name takes precedence.
    """

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)
    monkeypatch.setenv("SSH_BASE_PATH", legacy)
    monkeypatch.setenv("RUNNER_BASE_PATH", canonical)

    settings_obj = _build_settings()

    assert settings_obj.runner_base_path == canonical


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture, HealthCheck.too_slow),
)
@given(legacy=_ENV_VALUE)
def test_legacy_alias_used_when_canonical_absent(
    legacy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Validates: Requirement 11.4.**

    When ``RUNNER_BASE_PATH`` is unset, the deprecated ``SSH_BASE_PATH``
    alias is honoured. This is the backwards-compatibility contract
    that protects existing deployments from a silent fall-back to the
    ``/var/ai-runner`` default.
    """

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)
    monkeypatch.setenv("SSH_BASE_PATH", legacy)

    settings_obj = _build_settings()

    assert settings_obj.runner_base_path == legacy


def test_default_when_neither_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Validates: Requirement 11.4.**

    With neither env var set the documented default
    (``/var/ai-runner``, referenced by the ``task-creation-assistant``
    prompt template) wins. Example-level guard rather than Hypothesis
    because the property is a single point.
    """

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)

    settings_obj = _build_settings()

    assert settings_obj.runner_base_path == "/var/ai-runner"


# ---------------------------------------------------------------------------
# Property 6 — End-to-end Settings → build_workspace_path round-trip
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture, HealthCheck.too_slow),
)
@given(
    canonical=_ENV_VALUE,
    issue_key=valid_issue_keys,
    iter_n=valid_iter_n,
)
def test_settings_to_build_workspace_path_round_trip(
    canonical: str,
    issue_key: str,
    iter_n: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Validates: Requirements 11.3, 11.4, 11.6.**

    The two public surfaces — :class:`Settings.runner_base_path` and
    :func:`build_workspace_path` — compose deterministically. Setting
    ``RUNNER_BASE_PATH=<X>`` followed by
    ``build_workspace_path(settings.runner_base_path, issue_key, iter_n)``
    yields ``f"{X.rstrip('/')}/{issue_key}/iter-{iter_n}"``. This is
    the exact wiring used by ``runners/remote_ssh.py`` and
    ``runners/remote_ssh_docker.py`` (task 13.3); breaking it desyncs
    the prompt-template path layout from the runtime layout.
    """

    monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
    monkeypatch.delenv("SSH_BASE_PATH", raising=False)
    monkeypatch.setenv("RUNNER_BASE_PATH", canonical)

    settings_obj = _build_settings()
    output = build_workspace_path(
        settings_obj.runner_base_path, issue_key, iter_n
    )

    assert output == f"{canonical.rstrip('/')}/{issue_key}/iter-{iter_n}"
