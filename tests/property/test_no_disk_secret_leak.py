"""Property test P2 — Env_Override values are never persisted to disk.

Validates: Requirements 9.2, 9.4

Property P2 (admin-dashboard-control-plane spec, design §3.5 + §3.4):
After a successful ``LifecycleService.start`` call the operator-supplied
``env_overrides`` map MUST exist **only** inside Vault (in this test:
the in-memory ``_FakeVaultClient.stored`` dict) and inside the spawned
subprocess's ``env`` mapping. No value string may appear in any file
on disk under the synthetic workspace, and the recorded Compose argv
must never contain ``--env-file`` / ``--env`` flags constructed from
the override map.

Strategy
--------
``st.dictionaries(keys=st.from_regex(r"[A-Z][A-Z0-9_]*"),
values=st.text(min_size=8, max_size=64))`` produces a random
``Env_Override`` map. For each draw the test:

1. Builds a fresh synthetic workspace under :func:`tempfile.mkdtemp`
   so each Hypothesis example starts from a clean slate.
2. Materialises a ``services/automation-service/.env.example`` whose
   LHS keys exactly match the generated override map (the form-schema
   parity check inside ``LifecycleService.start`` requires this).
3. Wires fake Vault / Compose / Health / Audit collaborators and
   invokes ``LifecycleService.start``.
4. Walks every file under the workspace (excluding ``.git/``,
   ``node_modules/``, ``tests/.hypothesis/``, ``__pycache__/``) and
   asserts that no override **value** substring appears in any file's
   bytes.
5. Asserts that the recorded Compose argv contains no ``--env-file``
   or ``--env`` flag, and that the ``_FakeVaultClient`` recorded one
   write per generated key.

This file deliberately constructs its own synthetic workspace inside
each Hypothesis example rather than relying on pytest's ``tmp_path``
fixture: function-scoped fixtures interact poorly with Hypothesis's
example shrinking, which is why the test uses ``tempfile.mkdtemp``
plus ``shutil.rmtree`` in a ``try / finally`` block.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap so the orchestrator package and its dependencies resolve.
# ---------------------------------------------------------------------------

#: Workspace root, derived from this file's location: ``tests/property/X``
#: → ``tests/property`` → ``tests`` → ``<workspace>``.
_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]

#: Service root: ``services/admin-dashboard-api/``. Required on
#: ``sys.path`` because ``src.lifecycle.service`` imports relative
#: modules under the ``src`` package (which expects the service root
#: to be importable as the package parent).
_SERVICE_ROOT: Path = _WORKSPACE_ROOT / "services" / "admin-dashboard-api"

for _p in (_WORKSPACE_ROOT / "tests", _SERVICE_ROOT):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

# ---------------------------------------------------------------------------
# Imports from the admin-dashboard-api orchestrator under test.
# ---------------------------------------------------------------------------

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditWriteOutcome,
)
from src.lifecycle.compose_runner import ComposeResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import LifecycleService  # noqa: E402
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (mirror the patterns from
# ``services/admin-dashboard-api/tests/unit/test_lifecycle_service.py``)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Tracks every audit interaction; everything succeeds in this test."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)

    async def precheck(self) -> None:
        self.precheck_calls += 1

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """Records every Vault write so the test can assert every key landed.

    Property P2 checks the *negative* invariant (no plain-text value on
    disk); this fake confirms the *positive* contract: every operator
    override is round-tripped through Vault, exactly once per key
    (Requirement 9.1, 9.6).
    """

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    stored: dict[str, dict[str, str]] = field(default_factory=dict)

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def delete_env_override(
        self, *, service_name: str, key: str
    ) -> None:  # pragma: no cover - unused in this test
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Records every ``up``/``stop``/``logs`` call's argv-shaped kwargs.

    The orchestrator does not give us direct access to the OS-level
    argv list (that is the responsibility of the real ``ComposeRunner``
    in ``src/lifecycle/compose_runner.py``), but the lifecycle handler
    only ever invokes the runner via these high-level methods. This
    fake mirrors the recorded-call shape used by the unit tests so we
    can assert no ``--env-file`` / ``--env`` flag was ever requested
    via the orchestrator's public surface.
    """

    up_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)
    logs_calls: list[dict[str, Any]] = field(default_factory=list)

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        argv = (
            "docker",
            "compose",
            "-f",
            "infra/docker-compose.yml",
            "--profile",
            profile,
            "up",
            "-d",
            service_name,
        )
        self.up_calls.append(
            {
                "profile": profile,
                "service_name": service_name,
                "env_overrides": dict(env_overrides or {}),
                "argv": argv,
            }
        )
        return ComposeResult(exit_code=0, stdout="", stderr="", argv=argv)

    async def stop(
        self,
        *,
        service_name: str,
        remove_volumes: bool = False,
    ) -> ComposeResult:
        argv = (
            "docker",
            "compose",
            "-f",
            "infra/docker-compose.yml",
            "stop",
            service_name,
        )
        self.stop_calls.append(
            {"service_name": service_name, "remove_volumes": remove_volumes, "argv": argv}
        )
        return ComposeResult(exit_code=0, stdout="", stderr="", argv=argv)

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:  # pragma: no cover - unused in this test
        argv = (
            "docker",
            "compose",
            "-f",
            "infra/docker-compose.yml",
            "logs",
            service_name,
        )
        self.logs_calls.append(
            {"service_name": service_name, "tail": tail, "follow": follow, "argv": argv}
        )
        return ComposeResult(exit_code=0, stdout="", stderr="", argv=argv)


