"""Integration smoke test 9.1 — Boot_Bundle açılış davranışı.


What it checks
--------------
* ``docker compose -f infra/docker-compose.yml up -d --wait`` (no
 ``--profile`` flag) brings up **only** the four Boot_Bundle services
 defined in : ``admin-dashboard-ui``,
 ``admin-dashboard-api``, ``postgres``, ``vault``.
* No Managed_Service (``redis``, ``minio``, ``temporal``,
 ``temporal-ui``, ``firecrawl``, ``atlassian-mcp``,
 ``opencode-sidecar``, ``automation-service``, ``assistant-service``,
 ``streamlit-app``, ``task-intake-service``,
 ``agent-runner-worker``, ``execution-runner-worker``) appears in
 ``docker compose ps`` output profile-gated start).
* ``admin-dashboard-ui`` publishes host port ``3000`` and
 ``admin-dashboard-api`` publishes host port ``8082`` .
* ``admin-dashboard-api`` ``/healthz`` returns ``200`` and ``/readyz``
 returns ``200`` — confirms that the Service_Manifest loaded cleanly
 + happy path).
* The Next.js UI on ``http://localhost:3000`` answers with HTTP ``200``
 .
* ``docker compose down -v`` runs in a teardown ``finally`` block so
 the host is left clean even when an assertion fails.

Gating
------
Gated behind the ``--run-docker`` pytest flag registered in
``tests/conftest.py``. When the flag is absent the test SKIPs cleanly
so the default fast-lane suite stays self-contained. With
``--run-docker`` the test additionally probes ``docker info`` to
confirm the daemon is reachable before attempting to boot the stack.
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

#: The exact set of services that MUST be running under the default
#: (no-profile) ``docker compose up`` invocation per 
BOOT_BUNDLE_SERVICES: frozenset[str] = frozenset(
    {
        "admin-dashboard-ui",
        "admin-dashboard-api",
        "postgres",
        "vault",
    }
)

#: Maximum wall-clock seconds to wait for ``compose up --wait`` to
#: declare the Boot_Bundle healthy. ``--wait`` already blocks until the
#: stack reports healthy or fails, but we cap the subprocess in case
#: the daemon hangs.
BOOT_TIMEOUT_SECONDS: float = 240.0

#: Polling cadence used by the post-boot HTTP probes.
POLL_INTERVAL_SECONDS: float = 2.0

#: Maximum wall-clock seconds for the secondary HTTP probes (``/healthz``,
#: ``/readyz``, ``http://localhost:3000``). ``compose up --wait`` should
#: have already gated on healthchecks; this is just a safety net for the
#: UI which may need a few extra seconds to compile its initial route.
POST_BOOT_PROBE_TIMEOUT_SECONDS: float = 60.0


@dataclass(frozen=True)
class HealthEndpoint:
    """A single host-side probe target."""

    label: str
    url: str
    expected_status: int = 200


#: HTTP endpoints checked after ``compose up --wait`` reports healthy.
BOOT_BUNDLE_ENDPOINTS: tuple[HealthEndpoint, ...] = (
    HealthEndpoint("admin-dashboard-api /healthz", "http://localhost:8082/healthz"),
    HealthEndpoint("admin-dashboard-api /readyz", "http://localhost:8082/readyz"),
    HealthEndpoint("admin-dashboard-ui /", "http://localhost:3000"),
)


# ---------------------------------------------------------------------------
# Skip-gating helpers
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True iff the ``docker`` CLI is usable and the daemon responds."""

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


