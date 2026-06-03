"""invariant C5 — Log redaction Sensitive_Env_Key değerlerini sızdırmaz.



invariant
--------
For any (a) set of ``Sensitive_Env_Key → value`` pairs, (b) set of
non-sensitive ``KEY → value`` pairs, and (c) freeform plain-text log
noise, the lines returned by:meth:`LifecycleService.logs` SHALL
satisfy three invariants simultaneously:

1. **No leak.** No sensitive value string appears anywhere in the
 redacted output (substring check across the joined output).
2. **Key visibility.** Each sensitive ``KEY=value`` token in the input
 becomes ``KEY=<redacted>`` in the output, so operators can see
 *which* variable was masked even though its value is gone.
3. **Pass-through.** Each non-sensitive ``KEY=value`` token survives
 the redactor unchanged.

These three invariants together encode the lifecycle log endpoint
contract: every Sensitive_Env_Key value must be ``<redacted>``-ified
before responding.

Strategy
--------
* ``sensitive_pairs`` and ``non_sensitive_pairs`` are
 ``st.dictionaries`` over disjoint key pools and a constrained
 ``st.text`` value alphabet (``[a-z0-9-]``, length 4..32). The pools
 are validated against:func:`src.lifecycle.sensitive.is_sensitive_env_key` at import time
 so a future change to the matcher fails the test fast rather than
 producing a confusing counterexample.
* ``extra_log_lines`` is ``st.text`` over ``[a-z ]`` to inject
 freeform plain-text log noise without ``KEY=`` tokens.
* ``hypothesis.assume(...)`` filters the small number of pathological
 cases where (i) a sensitive value coincidentally appears as a
 substring of a non-sensitive value, (ii) a sensitive value is a
 substring of the literal sentinel ``<redacted>``, or (iii) a
 sensitive value is a substring of an injected noise line. Those
 cases are not bugs in the redactor — they would fail the assertion
 for trivial reasons unrelated to the property under test.

Stub fakes
----------
``_FakeAuditWriter``, ``_FakeVaultClient``, ``_FakeComposeRunner``,
and ``_FakeHealthProbe`` mirror the patterns established by
``services/admin-dashboard-api/tests/unit/test_lifecycle_service.py``
and ``tests/property/test_stop_idempotent.py`` (invariant). The
Compose runner stub returns a programmable ``logs_stdout`` payload
from ``logs(...)`` so the property exercises the orchestrator's
redaction surface against arbitrary log content.
"""

from __future__ import annotations

import asyncio
import string
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest auto-loads it but we
# add ``tests/`` to ``sys.path`` defensively so this module also imports
# cleanly under a direct ``python -m pytest tests/property`` invocation
# (mirrors the pattern used by the other invariant in this folder).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way the per-service unit tests do. This lets us
# ``import src.lifecycle.service`` directly (mirrors ``test_stop_idempotent.py``).
_SERVICE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "admin-dashboard-api"
)
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditWriteOutcome,
)
from src.lifecycle.compose_runner import ComposeResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.sensitive import is_sensitive_env_key  # noqa: E402
from src.lifecycle.service import LifecycleService  # noqa: E402
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (deliberately green — this property only exercises ``logs(...)``)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """No-op audit writer; ``logs(...)`` does not touch the audit path."""

    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)

    async def precheck(self) -> None:  # pragma: no cover - unused by ``logs``
        return None

    async def write(self, entry: AuditEntry) -> None:  # pragma: no cover - unused
        return None

    async def write_with_retry(  # pragma: no cover - unused
        self, entry: AuditEntry
    ) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """No-op Vault client; ``logs(...)`` does not read or write secrets."""

    async def write_env_override(  # pragma: no cover - unused
        self, *, service_name: str, key: str, value: str
    ) -> None:
        return None

    async def read_env_overrides(  # pragma: no cover - unused
        self, *, service_name: str
    ) -> dict[str, str]:
        return {}

    async def delete_env_override(  # pragma: no cover - unused
        self, *, service_name: str, key: str
    ) -> None:
        return None