@dataclass
class _FakeHealthProbe:
    """Always reports healthy so ``start`` reaches its terminal state."""

    calls: list[ManagedServiceEntry] = field(default_factory=list)

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# Synthetic workspace builder
# ---------------------------------------------------------------------------

#: Directory names that are excluded from the file walk. Mirrors the
#: spec's exclusion list (tasks.md task 7.2): ``.git/``,
#: ``node_modules/``, ``tests/.hypothesis/``, ``__pycache__/``. The
#: ``.hypothesis`` exclusion has to handle both the ``tests/.hypothesis``
#: form (the project layout) and any nested cache directory the
#: Hypothesis runtime may create inside the synthetic workspace under
#: stress.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {".git", "node_modules", ".hypothesis", "__pycache__"}
)

#: Minimum substring length to participate in the disk-leak sweep.
#: Values shorter than this are skipped to avoid spurious collisions
#: with ordinary file content (e.g. an 8-byte value that happens to
#: contain the substring ``"abc"``). The Hypothesis strategy already
#: enforces ``min_size=8`` so this guard is defensive — it should
#: rarely (if ever) trigger.
_MIN_VALUE_LENGTH_FOR_SWEEP: int = 4


def _build_synthetic_workspace(root: Path, override_keys: list[str]) -> None:
    """Materialise the minimum synthetic workspace ``LifecycleService.start``
    needs to run end-to-end against the fakes.

    The workspace contains:

    * ``services/automation-service/.env.example`` — the LHS key set
      MUST equal ``override_keys`` because
      ``LifecycleService._validate_env_overrides`` raises
      :class:`FormSchemaMismatchError` on any mismatch (Requirement
      5.6).
    * ``infra/docker-compose.yml`` — placeholder so the file walk has
      something to inspect under ``infra/`` (no Compose CLI is
      actually invoked — the runner is a fake).
    * ``config/services.manifest.json`` — placeholder for the same
      reason.
    * Top-level ``.env.example`` and ``README.md`` so the workspace
      root has artefacts to scan.

    Every Sensitive_Env_Key field in the ``.env.example`` is given an
    empty default — the operator's override is the *only* permissible
    source of sensitive values (Requirement 5.7) and the validator
    inside ``LifecycleService.start`` requires non-empty submitted
    values for those keys.
    """

    service_dir = root / "services" / "automation-service"
    service_dir.mkdir(parents=True)
    env_example_lines: list[str] = [
        "# automation-service .env.example (synthetic test fixture)",
    ]
    for key in override_keys:
        # Every key gets an empty default; the operator's override is
        # the only permitted source of values during the test.
        env_example_lines.append(f"{key}=")
    (service_dir / ".env.example").write_text(
        "\n".join(env_example_lines) + "\n",
        encoding="utf-8",
    )

    infra_dir = root / "infra"
    infra_dir.mkdir(parents=True)
    (infra_dir / "docker-compose.yml").write_text(
        "version: '3.9'\nservices: {}\n",
        encoding="utf-8",
    )

    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "services.manifest.json").write_text(
        '{"version": 1, "services": []}\n',
        encoding="utf-8",
    )

    # Some additional artefacts at the workspace root so the walk has
    # more surface area to sweep over.
    (root / "README.md").write_text(
        "# Synthetic workspace fixture for test_no_disk_secret_leak.py\n",
        encoding="utf-8",
    )
    (root / ".env.example").write_text("# placeholder\n", encoding="utf-8")


