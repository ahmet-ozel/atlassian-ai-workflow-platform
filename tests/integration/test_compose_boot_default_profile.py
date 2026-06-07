"""Integration smoke tests: default-profile Compose stack.

This module hosts two complementary smoke tests with different
runtime envelopes:

1. ``test_compose_default_profile_config_parses_with_ten_services``
 - runs ``docker compose -f
 infra/docker-compose.yml config`` and asserts the parsed YAML
 exposes every one of the 10 manifest entries from
 ``platform/config/services.manifest.json``. When the ``docker``
 CLI is absent (CI fast-lane, sandboxed dev machines), the test
 falls back to parsing ``infra/docker-compose.yml`` directly with
 PyYAML so the foundation invariant ("manifest ↔ Compose parity")
 stays enforced even without a Docker daemon. This test does **not**
 require ``--run-docker`` - parsing is cheap, deterministic, and
 carries no side effects.

2. ``test_compose_default_profile_boots_and_services_are_healthy`` -
 brings the stack up via ``docker compose up -d`` and probes each
 service's ``/healthz`` endpoint. This is gated
 behind the ``--run-docker`` pytest flag because it spins up real
 containers and binds host ports. When the flag is absent (the
 default), the test SKIPs cleanly so the fast-lane suite stays
 self-contained.

The test never asserts on services that require external network
access during their startup probe (``firecrawl`` pulls upstream
dependencies; ``opencode-sidecar`` requires a vLLM endpoint). Those
are left to be observed via Compose's own ``depends_on:
service_healthy`` ordering rather than re-asserted from the host.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------

#: Compose file path relative to the workspace root.
COMPOSE_FILE_REL: str = "infra/docker-compose.yml"

#: Maximum wall-clock time to wait for every probed endpoint to start
#: returning a successful response. The first boot of a fresh stack
#: pulls images and runs Postgres/Temporal init scripts, so the timeout
#: leaves room for slow first-time startup.
BOOT_TIMEOUT_SECONDS: float = 180.0

#: Polling cadence between health probes. 2s keeps load on the docker
#: daemon negligible without making the test wall-clock dominated by
#: sleep latency.
POLL_INTERVAL_SECONDS: float = 2.0


@dataclass(frozen=True)
class HealthEndpoint:
    """A single host-side health probe.

 ``url`` is the URL the test polls from the host; ``service`` is the
 Compose service name the URL maps to (used purely for diagnostics
 in failure messages).
 """

    service: str
    url: str


#: Endpoints probed under the default profile. ``task-intake-service``
#: (port 8083) is intentionally absent - it is profile-gated and MUST
#: NOT come up without ``--profile task-intake``.
DEFAULT_PROFILE_ENDPOINTS: tuple[HealthEndpoint, ...] = (
    # Application HTTP services : every HTTP service
    # exposes /healthz on its container port; published 1:1 to the host
    # in the base Compose file).
    HealthEndpoint("automation-service", "http://localhost:8080/healthz"),
    HealthEndpoint("assistant-service", "http://localhost:8081/healthz"),
    HealthEndpoint("admin-dashboard-api", "http://localhost:8082/healthz"),
    HealthEndpoint("admin-dashboard-ui", "http://localhost:3000/api/health"),
    # Atlassian MCP (built from atlassian_mcp_bitbucket/). Published on
    # 8090; carries a /healthz endpoint per the
    # Compose-level healthcheck.
    HealthEndpoint("atlassian-mcp", "http://localhost:8090/healthz"),
)


#: Compose service ↔ component path pairs whose ``env_file:`` directive
#: points at a ``.env`` file that does NOT ship with the repo (only
#: ``.env.example`` does, per . The test stages
#: each file by copying ``.env.example`` → ``.env`` before bringing up
#: the stack and removes it on cleanup if the test created it.
ENV_FILE_TARGETS: tuple[str, ...] = (
    "services/automation-service",
    "services/assistant-service",
    "services/admin-dashboard-api",
    "services/task-intake-service",
    "ui/admin-dashboard",
    "ui/streamlit-app",
    "workers/agent-runner-worker",
    "workers/execution-runner-worker",
)


# ---------------------------------------------------------------------------
# Skip-gating helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Returns True iff a usable ``docker`` CLI is on PATH and the daemon
 responds to ``docker info``.

 We probe ``docker info`` instead of ``docker version`` because the
 latter succeeds even when the daemon is offline; ``docker info``
 requires a live daemon connection.
 """

    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Env file staging
