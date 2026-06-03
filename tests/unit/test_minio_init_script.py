"""Static checks for ``infra/minio/init.sh``.

These tests do NOT execute the script — bash is not always available
on Windows dev machines and the script needs a live MinIO endpoint
to do anything useful. Instead we lock down the contract operators
rely on:

* The script exists at the path the README documents.
* It declares the canonical bucket names ``audit-archive`` and
  ``ai-runs``.
* It uses ``set -euo pipefail`` so any un-handled error fails fast.
* It honours the dev-mode env vars defined in
  ``infra/docker-compose.yml`` (MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)
  with sensible defaults.
* It treats ``409 BucketAlreadyOwnedByYou`` as success (idempotency).

The test file lives in ``platform/tests/unit/`` so it ships alongside
other infra-shape tests (compose structure, dockerfile shape, ...)
and is picked up by the workspace-level ``pytest tests/`` invocation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_INIT_SCRIPT: Path = (
    Path(__file__).resolve().parents[2] / "infra" / "minio" / "init.sh"
)


@pytest.fixture(scope="module")
def script_text() -> str:
    """Read the bootstrap script once per test module."""

    assert _INIT_SCRIPT.is_file(), (
        f"infra/minio/init.sh missing at {_INIT_SCRIPT}; "
        "the MinIO bootstrap script is required"
    )
    return _INIT_SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_script_starts_with_bash_shebang(script_text: str) -> None:
    first_line = script_text.splitlines()[0]
    # Accept ``/usr/bin/env bash`` or any explicit /bin/bash.
    assert re.match(r"^#!.*\bbash\b", first_line), (
        f"init.sh must start with a bash shebang; got {first_line!r}"
    )


def test_script_uses_strict_mode(script_text: str) -> None:
    """``set -euo pipefail`` so a single failure aborts the run."""

    assert re.search(r"^set -euo pipefail\b", script_text, re.MULTILINE), (
        "init.sh must enable strict mode via 'set -euo pipefail'"
    )


def test_script_declares_required_buckets(script_text: str) -> None:
    """Both canonical bucket names appear with sensible defaults.

    The script defines them as ``${VAR:-default}`` env-overridable
    constants; this assertion just makes sure operators reading the
    file see the bucket names without hunting through the Compose
    env.
    """

    # The defaults appear inside ``${VAR:-default}`` — match the
    # literal substring rather than insisting on surrounding quotes
    # (the bash assignment is ``X="${X:-audit-archive}"``).
    assert "AUDIT_ARCHIVE_BUCKET" in script_text
    assert ":-audit-archive}" in script_text
    assert "AI_RUNS_BUCKET" in script_text
    assert ":-ai-runs}" in script_text


def test_required_buckets_array_includes_audit_archive(script_text: str) -> None:
    """The REQUIRED_BUCKETS array is the bootstrap's source of truth."""

    match = re.search(
        r"REQUIRED_BUCKETS\s*=\s*\(([^)]+)\)", script_text
    )
    assert match, "REQUIRED_BUCKETS array must be declared"
    body = match.group(1)
    assert "AUDIT_ARCHIVE_BUCKET" in body, (
        "REQUIRED_BUCKETS must include the AUDIT_ARCHIVE_BUCKET variable"
    )


# ---------------------------------------------------------------------------
# Idempotency contract
# ---------------------------------------------------------------------------


def test_409_bucket_already_owned_treated_as_success(script_text: str) -> None:
    """The HTTP fallback maps 409 → success log line, not a die()."""

    # Find the case statement that handles status codes.
    case_match = re.search(
        r"case\s+\"\$\{?http_status\}?\"\s+in(.*?)esac",
        script_text,
        re.DOTALL,
    )
    assert case_match, "HTTP status case statement not found"
    case_body = case_match.group(1)
    # ``409)`` clause must call ``log`` (not ``die``).
    branch = re.search(r"409\)(.*?);;", case_body, re.DOTALL)
    assert branch, "init.sh must handle 409 (BucketAlreadyOwnedByYou)"
    assert "log " in branch.group(1)
    assert "die " not in branch.group(1)


def test_mc_path_uses_ignore_existing(script_text: str) -> None:
    """``mc mb --ignore-existing`` keeps the mc branch idempotent too."""

    assert "mc mb --ignore-existing" in script_text, (
        "init.sh must use 'mc mb --ignore-existing' for idempotency"
    )


# ---------------------------------------------------------------------------
# Env var defaults aligned with infra/docker-compose.yml
# ---------------------------------------------------------------------------


def test_default_env_vars_match_compose_defaults(script_text: str) -> None:
    """Defaults match the values in ``infra/docker-compose.yml``."""

    # MINIO_ROOT_USER / MINIO_ROOT_PASSWORD defaults must reuse the
    # same dev-mode placeholders the Compose file uses, otherwise
    # ``bash init.sh`` against the default Compose stack would fail.
    assert 'MINIO_ROOT_USER:-minio' in script_text
    assert 'MINIO_ROOT_PASSWORD:-miniosecret_dev_only' in script_text


def test_endpoint_defaults_to_localhost(script_text: str) -> None:
    assert 'MINIO_ENDPOINT:-localhost:9000' in script_text


# ---------------------------------------------------------------------------
# Reachability probe
# ---------------------------------------------------------------------------


def test_health_probe_targets_official_endpoint(script_text: str) -> None:
    """The probe hits ``/minio/health/live`` (matches the Compose
    healthcheck for the same service)."""

    assert "/minio/health/live" in script_text


def test_probe_has_bounded_attempts(script_text: str) -> None:
    """The probe loop must terminate; we assert a numeric upper bound."""

    match = re.search(r"max_attempts\s*=\s*(\d+)", script_text)
    assert match, "init.sh must declare a numeric max_attempts ceiling"
    attempts = int(match.group(1))
    assert 1 <= attempts <= 100, (
        f"max_attempts={attempts} is outside the sane [1, 100] range"
    )