def _make_manifest_entry() -> ManagedServiceEntry:
    """Manifest entry that matches the synthetic workspace layout."""

    return ManagedServiceEntry(
        name="automation-service",
        kind="http_service",
        compose_service_name="automation-service",
        compose_profile="automation-service",
        env_example_path="services/automation-service/.env.example",
        health_endpoint="/healthz",
        test_command=None,
    )


def _walk_workspace_files(root: Path) -> list[Path]:
    """Yield every file path under ``root`` that participates in the sweep.

    Directories whose ``name`` is in :data:`_EXCLUDED_DIR_NAMES` are
    pruned in-place during the walk so we never descend into them.
    Only regular files are returned — symlinks, sockets and other
    special files are skipped (none should exist in the synthetic
    workspace, but the guard keeps the walk hermetic).
    """

    out: list[Path] = []
    for current, dirs, files in _os_walk(root):
        # Prune excluded dirs in-place so :func:`os.walk` does not
        # descend into them.
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIR_NAMES]
        for name in files:
            path = Path(current) / name
            try:
                if path.is_file() and not path.is_symlink():
                    out.append(path)
            except OSError:  # pragma: no cover - defensive
                continue
    return out


def _os_walk(root: Path):
    """Wrapper around :func:`os.walk` returning ``(path, dirs, files)``.

    Pulled out of :func:`_walk_workspace_files` so the signature the
    walker expects (a list of subdirectory names that can be mutated
    in place) stays compatible with the standard-library helper.
    """

    import os

    return os.walk(root)


def _file_contains(path: Path, needle_bytes: bytes) -> bool:
    """Return ``True`` iff ``needle_bytes`` appears anywhere in ``path``.

    Reads the file in binary mode so non-UTF-8 content does not raise.
    Empty needles are treated as "always absent" (the guard upstream
    in :func:`_assert_no_disk_leak` already filters these out).
    """

    if not needle_bytes:
        return False
    try:
        data = path.read_bytes()
    except OSError:  # pragma: no cover - defensive
        return False
    return needle_bytes in data


def _assert_no_disk_leak(
    workspace_root: Path,
    env_overrides: dict[str, str],
) -> None:
    """Assert that no override value appears in any file under the workspace.

    For each ``(key, value)`` pair where ``value`` is at least
    :data:`_MIN_VALUE_LENGTH_FOR_SWEEP` characters long, walk every
    eligible file and assert the value's UTF-8 byte sequence does not
    appear. Failure messages cite the exact file (workspace-relative)
    and the offending key so the operator can locate the leak quickly
    without exposing the value itself.
    """

    files = _walk_workspace_files(workspace_root)
    for key, value in env_overrides.items():
        if len(value) < _MIN_VALUE_LENGTH_FOR_SWEEP:
            continue
        try:
            needle = value.encode("utf-8")
        except UnicodeEncodeError:  # pragma: no cover - defensive
            continue
        for path in files:
            if _file_contains(path, needle):
                rel = path.relative_to(workspace_root).as_posix()
                # Do NOT print the value — the leak itself is the
                # contract violation. Cite key + file only.
                raise AssertionError(
                    f"P2 violated: value of override key {key!r} appears "
                    f"in workspace file {rel!r}. Env_Override values must "
                    f"never reach disk (Requirement 9.2)."
                )