# ---------------------------------------------------------------------------


def _stage_env_files(repo_root: Path) -> list[Path]:
    """Copy ``.env.example`` → ``.env`` for every Compose ``env_file:``
 target that is missing.

 Returns the list of ``.env`` files this call created so the
 teardown step can remove only those files (and not stomp on a
 user's pre-existing ``.env``).
 """

    created: list[Path] = []
    for target in ENV_FILE_TARGETS:
        component_dir = repo_root / target
        env_file = component_dir / ".env"
        env_example = component_dir / ".env.example"
        if env_file.exists():
            continue  # respect any pre-existing local override
        if not env_example.is_file():
            raise FileNotFoundError(
                f"missing .env.example for Compose env_file target: {env_example}"
            )
        env_file.write_bytes(env_example.read_bytes())
        created.append(env_file)
    return created


def _remove_staged_env_files(paths: list[Path]) -> None:
    """Best-effort cleanup of ``.env`` files staged by this test."""

    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best-effort; a leftover .env will be picked up
            # by the .gitignore rule (`*.env` per .
            pass


# ---------------------------------------------------------------------------
# Compose lifecycle helpers
# ---------------------------------------------------------------------------


def _compose_up(repo_root: Path) -> subprocess.CompletedProcess:
    """Bring the default-profile stack up in detached mode."""

    return subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "up", "-d"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_down(repo_root: Path) -> None:
    """Tear the stack down and drop named volumes.

 ``-v`` is required to drop ``pg_data`` / ``minio_data`` /
 ``agent_workspace`` so a subsequent run starts from a clean
 Postgres init-script state .
 """

    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "down", "-v"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )


def _wait_for_endpoints(
    endpoints: tuple[HealthEndpoint, ...],
    timeout: float,
    interval: float,
) -> dict[HealthEndpoint, str | None]:
    """Poll every endpoint until each returns 2xx or the timeout expires.

 Returns a mapping from endpoint → last error message (``None``
 means the endpoint became healthy within the timeout).
 """

    import httpx  # local import to keep module import cheap when skipped

    deadline = time.monotonic() + timeout
    pending: set[HealthEndpoint] = set(endpoints)
    last_errors: dict[HealthEndpoint, str | None] = {ep: "not yet probed" for ep in endpoints}

    while pending and time.monotonic() < deadline:
        for endpoint in list(pending):
            try:
                response = httpx.get(endpoint.url, timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - any transport error means "not yet up"
                last_errors[endpoint] = f"{type(exc).__name__}: {exc}"
                continue
            if 200 <= response.status_code < 300:
                last_errors[endpoint] = None
                pending.discard(endpoint)
            else:
                last_errors[endpoint] = (
                    f"HTTP {response.status_code}: {response.text[:120]}"
                )
        if pending:
            time.sleep(interval)

    return last_errors


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_compose_default_profile_boots_and_services_are_healthy(
    request: pytest.FixtureRequest, repo_root: Path
) -> None:
    """Default-profile Compose stack boots and every published HTTP
 service responds on its healthcheck endpoint.

 Validates and 16.3.

 The test is opt-in via ``--run-docker``. Without the flag (the
 default) it skips with a clear reason so CI fast-lanes don't pay
 for a Docker daemon spin-up.
 """

    if not request.config.getoption("--run-docker"):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable."
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` failed); "
            "cannot run Compose boot smoke test."
        )

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot boot stack."
    )

    staged_envs = _stage_env_files(repo_root)
    try:
        up_result = _compose_up(repo_root)
        assert up_result.returncode == 0, (
            "`docker compose up -d` failed:\n"
            f"  stdout: {up_result.stdout}\n"
            f"  stderr: {up_result.stderr}"
        )

        last_errors = _wait_for_endpoints(
            DEFAULT_PROFILE_ENDPOINTS,
            timeout=BOOT_TIMEOUT_SECONDS,
            interval=POLL_INTERVAL_SECONDS,
        )

        unhealthy = {ep: err for ep, err in last_errors.items() if err is not None}
        assert not unhealthy, (
            f"the following services did not become healthy within "
            f"{BOOT_TIMEOUT_SECONDS:.0f}s:\n"
            + "\n".join(
                f"  - {ep.service} ({ep.url}): {err}"
                for ep, err in unhealthy.items()
            )
        )

        # Sanity check: confirm task-intake-service is NOT running under
        # the default profile . A non-zero exit from
        # ``ps -q --filter`` simply means the service isn't present,
        # which is what we want.
        ps_result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                COMPOSE_FILE_REL,
                "ps",
                "-q",
                "task-intake-service",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        running_ids = ps_result.stdout.strip()
        assert not running_ids, (
            "task-intake-service is profile-gated "
            "and MUST NOT run under the default profile, but compose ps "
            f"returned: {running_ids!r}"
        )
    finally:
        _compose_down(repo_root)
        _remove_staged_env_files(staged_envs)


# ---------------------------------------------------------------------------
# the implementation - `docker compose ... config` parses with all 10 manifest entries
# ---------------------------------------------------------------------------


#: Path of the services manifest, relative to the workspace root.
SERVICES_MANIFEST_REL: str = "config/services.manifest.json"


#: The 10 foundation services required in Compose. The manifest is
#: allowed to carry additional carry-over entries (e.g.
#: ``task-intake-service``); those extras are NOT part of the
#: 10-entry foundation invariant but they still must be valid Compose
#: services and obey the per-service profile-gating rule.
FOUNDATION_COMPOSE_SERVICES: frozenset[str] = frozenset(
    {
        # kind=infra
        "atlassian-mcp",
        "firecrawl",
        # kind=http_service
        "automation-service",
        "assistant-service",
        "admin-dashboard-api",
        # kind=worker
        "agent-runner-worker",
        "execution-runner-worker",
        # kind=sidecar
        "opencode-sidecar",
        # kind=ui
        "streamlit-ui",
        "admin-dashboard-ui",
    }
)


def _load_manifest_compose_service_names(repo_root: Path) -> frozenset[str]:
    """Returns the ``compose_service_name`` set declared in the manifest.

 The foundation service set contains 10 entries; the manifest may
 carry additional carry-over entries (e.g. ``task-intake-service``). This
 loader does NOT enforce cardinality itself - that invariant is
 owned by ``test_compose_structure.py``. We just return whatever
 the manifest declares so the smoke test reports meaningful diffs
 even if the manifest drifts.
 """

    manifest_path = repo_root / SERVICES_MANIFEST_REL
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    services = manifest.get("services", [])
    return frozenset(svc["compose_service_name"] for svc in services)


def _parse_compose_via_docker(
    repo_root: Path, compose_file_rel: str
) -> tuple[dict, str]:
    """Runs ``docker compose ... config --format json`` and returns the
 parsed dict plus the raw stdout for diagnostics.

 All profiles are enabled via ``--profile "*"`` so every
 profile-gated service every foundation service
 is profile-gated under its own ``compose_service_name``) shows up
 in the parsed output. Without this flag, ``docker compose config``
 only emits services that are part of the implicit default profile,
 which would hide every gated service from the smoke check.

 Raises ``RuntimeError`` on a non-zero exit so the caller can choose
 between hard-failing the test (Compose syntax error) and falling
 back to a YAML-only parse path (Docker daemon unavailable).
 """

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            compose_file_rel,
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "`docker compose config` exited with "
            f"{result.returncode}:\n  stdout: {result.stdout[:500]}\n"
            f"  stderr: {result.stderr[:500]}"
        )
    parsed = json.loads(result.stdout)
    return parsed, result.stdout


def _parse_compose_via_yaml(repo_root: Path, compose_file_rel: str) -> dict:
    """Fallback: parse ``infra/docker-compose.yml`` directly with PyYAML.

 Used when the ``docker`` CLI is unavailable. The structural shape
 matches what ``docker compose config --format json`` returns
 (``{"services": {<name>: {...}, ...}, "volumes": {...}, ...}``),
 so downstream assertions can stay backend-agnostic.

 PyYAML is a hard requirement of this fallback path; if it isn't
 importable, the caller skips the test rather than failing it.
 """

    import yaml  # local import - only needed in the fallback path

    compose_path = repo_root / compose_file_rel
    with compose_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_default_profile_config_parses_with_ten_services(
    repo_root: Path,
) -> None:
    """``docker compose -f infra/docker-compose.yml config`` parses
 cleanly and the parsed output exposes every manifest service.

 Validates (each manifest service is profile-gated
 in Compose) and 2.8 (single-command boot via the default profile
 must surface every ``kind ∈ {infra, http_service, worker, sidecar,
 ui}`` service).

 The test prefers ``docker compose ... config --format json`` when
 the daemon is reachable because it exercises the same parser the
 real boot uses (catches ``${VAR}`` interpolation gaps, anchor
 misuses, profile typos). When the daemon is missing, it falls back
 to a PyYAML parse of ``infra/docker-compose.yml`` so the
 invariant ("every manifest service is declared in Compose") stays
 enforced in environments without Docker (CI fast-lane,
 air-gapped dev machines).
 """

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; "
        "cannot validate manifest ↔ Compose parity."
    )

    expected_services = FOUNDATION_COMPOSE_SERVICES
    # Foundation : exactly 10 foundation services.
    assert len(expected_services) == 10, (
        "FOUNDATION_COMPOSE_SERVICES must list exactly 10 entries per "
        f"Expected service set mismatch; got {len(expected_services)}: "
        f"{sorted(expected_services)}"
    )

    # Cross-check: every foundation service is also declared in the
    # services manifest (so the manifest is a superset of the
    # foundation set). Carry-over manifest entries like
    # ``task-intake-service`` are allowed but not required by 13.1.
    manifest_services = _load_manifest_compose_service_names(repo_root)
    foundation_missing_from_manifest = expected_services - manifest_services
    assert not foundation_missing_from_manifest, (
        "the following foundation services are missing from "
        "services.manifest.json : "
        f"{sorted(foundation_missing_from_manifest)}"
    )

    parsed: dict
    parse_source: str
    if _docker_available():
        # ``docker compose config`` reads ``env_file:`` directives even
        # though it never starts containers, so we stage missing
        # ``.env`` files from their ``.env.example`` siblings just like
        # the boot-time smoke test does. Created files are removed in
        # the ``finally`` block to leave the workspace clean.
        staged_envs = _stage_env_files(repo_root)
        try:
            try:
                parsed, _raw = _parse_compose_via_docker(repo_root, COMPOSE_FILE_REL)
                parse_source = "docker compose config"
            except RuntimeError as exc:
                # `docker compose config` failure means the file itself
                # is malformed violation) - fail hard.
                pytest.fail(str(exc))
        finally:
            _remove_staged_env_files(staged_envs)
    else:
        try:
            parsed = _parse_compose_via_yaml(repo_root, COMPOSE_FILE_REL)
            parse_source = "PyYAML fallback"
        except ImportError:
            pytest.skip(
                "Docker CLI unavailable and PyYAML is not installed; "
                "cannot validate Compose parse without one of the two."
            )

    services_section = parsed.get("services") or {}
    assert isinstance(services_section, dict), (
        f"`{parse_source}` produced a non-dict services section: "
        f"{type(services_section).__name__}"
    )

    declared_services = frozenset(services_section.keys())
    missing = expected_services - declared_services
    assert not missing, (
        f"the following foundation services are "
        f"missing from the parsed Compose output ({parse_source}): "
        f"{sorted(missing)}\n"
        f"declared in compose: {sorted(declared_services)}"
    )

    # Each manifest service must carry a non-empty ``profiles`` list
    # whose membership includes its ``compose_service_name`` (foundation
    # . The Compose CLI normalises ``profiles:`` to a
    # list; the YAML fallback preserves whatever was authored.
    profile_violations: list[str] = []
    for svc_name in sorted(expected_services):
        svc_def = services_section[svc_name]
        profiles = svc_def.get("profiles") or []
        if not isinstance(profiles, list):
            profile_violations.append(
                f"{svc_name}: profiles must be a list, got "
                f"{type(profiles).__name__}"
            )
            continue
        if svc_name not in profiles:
            profile_violations.append(
                f"{svc_name}: profiles {profiles!r} does not include "
                "the service's own compose_service_name "
                ""
            )
    assert not profile_violations, (
        "profile gating violations detected:\n - "
        + "\n - ".join(profile_violations)
    )

    # Sanity guard: vLLM is explicitly excluded from the Compose stack
    # vLLM is reached via VLLM_BASE_URL on a
    # non-Compose host).
    assert "vllm" not in declared_services, (
        "vLLM must NOT be packaged in the Compose stack (Requirement "
        "2.6); it is reached via VLLM_BASE_URL from a non-Compose host."
    )