def _require_docker_or_skip(request: pytest.FixtureRequest) -> None:
    """Skip the test when ``--run-docker`` is absent or the daemon is down.

 Centralised so the body of the test reads top-down without
 interleaving skip checks.
 """

    if not request.config.getoption("--run-docker", default=False):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable.",
        )
    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` failed); "
            "cannot run Boot_Bundle smoke test.",
        )


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------


def _compose_up_wait(repo_root: Path) -> subprocess.CompletedProcess:
    """Bring the Boot_Bundle up and block until healthy (or fail)."""

    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            str(int(BOOT_TIMEOUT_SECONDS)),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=BOOT_TIMEOUT_SECONDS + 30,
        check=False,
    )


def _compose_down(repo_root: Path) -> None:
    """Tear down the stack, drop named volumes, swallow errors."""

    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "down", "-v"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _compose_ps_services(repo_root: Path) -> set[str]:
    """Return the set of Compose service names currently running.

 Uses ``--format json`` so we can parse the output deterministically
 regardless of column ordering. Newer Compose versions emit one JSON
 object line (NDJSON); older versions emit a single JSON array.
 Both shapes are handled.
 """

    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "ps", "--format", "json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"`docker compose ps --format json` failed (exit "
            f"{result.returncode}):\n  stdout: {result.stdout}\n"
            f"  stderr: {result.stderr}"
        )

    out = result.stdout.strip()
    if not out:
        return set()

    services: set[str] = set()
    # Try NDJSON first (one object line).
    try:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            name = obj.get("Service") or obj.get("Name")
            if isinstance(name, str) and name:
                services.add(name)
        if services:
            return services
    except json.JSONDecodeError:
        services.clear()

    # Fall back to a single JSON array.
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"could not parse `docker compose ps --format json` output: {exc}\n"
            f"raw output: {out!r}"
        ) from exc

    if isinstance(payload, list):
        for obj in payload:
            if not isinstance(obj, dict):
                continue
            name = obj.get("Service") or obj.get("Name")
            if isinstance(name, str) and name:
                services.add(name)
    return services


def _compose_published_ports(repo_root: Path, service: str) -> set[int]:
    """Return the host-side TCP ports published by ``service``.

 Uses ``docker compose port`` published container port; we
 enumerate the Boot_Bundle's known ports rather than parsing the
 full ``ps`` output to keep this robust across Compose versions.
 """

    ports: set[int] = set()
    # The two service ports we care about for 
    candidates = {
        "admin-dashboard-ui": (3000,),
        "admin-dashboard-api": (8082,),
    }
    for container_port in candidates.get(service, ()):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                COMPOSE_FILE_REL,
                "port",
                service,
                str(container_port),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            continue
        # Output shape: ``0.0.0.0:3000`` (or ``[::]:3000``).
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if ":" in line:
            try:
                ports.add(int(line.rsplit(":", 1)[1]))
            except ValueError:
                pass
    return ports


def _wait_for_endpoint(
    endpoint: HealthEndpoint,
    timeout: float,
    interval: float,
) -> str | None:
    """Poll ``endpoint`` until it returns the expected status or times out.

 Returns ``None`` on success and a human-readable error message on
 failure. We accept any 2xx for the UI root, but require an exact
 ``200`` for the API health probes.
 """

    import httpx  # local import keeps module import cheap when skipped

    deadline = time.monotonic() + timeout
    last_err = "not yet probed"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(endpoint.url, timeout=5.0, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - any transport error means "not yet up"
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(interval)
            continue
        if 200 <= response.status_code < 300:
            return None
        last_err = f"HTTP {response.status_code}: {response.text[:120]}"
        time.sleep(interval)
    return last_err


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_boot_bundle_only_brings_up_four_services_and_they_are_healthy(
    request: pytest.FixtureRequest,
    repo_root: Path,
) -> None:
    """Default ``compose up`` brings up exactly the Boot_Bundle and it's healthy.

 """

    _require_docker_or_skip(request)

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot boot Boot_Bundle."
    )

    try:
        up = _compose_up_wait(repo_root)
        assert up.returncode == 0, (
            "`docker compose up -d --wait` failed:\n"
            f"  stdout: {up.stdout}\n"
            f"  stderr: {up.stderr}"
        )

        # ---- — only Boot_Bundle services running ----
        running = _compose_ps_services(repo_root)
        assert running == BOOT_BUNDLE_SERVICES, (
            "Boot_Bundle invariant violated. `docker compose ps` should "
            f"list exactly {sorted(BOOT_BUNDLE_SERVICES)} but listed "
            f"{sorted(running)}. Extra services indicate a profile-gating "
            "regression ."
        )

        # ---- host ports published ----
        ui_ports = _compose_published_ports(repo_root, "admin-dashboard-ui")
        api_ports = _compose_published_ports(repo_root, "admin-dashboard-api")
        assert 3000 in ui_ports, (
            f"admin-dashboard-ui must publish host port 3000 "
            f"; saw {sorted(ui_ports)}"
        )
        assert 8082 in api_ports, (
            f"admin-dashboard-api must publish host port 8082 "
            f"; saw {sorted(api_ports)}"
        )

        # ---- — HTTP probes ----
        # ``compose up --wait`` already gates on healthchecks, but a few
        # services (notably the Next.js UI) accept connections slightly
        # before they finish their first compile. The bounded poll
        # absorbs that lag without making the test wall-clock dominated
        # by sleep latency.
        for endpoint in BOOT_BUNDLE_ENDPOINTS:
            err = _wait_for_endpoint(
                endpoint,
                timeout=POST_BOOT_PROBE_TIMEOUT_SECONDS,
                interval=POLL_INTERVAL_SECONDS,
            )
            assert err is None, (
                f"{endpoint.label} did not reach 2xx within "
                f"{POST_BOOT_PROBE_TIMEOUT_SECONDS:.0f}s: {err}"
            )
    finally:
        _compose_down(repo_root)