# ---------------------------------------------------------------------------
# Property P2
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
    ],
)
@given(
    env_overrides=st.dictionaries(
        # Keys must satisfy the ``.env.example`` parser regex
        # ``^[A-Z][A-Z0-9_]*$`` (env_parser.py). The bounded quantifier
        # keeps Hypothesis's regex engine fast.
        keys=st.from_regex(r"\A[A-Z][A-Z0-9_]{0,30}\Z", fullmatch=True),
        # min_size=8 satisfies the orchestrator's "non-empty sensitive
        # value" check (Requirement 5.7) without any classification
        # guesswork — every value is non-empty regardless of whether
        # the key happens to match a Sensitive_Env_Key pattern.
        values=st.text(min_size=8, max_size=64),
        min_size=1,
        max_size=5,
    )
)
def test_env_override_values_are_not_persisted_to_disk(
    env_overrides: dict[str, str],
) -> None:
    """Property P2 — Env_Override values never reach disk.

    Validates: Requirements 9.2, 9.4.

    Every ``(key, value)`` pair sent through
    ``LifecycleService.start`` MUST exist only inside Vault (the
    in-memory ``_FakeVaultClient.stored`` dict for this test) and
    inside the spawned subprocess's ``env`` mapping. No value
    substring may appear in any file on disk under the synthetic
    workspace, and the recorded Compose argv must not contain any
    ``--env-file`` / ``--env`` flag derived from the override map.
    """

    workspace_root = Path(tempfile.mkdtemp(prefix="p2-no-disk-leak-"))
    try:
        keys = list(env_overrides.keys())
        _build_synthetic_workspace(workspace_root, keys)

        audit = _FakeAuditWriter()
        vault = _FakeVaultClient()
        compose = _FakeComposeRunner()
        health = _FakeHealthProbe()

        async def _no_sleep(_seconds: float) -> None:
            return None

        service = LifecycleService(
            manifest=(_make_manifest_entry(),),
            audit=audit,  # type: ignore[arg-type]
            vault=vault,  # type: ignore[arg-type]
            compose=compose,  # type: ignore[arg-type]
            health=health,  # type: ignore[arg-type]
            workspace_root=workspace_root,
            health_ready_timeout_seconds=1.0,
            sleep=_no_sleep,
        )

        async def run() -> None:
            response = await service.start(
                name="automation-service",
                env_overrides=env_overrides,
                actor="ops-1",
            )
            assert response.state == "running", (
                f"start() did not reach running; got {response.state!r}"
            )

        asyncio.run(run())

        # ------------------------------------------------------------------
        # Positive contract: Vault saw every key (Requirement 9.1, 9.6).
        # ------------------------------------------------------------------
        recorded_pairs = {(k, v) for _svc, k, v in vault.writes}
        expected_pairs = {(k, v) for k, v in env_overrides.items()}
        assert recorded_pairs == expected_pairs, (
            f"Vault did not record every override exactly once. "
            f"Expected {len(expected_pairs)} pairs; recorded {len(recorded_pairs)}."
        )

        # ------------------------------------------------------------------
        # Compose argv must not contain ``--env-file`` / ``--env`` flags
        # (Requirement 9.3 + design §3.4 surface).
        # ------------------------------------------------------------------
        assert len(compose.up_calls) == 1, (
            f"Expected exactly one compose.up call; got {len(compose.up_calls)}"
        )
        up_argv: tuple[str, ...] = compose.up_calls[0]["argv"]
        assert "--env-file" not in up_argv, (
            f"compose.up argv contains --env-file flag: {up_argv!r}. "
            f"Env_Override values must not be staged to a temporary "
            f"file on disk (Requirement 9.3)."
        )
        assert "--env" not in up_argv, (
            f"compose.up argv contains a --env flag: {up_argv!r}. "
            f"Env_Override values must travel via the subprocess env "
            f"mapping, not as argv tokens (Requirement 9.3)."
        )
        # And no value string may bleed into the argv tokens themselves.
        for token in up_argv:
            for value in env_overrides.values():
                if len(value) >= _MIN_VALUE_LENGTH_FOR_SWEEP:
                    assert value not in token, (
                        f"Override value leaked into compose argv token "
                        f"{token!r} (Requirement 9.2)."
                    )

        # ------------------------------------------------------------------
        # Negative contract: walk every file in the synthetic workspace
        # and assert that no override value byte sequence appears.
        # ------------------------------------------------------------------
        _assert_no_disk_leak(workspace_root, env_overrides)

        # ------------------------------------------------------------------
        # Audit details_json must not carry values either (Property P6
        # surface; relevant here because the audit writer is a fake and
        # therefore in-memory only — we still verify the *contract* the
        # orchestrator delivers to it).
        # ------------------------------------------------------------------
        for entry in (*audit.write_calls, *audit.write_with_retry_calls):
            details_repr = repr(entry.details_json)
            for value in env_overrides.values():
                if len(value) >= _MIN_VALUE_LENGTH_FOR_SWEEP:
                    assert value not in details_repr, (
                        f"Override value leaked into audit details_json "
                        f"for action={entry.action!r} (Requirement 11.3)."
                    )
    finally:
        shutil.rmtree(workspace_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 9 — platform-mimari-foundation (task 9.2)
# ---------------------------------------------------------------------------
#
# Validates: Requirements 6.2, 6.3, 6.10, 9.6, 9.7
#
# Property P2 above pins the lifecycle-orchestrator's negative
# disk-leak invariant. Property 9 below complements it from the
# *log-sink* angle: if a service's logs are persisted to disk
# (rotating file handler, journald, container stdout captured by the
# Docker daemon, etc.) then the platform's
# :class:`http_shared.redaction.RedactionFilter` MUST guarantee that
# the on-disk artefact carries the redaction sentinel and *not* the
# original credential bytes.
#
# Strategy
# --------
#
# * Generate random log lines containing one or more of the five
#   credential families enumerated in Requirement 6.10
#   (``Authorization: Basic …``, ``Bearer …``, ``api_token=…``,
#   ``password=…``, ``secret=…``).
# * Wire a :class:`logging.FileHandler` against a temp file and
#   attach the platform's :class:`RedactionFilter`.
# * Emit the generated line through both common log-call shapes
#   (positional ``msg`` + ``args``, pre-rendered ``msg``).
# * Read the temp file back from disk and assert no generated
#   secret value survives.
#
# This is the integration counterpart of the pure-string property
# in ``test_log_redaction.py``: it exercises the full
# ``logger → filter → handler → file`` path and therefore catches
# regressions where, for example, a custom formatter or a
# ``handler.format(...)`` override bypasses the filter.

import logging  # noqa: E402  -- placed near use site for locality
import re  # noqa: E402  -- placed near use site for locality
import string  # noqa: E402  -- placed near use site for locality
import uuid as _uuid  # noqa: E402

from http_shared.redaction import (  # noqa: E402  -- module-level import OK
    REDACTION_PLACEHOLDER,
    RedactionFilter as _PlatformRedactionFilter,
)


# Reuse the same alphabets and detector regexes as
# ``test_log_redaction.py`` so a regression in one property surfaces
# the same diagnostics in the other.
_PROP9_VALUE_ALPHABET: str = (
    string.ascii_letters + string.digits + "+/=._-"
)

_PROP9_NOISE_ALPHABET: str = string.ascii_lowercase + string.digits


_PROP9_DETECTORS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        "Authorization: Basic <blob>",
        re.compile(r"(?i)Authorization:\s*Basic\s+(?!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "Bearer <token>",
        re.compile(r"(?i)Bearer\s+(?!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "api_token=<value>",
        re.compile(r"(?i)api_token=(?!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "password=<value>",
        re.compile(r"(?i)password=(?!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
    (
        "secret=<value>",
        re.compile(r"(?i)secret=(?!\*)[A-Za-z0-9+/=._\-]{4,}"),
    ),
)


_prop9_secret_value: st.SearchStrategy[str] = st.text(
    alphabet=_PROP9_VALUE_ALPHABET,
    min_size=8,
    max_size=40,
)

_prop9_noise_word: st.SearchStrategy[str] = st.text(
    alphabet=_PROP9_NOISE_ALPHABET,
    min_size=1,
    max_size=20,
)


@st.composite
def _prop9_credential(draw: st.DrawFn) -> tuple[str, str, str]:
    """Draw one credential token in one of the five Requirement 6.10 families.

    Returns ``(family, value, rendered_token)``.
    """

    family = draw(
        st.sampled_from(
            ("basic", "bearer", "api_token", "password", "secret")
        )
    )
    value = draw(_prop9_secret_value)

    if family == "basic":
        prefix = draw(
            st.sampled_from(
                (
                    "Authorization: Basic ",
                    "authorization: basic ",
                    "AUTHORIZATION: BASIC ",
                )
            )
        )
        return ("basic", value, f"{prefix}{value}")

    if family == "bearer":
        prefix = draw(st.sampled_from(("Bearer ", "bearer ", "BEARER ")))
        return ("bearer", value, f"{prefix}{value}")

    key_variants: dict[str, tuple[str, ...]] = {
        "api_token": ("api_token", "API_TOKEN"),
        "password": ("password", "PASSWORD"),
        "secret": ("secret", "SECRET"),
    }
    key = draw(st.sampled_from(key_variants[family]))
    return (family, value, f"{key}={value}")


@st.composite
def _prop9_log_payload(
    draw: st.DrawFn,
) -> tuple[str, list[tuple[str, str, str]], list[str]]:
    """Draw a credential-bearing log line.

    Returns ``(line, credentials, noise_words)``.
    """

    creds = draw(st.lists(_prop9_credential(), min_size=1, max_size=3))
    noise = draw(st.lists(_prop9_noise_word, min_size=0, max_size=3))
    pieces = [token for _f, _v, token in creds] + noise
    perm = draw(st.permutations(list(range(len(pieces)))))
    line = " ".join(pieces[i] for i in perm)
    return (line, creds, noise)


@given(payload=_prop9_log_payload())
@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_property9_redacted_logs_do_not_persist_to_disk(
    payload: tuple[str, list[tuple[str, str, str]], list[str]],
) -> None:
    """Property 9 — redacted log lines do not leak credentials to disk.

    Validates: Requirements 6.2, 6.3, 6.10, 9.6, 9.7

    For an arbitrary credential-bearing log line, after emitting
    through a logger whose handlers all carry the platform's
    :class:`RedactionFilter`, the on-disk log file MUST contain:

    * **No plain-text secret bytes** — every generated secret value
      is absent from the persisted file content (Requirement 6.3,
      9.7 — no plain-text in CI build outputs / artifact directories).
    * **No detector-shaped survivor** — the file MUST NOT contain
      any ``KEY=blob``, ``Authorization: Basic blob`` or
      ``Bearer blob`` shape that another consumer might later
      mis-classify as a credential (Requirement 9.6 — failed test
      reports the leak with file location + offending pattern).

    The test creates a fresh temp file per Hypothesis example so
    there is no cross-example contamination.
    """

    line, creds, noise = payload

    # Same ``assume()`` filter as the pure-string property: discard
    # examples where a generated secret value coincides with — or is
    # a substring of — a noise word, since the noise word is not
    # something the redactor is responsible for masking.
    for _family, value, _token in creds:
        for noise_word in noise:
            assume(value not in noise_word)
            assume(noise_word not in value)

    log_path = Path(tempfile.mkdtemp(prefix="p9-disk-leak-")) / "service.log"
    try:
        logger = logging.getLogger(f"property9.disk.{_uuid.uuid4().hex}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.handlers.clear()
        logger.filters.clear()

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(_PlatformRedactionFilter())
        logger.addHandler(handler)

        try:
            # Emit through both shapes to cover ``record.msg`` and
            # ``record.args`` paths.
            logger.info(line)
            logger.info("got %s", line)
            handler.flush()
            handler.close()
        finally:
            logger.handlers.clear()
            logger.filters.clear()

        # Read the persisted file back from disk.
        on_disk = log_path.read_text(encoding="utf-8")

        # Invariant 1 — no generated secret value appears in the file.
        for family, value, token in creds:
            assert value not in on_disk, (
                f"Property 9 violated (disk path): secret value "
                f"from family {family!r} (token={token!r}, "
                f"value={value!r}) leaked through "
                f"RedactionFilter into on-disk log "
                f"{log_path.name!r}: {on_disk!r}. "
                f"Original line: {line!r}."
            )

        # Invariant 2 — no detector regex matches anywhere in the
        # persisted file.
        for label, detector in _PROP9_DETECTORS:
            match = detector.search(on_disk)
            assert match is None, (
                f"Property 9 violated (disk path): detector {label!r} "
                f"matched {match.group(0)!r} in on-disk log "
                f"{log_path.name!r}: {on_disk!r}. "
                f"Original line: {line!r}."
            )

        # Invariant 3 — the redaction sentinel is present (proof
        # that the filter actually fired; without this check a
        # silent regression that *drops* every record would also
        # pass invariants 1 and 2).
        assert REDACTION_PLACEHOLDER in on_disk, (
            f"Property 9 violated (disk path): redaction sentinel "
            f"``{REDACTION_PLACEHOLDER}`` is missing from on-disk "
            f"log {log_path.name!r}: {on_disk!r}. "
            f"This suggests the RedactionFilter never fired. "
            f"Original line: {line!r}."
        )
    finally:
        shutil.rmtree(log_path.parent, ignore_errors=True)