@dataclass
class _FakeComposeRunner:
    """Returns a programmable ``logs_stdout`` from ``logs(follow=False)``.

 The lifecycle service splits this stdout into lines and runs each
 line through its sensitive-key redactor — exactly the surface the
 property under test wants to exercise.
 """

    logs_stdout: str = ""
    logs_calls: list[dict[str, Any]] = field(default_factory=list)

    async def up(  # pragma: no cover - unused by invariant
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(  # pragma: no cover - unused by invariant
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        self.logs_calls.append(
            {"service_name": service_name, "tail": tail, "follow": follow}
        )
        return ComposeResult(
            exit_code=0,
            stdout=self.logs_stdout,
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(  # pragma: no cover - unused by invariant
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> Any:
        raise NotImplementedError


@dataclass
class _FakeHealthProbe:
    """Stable ``healthy`` snapshot; ``logs(...)`` never invokes it."""

    async def probe(  # pragma: no cover - unused by invariant
        self, entry: ManagedServiceEntry
    ) -> HealthSnapshot:
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# Sensitive / non-sensitive key pools
# ---------------------------------------------------------------------------


# Each member of this pool is *known* to match
#:func:`is_sensitive_env_key` — covers every documented suffix
# (``_TOKEN``/``_KEY``/``_SECRET``/``_PASSWORD``/``_DSN``/``_CREDENTIAL``)
# plus the ``_PRIVATE_`` infix. The static ``assert`` block below
# pins the pool to the matcher so a future change to either side
# is noticed immediately.
_SENSITIVE_KEYS: tuple[str, ...] = (
    "API_TOKEN",
    "VAULT_TOKEN",
    "DB_PASSWORD",
    "JWT_SECRET",
    "STRIPE_API_KEY",
    "POSTGRES_DSN",
    "OAUTH_CREDENTIAL",
    "DB_PRIVATE_HOST",
)


# Non-sensitive counterparts: structurally similar (uppercase env-var
# style) but their names match none of the Sensitive_Env_Key patterns.
_NON_SENSITIVE_KEYS: tuple[str, ...] = (
    "PORT",
    "LOG_LEVEL",
    "WORKER_NAME",
    "TIMEOUT_SECONDS",
    "REGION",
    "FEATURE_FLAG",
)


# Static guard — fail at import time rather than producing a confusing
# Hypothesis counterexample if someone rewrites the matcher without
# refreshing the pools.
for _k in _SENSITIVE_KEYS:
    assert is_sensitive_env_key(_k), (
        f"_SENSITIVE_KEYS member {_k!r} is not flagged sensitive by "
        f"is_sensitive_env_key — refresh the pool to match the matcher."
    )
for _k in _NON_SENSITIVE_KEYS:
    assert not is_sensitive_env_key(_k), (
        f"_NON_SENSITIVE_KEYS member {_k!r} is flagged sensitive by "
        f"is_sensitive_env_key — pick a structurally different name."
    )


# Value alphabet: lowercase ASCII letters + digits + dash. Three
# motivations:
#
# * No whitespace → each value is a single ``\S+`` token, which is
# exactly what the redaction regex matches against.
# * No uppercase → values cannot accidentally collide with KEY
# identifiers (the redaction pattern is anchored on uppercase
# word boundaries, so a lowercase value is structurally distinct).
# * ``min_size=4`` → keeps generated values long enough to be
# meaningful substrings; ``max_size=32`` keeps Hypothesis examples
# short enough that the property runs in well under the
# ``deadline=None`` budget.
_VALUE_ALPHABET: str = string.ascii_lowercase + string.digits + "-"

_value_strategy: st.SearchStrategy[str] = st.text(
    alphabet=_VALUE_ALPHABET,
    min_size=4,
    max_size=32,
)


# ---------------------------------------------------------------------------
# Synthetic workspace builder
# ---------------------------------------------------------------------------


_MANIFEST_NAME = "test-service"
_COMPOSE_SERVICE_NAME = "test-service"
_ENV_EXAMPLE_RELPATH = f"services/{_MANIFEST_NAME}/.env.example"


def _build_workspace(
    tmp_path: Path,
    sensitive_pairs: dict[str, str],
    non_sensitive_pairs: dict[str, str],
) -> Path:
    """Materialise a synthetic workspace whose ``.env.example`` lists every key.

 The ``LifecycleService`` builds its redaction pattern from the
 ``Sensitive_Env_Key`` subset of the LHS keys parsed out of this
 file (see:meth:`LifecycleService.build_log_redaction_pattern`).
 Non-sensitive keys are written with their generated default
 values; sensitive keys carry an empty default because the
 operator is expected to supply them at form time
 — the actual default value does not affect
 redaction since the redactor only looks at LHS key names.
 """

    svc_dir = tmp_path / "services" / _MANIFEST_NAME
    svc_dir.mkdir(parents=True)

    lines: list[str] = ["# Synthetic env example for invariant (log redaction)"]
    for key in sensitive_pairs:
        lines.append(f"# {key} (sensitive — masked in logs)")
        lines.append(f'{key}=""')
    for key, value in non_sensitive_pairs.items():
        lines.append(f"# {key}")
        lines.append(f"{key}={value}")
    text = "\n".join(lines) + "\n"

    (svc_dir / ".env.example").write_text(text, encoding="utf-8")
    return tmp_path


def _entry() -> ManagedServiceEntry:
    """Single-service manifest entry pointing at the synthetic env file."""

    return ManagedServiceEntry(
        name=_MANIFEST_NAME,
        kind="http_service",
        compose_service_name=_COMPOSE_SERVICE_NAME,
        compose_profile=_MANIFEST_NAME,
        env_example_path=_ENV_EXAMPLE_RELPATH,
        health_endpoint="/healthz",
        test_command=None,
    )


def _make_service(
    workspace_root: Path, compose: _FakeComposeRunner
) -> LifecycleService:
    """Wire a ``LifecycleService`` against the no-op fakes + the given Compose stub.

 ``sleep`` is replaced with an immediate-return coroutine so the
 health-poll loop in ``start`` (unused here) never burns wall-clock
 time inside Hypothesis examples.
 """

    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    health = _FakeHealthProbe()

    async def _no_sleep(_seconds: float) -> None:
        return None

    return LifecycleService(
        manifest=(_entry(),),
        state=None,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )


# ---------------------------------------------------------------------------
# invariant
# ---------------------------------------------------------------------------


@given(
    sensitive_pairs=st.dictionaries(
        keys=st.sampled_from(_SENSITIVE_KEYS),
        values=_value_strategy,
        min_size=1,
        max_size=4,
    ),
    non_sensitive_pairs=st.dictionaries(
        keys=st.sampled_from(_NON_SENSITIVE_KEYS),
        values=_value_strategy,
        min_size=1,
        max_size=4,
    ),
    extra_log_lines=st.lists(
        st.text(
            alphabet=string.ascii_lowercase + " ",
            min_size=4,
            max_size=40,
        ),
        min_size=0,
        max_size=4,
    ),
)
@settings(
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_logs_redact_only_sensitive_values(
    sensitive_pairs: dict[str, str],
    non_sensitive_pairs: dict[str, str],
    extra_log_lines: list[str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """invariant — ``LifecycleService.logs`` masks every Sensitive_Env_Key value.



 Three simultaneous invariants over the redacted output:

 1. No sensitive value substring survives anywhere.
 2. Each sensitive ``KEY=value`` token becomes ``KEY=<redacted>``.
 3. Each non-sensitive ``KEY=value`` token passes through unchanged.
 """

    sensitive_values = set(sensitive_pairs.values())
    non_sensitive_values = set(non_sensitive_pairs.values())

    # ``assume`` filters: discard pathological cases where a
    # sensitive value happens to appear *outside* a redacted token,
    # which would fail invariant 1 for a reason unrelated to the
    # redactor's correctness.
    #
    # 1) A sensitive value must not be a substring of any
    # non-sensitive value (the non-sensitive line is intentionally
    # NOT redacted, so it would carry the sensitive bytes through).
    for sv in sensitive_values:
        for nsv in non_sensitive_values:
            assume(sv not in nsv)
    # 2) A sensitive value must not be a substring of the literal
    # sentinel ``<redacted>`` (else the post-redaction line itself
    # would trivially contain the value bytes).
    for sv in sensitive_values:
        assume(sv not in "<redacted>")
    # 3) A sensitive value must not be a substring of any
    # plain-text noise line — those lines never had a ``KEY=``
    # prefix, so the redactor (correctly) leaves them alone.
    for sv in sensitive_values:
        for noise_line in extra_log_lines:
            assume(sv not in noise_line)

    # Materialise the synthetic workspace + a Compose stub whose
    # ``logs(...)`` returns one line per generated KEY=value pair plus
    # the noise lines. Iteration order interleaves sensitive and
    # non-sensitive tokens so the redactor cannot rely on positional
    # heuristics.
    workspace = _build_workspace(
        tmp_path_factory.mktemp("ws-c5"),
        sensitive_pairs,
        non_sensitive_pairs,
    )

    log_lines: list[str] = []
    for key, value in sensitive_pairs.items():
        log_lines.append(f"booting service with {key}={value}")
    for key, value in non_sensitive_pairs.items():
        log_lines.append(f"config: {key}={value}")
    log_lines.extend(extra_log_lines)
    compose = _FakeComposeRunner(logs_stdout="\n".join(log_lines))

    svc = _make_service(workspace, compose)

    async def run() -> list[str]:
        return await svc.logs(name=_MANIFEST_NAME, tail=200, follow=False)

    redacted_lines = asyncio.run(run())
    joined = "\n".join(redacted_lines)

    # Invariant 1 — no sensitive value survives anywhere.
    for sv in sensitive_values:
        assert sv not in joined, (
            f"sensitive value {sv!r} leaked into redacted log output. "
            f"Pairs: sensitive={sensitive_pairs!r}, "
            f"non_sensitive={non_sensitive_pairs!r}; "
            f"redacted output: {joined!r}"
        )

    # Invariant 2 — each sensitive KEY appears as ``KEY=<redacted>`` so
    # operators can see *which* variable was masked.
    for key in sensitive_pairs:
        assert f"{key}=<redacted>" in joined, (
            f"expected ``{key}=<redacted>`` in redacted output but it is "
            f"missing. Sensitive pairs: {sensitive_pairs!r}; redacted "
            f"output: {joined!r}"
        )

    # Invariant 3 — non-sensitive ``KEY=value`` tokens are pass-through.
    for key, value in non_sensitive_pairs.items():
        assert f"{key}={value}" in joined, (
            f"non-sensitive token ``{key}={value}`` was modified or lost "
            f"by the redactor. Non-sensitive pairs: "
            f"{non_sensitive_pairs!r}; redacted output: {joined!r}"
        )

    # Sanity: the redactor must not drop or merge lines (one input
    # line → one output line).
    assert len(redacted_lines) == len(log_lines), (
        f"redaction changed line count: input={len(log_lines)}, "
        f"output={len(redacted_lines)}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors (named cases that pin the property surface)
# ---------------------------------------------------------------------------


def test_logs_redact_concrete_mixed_pair_set(tmp_path: Path) -> None:
    """Concrete anchor: hand-picked sensitive + non-sensitive pair set.

 Pins the *KEY=<redacted>* shape and the pass-through behaviour for
 a fixed input so a regression in the redaction regex (e.g. losing
 the key name from the replacement, or mangling non-sensitive
 values) fails this test deterministically — independent of the
 Hypothesis search order.
 """

    sensitive_pairs = {
        "API_TOKEN": "super-secret-abc",
        "DB_PASSWORD": "p4ssw0rd-xyz",
        "DB_PRIVATE_HOST": "10-0-0-7-internal",
    }
    non_sensitive_pairs = {
        "PORT": "8080",
        "LOG_LEVEL": "info",
    }

    workspace = _build_workspace(tmp_path, sensitive_pairs, non_sensitive_pairs)

    log_lines = [
        f"booting service with API_TOKEN={sensitive_pairs['API_TOKEN']} "
        f"PORT={non_sensitive_pairs['PORT']}",
        f"connecting with DB_PASSWORD={sensitive_pairs['DB_PASSWORD']}",
        f"resolved DB_PRIVATE_HOST={sensitive_pairs['DB_PRIVATE_HOST']}",
        f"config: LOG_LEVEL={non_sensitive_pairs['LOG_LEVEL']}",
        "ready to accept connections",
    ]
    compose = _FakeComposeRunner(logs_stdout="\n".join(log_lines))
    svc = _make_service(workspace, compose)

    async def run() -> list[str]:
        return await svc.logs(name=_MANIFEST_NAME, tail=200, follow=False)

    redacted = asyncio.run(run())
    joined = "\n".join(redacted)

    # Sensitive values gone; key names preserved.
    for key, value in sensitive_pairs.items():
        assert value not in joined
        assert f"{key}=<redacted>" in joined

    # Non-sensitive tokens untouched.
    for key, value in non_sensitive_pairs.items():
        assert f"{key}={value}" in joined

    # Plain prose untouched.
    assert "ready to accept connections" in joined


# ---------------------------------------------------------------------------
# invariant —
# ---------------------------------------------------------------------------
#
#
# This property complements the invariant above (which exercises the
# dashboard lifecycle redactor over its ``Sensitive_Env_Key`` set) by covering the *platform-wide*
#:class:`http_shared.redaction.RedactionFilter` against the five
# fixed patterns enumerated in:
#
# 1. ``Authorization: Basic <base64-blob>``
# 2. ``Bearer <token>``
# 3. ``api_token=<value>``
# 4. ``password=<value>``
# 5. ``secret=<value>``
#
# For an arbitrary log line built from any mixture of these
# credential-bearing tokens plus arbitrary surrounding noise, the
# redacted output MUST satisfy:
#
# * **No-leak:** every randomly-generated secret value is absent
# from the redacted text.
# * **Operator-visible key:** for the ``KEY=value`` family
# (``api_token=``, ``password=``, ``secret=``), the key name
# survives so operators can still grep for which credential was
# masked redaction shape).
# * **Bearer / Basic mask:** the ``Authorization: Basic …`` and
# ``Bearer …`` runs collapse to the ``***REDACTED***`` sentinel.
# * **Idempotency:** running the redactor a second time produces
# the same output (the sentinel is opaque to every pattern).
#
# Strategy notes
# --------------
#
# Each generated example is a list of "tokens" — either a structured
# secret (one of the five families) or a plain noise word. The
# tokens are joined with spaces to form a single log line, then the
# redacted output is verified.
#
# To stay independent of the redactor's regex set we re-derive
# detector regexes from the same five-pattern alphabet, but the
# ``assume`` filters guarantee that randomly-generated noise words
# never accidentally form one of the five secret-bearing shapes
# (e.g. a noise word that happens to match ``api_token=...``). This
# keeps the no-leak invariant strict without producing pathological
# false-positives.

import logging  # noqa: E402 -- placed near use site for locality
import re  # noqa: E402 -- placed near use site for locality

from http_shared.redaction import (  # noqa: E402 -- module-level imports OK
    REDACTION_PLACEHOLDER,
    RedactionFilter as _PlatformRedactionFilter,
    redact_text as _platform_redact_text,
)


# Alphabet for randomly-generated secret values. Bounded to characters
# that cannot collide with whitespace or the ``KEY=`` value-stop set
# (``\s``, ``&``, ``,``, ``;`` per ``redaction.py``'s ``_kv_pattern``)
# so the entire generated value lands inside a single redactor match.
_PROPERTY9_VALUE_ALPHABET: str = (
    string.ascii_letters + string.digits + "+/=._-"
)

# Plain-noise alphabet — strictly lowercase letters + digits. No ``=``
# (avoids accidentally forming a ``KEY=value`` pair), no ``:`` (avoids
# accidentally forming a header echo), no whitespace (each noise word
# is a single token).
_PROPERTY9_NOISE_ALPHABET: str = string.ascii_lowercase + string.digits


# Detector regexes (independent of the redactor) — used to check that
# no instance of any of the five credential families survives in the
# redacted output. The patterns are deliberately a touch more
# permissive than the redactor's ``REDACTION_PATTERNS`` so a regression
# in the redactor (e.g. tightening the value run from ``\S+`` to
# ``\w+``) cannot also regress the detector and silently pass.
#
# The value alphabets explicitly EXCLUDE ``*`` so a successful
# redaction (``KEY=***REDACTED***`` or the bare ``***REDACTED***``
# sentinel) is *not* flagged as a surviving secret. This matches the
# invariant in:data:`REDACTION_PLACEHOLDER` — the sentinel is
# deliberately built from characters that cannot appear inside a real
# Atlassian PAT, OAuth token, base64 blob or URL-encoded password.
_PROPERTY9_DETECTORS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "Authorization: Basic <blob>",
        # The redactor replaces the entire ``Authorization: Basic...``
        # run with the bare ``***REDACTED***`` sentinel, so the
        # ``Authorization:`` prefix vanishes and this detector cannot
        # match. The negative lookahead is defensive in case the
        # redactor ever moves to a ``Authorization: ***REDACTED***``
        # shape — the placeholder starts with ``*`` which is not in
        # our blob alphabet anyway, but the lookahead keeps the
        # invariant explicit.
        re.compile(r"(sectioni)Authorization:\s*Basic\s+(section!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "Bearer <token>",
        # Negative lookahead avoids matching ``Bearer ***REDACTED***``
        # (the placeholder starts with ``*`` and our value alphabet
        # would otherwise stop just before it, leaving ``Bearer`` +
        # whitespace + nothing — which the ``{4,}`` quantifier rejects.
        # The lookahead is belt-and-braces so a future placeholder
        # change cannot regress the property.).
        re.compile(r"(sectioni)Bearer\s+(section!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "api_token=<value>",
        re.compile(r"(sectioni)api_token=(section!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "password=<value>",
        re.compile(r"(sectioni)password=(section!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "secret=<value>",
        re.compile(r"(sectioni)secret=(section!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
)


# Hypothesis strategies
# ~~~~~~~~~~~~~~~~~~~~~


_secret_value_strategy: st.SearchStrategy[str] = st.text(
    alphabet=_PROPERTY9_VALUE_ALPHABET,
    min_size=4,
    max_size=40,
)


_noise_word_strategy: st.SearchStrategy[str] = st.text(
    alphabet=_PROPERTY9_NOISE_ALPHABET,
    min_size=1,
    max_size=20,
)


@st.composite
def _credential_token(draw: st.DrawFn) -> tuple[str, str, str]:
    """Draw one credential-bearing token.

 Returns a ``(family, value, rendered_token)`` triple where:

 * ``family`` is one of ``"basic"``, ``"bearer"``, ``"api_token"``,
 ``"password"``, ``"secret"`` — used by assertions to look up
 the expected post-redaction shape.
 * ``value`` is the random secret blob the token carries.
 * ``rendered_token`` is the literal string that will be embedded
 into the log line.

 The ``KEY=`` families are rendered with case variations
 (lowercase / uppercase / mixed) drawn from a small finite set so
 the redactor's case-insensitivity is exercised without exploding
 the search space.
 """

    family = draw(
        st.sampled_from(
            ("basic", "bearer", "api_token", "password", "secret")
        )
    )
    value = draw(_secret_value_strategy)

    if family == "basic":
        # Header label case variations. The value has its own
        # bounded alphabet so a trailing ``=`` is fine — the
        # redactor's ``\S+`` value run consumes it.
        prefix = draw(
            st.sampled_from(
                (
                    "Authorization: Basic ",
                    "authorization: basic ",
                    "Authorization: Basic ",  # double space
                    "AUTHORIZATION: BASIC ",
                )
            )
        )
        return ("basic", value, f"{prefix}{value}")

    if family == "bearer":
        prefix = draw(
            st.sampled_from(
                ("Bearer ", "bearer ", "BEARER ")
            )
        )
        return ("bearer", value, f"{prefix}{value}")

    # KEY=value families (api_token, password, secret).
    key_variants: dict[str, tuple[str, ...]] = {
        "api_token": ("api_token", "API_TOKEN", "Api_Token"),
        "password": ("password", "PASSWORD", "Password"),
        "secret": ("secret", "SECRET", "Secret"),
    }
    key = draw(st.sampled_from(key_variants[family]))
    return (family, value, f"{key}={value}")


@st.composite
def _log_line(draw: st.DrawFn) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Draw a full log line composed of credential tokens + noise.

 Returns ``(rendered_line, credentials, noise_words)`` where:

 * ``credentials`` is the list of ``(family, value, token)``
 triples actually inserted into the line;
 * ``noise_words`` is the list of plain noise words inserted
 alongside the credentials (kept separate so the property can
 distinguish "secret value leaked" from "noise word coincides
 with a secret value").

 The rendered line is the space-joined concatenation of credential
 tokens and noise words in a randomly-chosen interleaving.
 """

    creds: list[tuple[str, str, str]] = draw(
        st.lists(_credential_token(), min_size=1, max_size=4)
    )
    noise: list[str] = draw(
        st.lists(_noise_word_strategy, min_size=0, max_size=4)
    )

    # Interleave deterministically based on a permutation drawn from
    # Hypothesis — keeps ordering reproducible per example.
    pieces: list[str] = [token for _f, _v, token in creds] + noise
    perm = draw(st.permutations(list(range(len(pieces)))))
    line = " ".join(pieces[i] for i in perm)
    return (line, creds, noise)


# invariant — randomised log line redaction
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


@given(generated=_log_line())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property9_redaction_drops_all_credential_patterns(
    generated: tuple[str, list[tuple[str, str, str]], list[str]],
) -> None:
    """invariant — known credential desenleri redaction sonrası kalmaz.



 For an arbitrary log line composed of ``Authorization: Basic …``,
 ``Bearer …``, ``api_token=…``, ``password=…``, ``secret=…`` tokens
 plus noise:

 1. Each generated secret value is absent from the redacted output
 (no-leak;.
 2. No detector regex matches anywhere in the redacted output —
 i.e. *no* credential-bearing shape survives, even one whose
 value happens to coincide with a noise word
 — failed test reports the surviving pattern).
 3. ``KEY=`` families render as ``KEY=***REDACTED***`` so operators
 can still see *which* credential was masked.
 4. ``Authorization: Basic …`` and ``Bearer …`` runs collapse to
 the bare ``***REDACTED***`` sentinel (no key prefix, since
 these are header echoes, not key/value pairs).
 5. The redactor is idempotent — applying it twice gives the same
 string as applying it once — sentinel is
 opaque to every pattern).
 """

    line, creds, noise = generated

    # ``assume`` filters: discard pathological draws where a
    # generated secret value happens to coincide with — or appear as
    # a substring of — an unrelated noise word or another credential
    # token. Those cases would fail the no-leak invariant for a
    # reason unrelated to the redactor's correctness.
    for _family, value, _token in creds:
        for noise_word in noise:
            assume(value not in noise_word)
            assume(noise_word not in value)
        for other_family, other_value, _other_token in creds:
            if (_family, value) is (other_family, other_value):
                continue
            # Two distinct credential tokens may share a value — both
            # tokens get redacted, so the value still vanishes. No
            # ``assume`` needed here.

    redacted = _platform_redact_text(line)

    # Invariant 1 — no plain-text value of any generated secret
    # survives. Bounded by ``len(value) >= 4`` to dodge the
    # exceptionally rare 1–3 char collisions with the random noise
    # alphabet (the value strategy has ``min_size=4`` so this guard
    # is defensive — it should never trigger).
    for family, value, token in creds:
        assert value not in redacted, (
            f"invariant violated: secret value from family "
            f"{family!r} (token={token!r}, value={value!r}) leaked "
            f"into redacted output: {redacted!r}. "
            f"Original line: {line!r}."
        )

    # Invariant 2 — no detector regex matches anywhere. This is the
    # *structural* check: even if no specific generated value
    # survives, the redactor must not leave behind a plausible
    # ``KEY=blob`` or ``Bearer blob`` shape that another consumer
    # might re-interpret as a credential.
    for label, detector in _PROPERTY9_DETECTORS:
        match = detector.search(redacted)
        assert match is None, (
            f"invariant violated: detector {label!r} matched "
            f"{match.group(0)!r} in redacted output: {redacted!r}. "
            f"Original line: {line!r}."
        )

    # Invariant 3 — for ``KEY=`` families, the key name survives
    # as ``KEY=***REDACTED***``.
    for family, _value, token in creds:
        if family in ("api_token", "password", "secret"):
            # Recover the key as it was rendered (preserves case).
            key = token.split("=", 1)[0]
            expected = f"{key}={REDACTION_PLACEHOLDER}"
            assert expected in redacted, (
                f"invariant violated: expected ``{expected}`` in "
                f"redacted output but it is missing. "
                f"Family={family!r}, token={token!r}, "
                f"redacted output: {redacted!r}, "
                f"original line: {line!r}."
            )

    # Invariant 4 — ``Authorization: Basic …`` and ``Bearer …`` runs
    # collapse to the bare sentinel.
    for family, _value, _token in creds:
        if family in ("basic", "bearer"):
            assert REDACTION_PLACEHOLDER in redacted, (
                f"invariant violated: expected sentinel "
                f"``{REDACTION_PLACEHOLDER}`` after redacting a "
                f"{family!r} token, but redacted output is "
                f"{redacted!r}. Original line: {line!r}."
            )

    # Invariant 5 — idempotency. Running the redactor a second time
    # must not change the output.
    redacted_twice = _platform_redact_text(redacted)
    assert redacted_twice == redacted, (
        f"invariant violated: redactor is not idempotent. "
        f"first pass: {redacted!r}, second pass: {redacted_twice!r}, "
        f"original line: {line!r}."
    )


@given(generated=_log_line())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property9_logging_filter_drops_credentials_through_handlers(
    generated: tuple[str, list[tuple[str, str, str]], list[str]],
) -> None:
    """invariant —:class:`RedactionFilter` masks credentials at the handler.



 Asserts the same no-leak invariant for the integrated logging
 path: a:class:`RedactionFilter` attached to a real:class:`logging.Handler` rewrites every record so the formatter
 only ever sees redacted content. This complements the
 pure-string:func:`redact_text` check above by exercising the
 ``logging.LogRecord`` mutation path (``record.msg`` /
 ``record.args``).
 """

    line, creds, noise = generated

    # Same ``assume`` filter as the pure-string property — a
    # generated secret value must not coincide with (or be a
    # substring of) any noise word, otherwise the no-leak check
    # fails for a reason unrelated to the redactor.
    for _family, value, _token in creds:
        for noise_word in noise:
            assume(value not in noise_word)
            assume(noise_word not in value)

    # Wire a fresh logger + StringIO handler with the platform
    # ``RedactionFilter`` attached. Each example uses a distinct
    # logger name so handlers / filters never bleed across draws.
    import io
    import uuid

    logger = logging.getLogger(f"property9.{uuid.uuid4().hex}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()
    logger.filters.clear()

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_PlatformRedactionFilter())
    logger.addHandler(handler)

    try:
        # Exercise both common log-call shapes:
        #
        # * ``logger.info(line)`` — secret arrives via ``record.msg``;
        # * ``logger.info("got %s", line)`` — secret arrives via
        # ``record.args``.
        logger.info(line)
        logger.info("got %s", line)
        emitted = buf.getvalue()

        # Invariant 1 — every generated secret value is absent.
        for family, value, token in creds:
            assert value not in emitted, (
                f"invariant violated (handler path): secret value "
                f"from family {family!r} (token={token!r}, "
                f"value={value!r}) leaked through "
                f"RedactionFilter into emitted output: "
                f"{emitted!r}. Original line: {line!r}."
            )

        # Invariant 2 — no detector regex matches.
        for label, detector in _PROPERTY9_DETECTORS:
            match = detector.search(emitted)
            assert match is None, (
                f"invariant violated (handler path): detector "
                f"{label!r} matched {match.group(0)!r} in emitted "
                f"output: {emitted!r}. Original line: {line!r}."
            )
    finally:
        logger.handlers.clear()
        logger.filters.clear()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
#
#
# This property verifies that captured container logs do NOT contain
# sensitive substrings that would indicate credential leakage. It
# operates in two modes:
#
# 1. **Evidence-file mode:** If ``vps-test-evidence/17-logs-*.txt``
# files exist (produced by the VPS E2E observability step), every
# line in every file is scanned for the forbidden literal
# substrings: ``Bearer ATCTT3x``, ``Bearer ATATT3x``,
# ``sk-proj-``, ``password=ai_dev_only``, ``ATATT3x``, ``ATCTT3x``.
#
# 2. **Hypothesis-driven mode:** Random log lines are generated by
# concatenating noise text with seeded sensitive patterns. The
# platform:func:`redact_text` function is applied and the output
# is verified to be free of the sensitive substrings.
#
# Together these two modes ensure that:
# - Real VPS deployment logs (when available) contain no leaked secrets.
# - The redaction layer correctly sterilizes any log line that
# accidentally includes credential-bearing patterns.

import glob  # noqa: E402 -- placed near use site for locality
from pathlib import Path as _P6Path  # noqa: E402 -- avoid shadowing earlier Path

# Sensitive substrings that MUST NOT appear in any log output.
# These correspond to the actual credential prefixes/patterns from
# credentials.md used in the VPS E2E deployment test.
_P6_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "Bearer ATCTT3x",
    "Bearer ATATT3x",
    "sk-proj-",
    "password=ai_dev_only",
    "ATATT3x",
    "ATCTT3x",
)

# Platform root - evidence files live at ``<platform>/vps-test-evidence/``
_P6_WORKSPACE_ROOT = _P6Path(__file__).resolve().parents[2]
_P6_EVIDENCE_GLOB = str(_P6_WORKSPACE_ROOT / "vps-test-evidence" / "17-logs-*.txt")


# ---------------------------------------------------------------------------
# Mode 1: Evidence-file scan (concrete test against real captured logs)
# ---------------------------------------------------------------------------


def test_property6_evidence_logs_contain_no_secrets() -> None:
    """invariant — captured VPS log files contain no sensitive substrings.



 Scans all ``vps-test-evidence/17-logs-*.txt`` files (if they exist)
 and asserts that no line contains any of the forbidden sensitive
 substrings. If no evidence files exist (test not yet run on VPS),
 the test is skipped with a clear message.
 """

    log_files = glob.glob(_P6_EVIDENCE_GLOB)

    if not log_files:
        pytest.skip(
            "No vps-test-evidence/17-logs-*.txt files found — "
            "VPS E2E observability step has not been executed yet. "
            "This test will pass once evidence files are captured."
        )

    violations: list[str] = []

    for log_file_path in sorted(log_files):
        file_path = _P6Path(log_file_path)
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"Cannot read {file_path.name}: {exc}")
            continue

        for line_no, line in enumerate(content.splitlines(), start=1):
            for pattern in _P6_FORBIDDEN_SUBSTRINGS:
                if pattern in line:
                    # Redact the actual secret from the violation report
                    redacted_line = line.replace(pattern, f"[LEAKED:{pattern[:8]}...]")
                    violations.append(
                        f"{file_path.name}:{line_no} contains "
                        f"forbidden pattern '{pattern[:8]}...': "
                        f"{redacted_line[:120]}"
                    )

    assert not violations, (
        f"invariant violated: {len(violations)} sensitive substring(s) "
        f"found in captured log files (the operational rule):\n"
        + "\n".join(f" - {v}" for v in violations[:20])
        + ("\n... (truncated)" if len(violations) > 20 else "")
    )


# ---------------------------------------------------------------------------
# Mode 2: Hypothesis-driven redaction verification
# ---------------------------------------------------------------------------
#
# The platform redactor (``http_shared.redaction.redact_text``) handles
# five pattern families: ``Authorization: Basic``, ``Bearer``,
# ``api_token=``, ``password=``, ``secret=``. The VPS E2E forbidden
# substrings map to these families as follows:
#
# - ``Bearer ATCTT3x...`` → caught by the ``Bearer <token>`` pattern
# - ``Bearer ATATT3x...`` → caught by the ``Bearer <token>`` pattern
# - ``password=ai_dev_only`` → caught by the ``password=<value>`` pattern
#
# The remaining patterns (``sk-proj-``, raw ``ATATT3x``, raw ``ATCTT3x``
# without Bearer prefix) are NOT covered by the current redactor — they
# would require additional regex rules. Mode 1 (evidence file scan)
# catches these at the file level; Mode 2 verifies the redactor handles
# the patterns it IS designed to catch.

# Templates for patterns the redactor IS designed to handle.
# Each entry: (forbidden_substring_to_check, template_for_log_line)
_P6_REDACTABLE_TEMPLATES: tuple[tuple[str, str], ...] = (
    # Bitbucket Workspace Access Token (Bearer auth)
    ("Bearer ATCTT3x", "Bearer ATCTT3x{suffix}"),
    # Bitbucket Personal API Token (Bearer auth)
    ("Bearer ATATT3x", "Bearer ATATT3x{suffix}"),
    # Postgres password in connection string style
    ("password=ai_dev_only", "password=ai_dev_only{suffix}"),
)

# Noise alphabet for surrounding log context — lowercase + digits +
# common log punctuation. Excludes characters that could accidentally
# form one of the forbidden patterns.
_P6_NOISE_ALPHABET: str = string.ascii_lowercase + string.digits + ".:/-_[]"

_p6_noise_strategy: st.SearchStrategy[str] = st.text(
    alphabet=_P6_NOISE_ALPHABET,
    min_size=5,
    max_size=60,
)

_p6_suffix_strategy: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "+/=._-",
    min_size=8,
    max_size=40,
)


@st.composite
def _p6_log_line_with_secrets(draw: st.DrawFn) -> tuple[str, list[str]]:
    """Generate a log line containing one or more seeded sensitive patterns.

 Returns ``(log_line, expected_forbidden_substrings)`` where:
 - ``log_line`` is a realistic log line with embedded secrets
 - ``expected_forbidden_substrings`` lists the patterns that should
 be absent after redaction (only patterns the redactor handles)
 """
    # Pick 1-3 sensitive patterns to embed
    num_secrets = draw(st.integers(min_value=1, max_value=3))
    chosen_templates = draw(
        st.lists(
            st.sampled_from(_P6_REDACTABLE_TEMPLATES),
            min_size=num_secrets,
            max_size=num_secrets,
        )
    )

    # Generate surrounding noise
    prefix_noise = draw(_p6_noise_strategy)
    suffix_noise = draw(_p6_noise_strategy)

    # Build the log line with embedded secrets
    parts: list[str] = [prefix_noise]
    forbidden: list[str] = []

    for pattern_prefix, template in chosen_templates:
        secret_suffix = draw(_p6_suffix_strategy)
        secret_fragment = template.format(suffix=secret_suffix)
        parts.append(secret_fragment)
        forbidden.append(pattern_prefix)
        # Add inter-secret noise
        parts.append(draw(_p6_noise_strategy))

    parts.append(suffix_noise)
    log_line = " ".join(parts)

    return (log_line, forbidden)


@given(generated=_p6_log_line_with_secrets())
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property6_redactor_removes_sensitive_patterns(
    generated: tuple[str, list[str]],
) -> None:
    """invariant — platform redactor sterilizes VPS credential patterns.



 For any log line containing seeded sensitive patterns that the
 platform redactor is designed to handle (Bearer tokens, password=
 values), the:func:`redact_text` function MUST produce output
 where none of the forbidden substrings survive.

 This exercises the redaction layer against the specific credential
 patterns used in the VPS E2E deployment test that fall within the
 redactor's documented pattern set.
 """

    log_line, forbidden_patterns = generated

    # Apply the platform redaction filter
    redacted = _platform_redact_text(log_line)

    # Verify: no forbidden substring survives in the redacted output
    for pattern in forbidden_patterns:
        assert pattern not in redacted, (
            f"invariant violated (the operational rule): sensitive pattern "
            f"'{pattern[:8]}...' survived redaction. "
            f"Redacted output: {redacted!r}. "
            f"Original line: {log_line!r}."
        )


@given(generated=_p6_log_line_with_secrets())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_property6_redaction_idempotent_for_vps_patterns(
    generated: tuple[str, list[str]],
) -> None:
    """invariant — redaction is idempotent for VPS credential patterns.



 Applying the redactor twice produces the same result as applying
 it once. This ensures that the redaction sentinel itself does not
 trigger further redaction passes.
 """

    log_line, _forbidden = generated

    redacted_once = _platform_redact_text(log_line)
    redacted_twice = _platform_redact_text(redacted_once)

    assert redacted_twice == redacted_once, (
        f"invariant violated: redaction is not idempotent for VPS "
        f"credential patterns. First pass: {redacted_once!r}, "
        f"second pass: {redacted_twice!r}, "
        f"original: {log_line!r}."
    )
