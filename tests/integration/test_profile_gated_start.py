"""Integration smoke test 9.2 — Profile-gated start senaryosu.


Scenario
--------
1. ``docker compose -f infra/docker-compose.yml up -d --wait`` brings up
 the Boot_Bundle x).
2. ``POST /admin/services/redis/start`` against the running
 ``admin-dashboard-api`` returns ``202 Accepted`` with
 ``state="starting"`` .
3. Polling ``GET /admin/services/redis`` flips to ``state="running"``
 within 30 seconds manifest profile gates a
 started container — and health-driven state
 transition).
4. ``docker compose ps redis`` confirms a Redis container is actually
 running (cross-checks the orchestrator's view against Compose).
5. ``POST /admin/services/redis/stop`` returns ``200`` and a second
 call returns ``200 + noop=true`` .
6. Teardown stops every Managed_Service (best-effort) and then
 ``docker compose down -v`` to clean the host.

Authentication
--------------
The admin-dashboard-api ships with ``AUTH_MODE=dev`` by default in
the project ``.env.example`` (see ``services/admin-dashboard-api/
.env.example``); the test sends ``Authorization: Bearer dev-test``
which is accepted by the dev-mode validator (canned admin claims —
. When the deployed image runs in production mode
the test SKIPs cleanly with a clear reason rather than failing.

Gating
------
``--run-docker`` flag (registered in ``tests/conftest.py``). Skipped
otherwise. Additionally requires ``docker info`` to succeed.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------

COMPOSE_FILE_REL: str = "infra/docker-compose.yml"

#: Boot_Bundle ``compose up --wait`` timeout in seconds.
BOOT_TIMEOUT_SECONDS: float = 240.0

#: Maximum wall-clock time to wait for ``redis`` to flip to
#: ``state="running"`` after the start request design
#: §"Lifecycle State Machine").
START_TO_RUNNING_TIMEOUT_SECONDS: float = 60.0

#: Polling cadence for ``GET /admin/services/redis``.
STATE_POLL_INTERVAL_SECONDS: float = 1.0

#: Admin API base URL (admin-dashboard-api publishes 8082 in the base
#: Compose file — see .
ADMIN_API_BASE: str = "http://localhost:8082"

#: Bearer token sent on every admin request. The dev-mode OIDCValidator
#: accepts any non-empty string and returns canned admin claims
#: ; production mode rejects it and the test SKIPs.
DEV_BEARER_TOKEN: str = "smoke-test-bearer"

#: Service we drive through start/stop. ``redis`` is a stable infra
#: choice that has no upstream network requirement and a fast
#: ``compose up`` time (≤10s on warm hosts).
TARGET_SERVICE: str = "redis"

#: Managed_Services we attempt to stop during teardown so the next
#: test run starts from a clean slate. Best-effort — ignored failures.
MANAGED_SERVICES_TO_TEARDOWN: tuple[str, ...] = (
    "redis",
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
    if not request.config.getoption("--run-docker", default=False):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker to enable.",
        )
    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` failed); "
            "cannot run profile-gated start smoke test.",
        )


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------


def _compose_up_wait(repo_root: Path) -> subprocess.CompletedProcess:
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
    subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "down", "-v"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _compose_service_running(repo_root: Path, service: str) -> bool:
    """Return True iff ``docker compose ps -q <service>`` lists a container."""

    result = subprocess.run(
        ["docker", "compose", "-f", COMPOSE_FILE_REL, "ps", "-q", service],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    return bool(result.stdout.strip())


@dataclass
class _ServiceClient:
    """Tiny wrapper around the admin-dashboard-api ``/admin/services`` API.

 Kept inline so the test file is self-contained — no fixtures or
 extra imports leak into the rest of the suite.
 """

    base_url: str
    bearer: str
    timeout: float = 30.0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer}",
            "Accept": "application/json",
        }

    def get_detail(self, name: str) -> tuple[int, dict[str, Any] | None]:
        import httpx

        try:
            response = httpx.get(
                f"{self.base_url}/admin/services/{name}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise AssertionError(f"GET /admin/services/{name} failed: {exc}") from exc
        return response.status_code, _safe_json(response)

    def post_start(
        self, name: str, env_overrides: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any] | None]:
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/admin/services/{name}/start",
                headers=self._headers(),
                json={"env_overrides": env_overrides or {}},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise AssertionError(
                f"POST /admin/services/{name}/start failed: {exc}"
            ) from exc
        return response.status_code, _safe_json(response)

    def post_stop(self, name: str) -> tuple[int, dict[str, Any] | None]:
        import httpx

        try:
            response = httpx.post(
                f"{self.base_url}/admin/services/{name}/stop",
                headers=self._headers(),
                json={"remove_volumes": False},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise AssertionError(
                f"POST /admin/services/{name}/stop failed: {exc}"
            ) from exc
        return response.status_code, _safe_json(response)


def _safe_json(response: Any) -> dict[str, Any] | None:
    """Return parsed JSON or ``None`` when the response has no body."""

    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _wait_for_admin_api(timeout_seconds: float = 60.0) -> None:
    """Poll ``/healthz`` on the admin-dashboard-api until 200 or timeout."""

    import httpx

    deadline = time.monotonic() + timeout_seconds
    last_err = "not yet probed"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{ADMIN_API_BASE}/healthz", timeout=5.0)
            if response.status_code == 200:
                return
            last_err = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)
    raise AssertionError(
        f"admin-dashboard-api /healthz did not become 200 within "
        f"{timeout_seconds:.0f}s: {last_err}"
    )


def _wait_for_state(
    client: _ServiceClient,
    name: str,
    target_state: str,
    timeout: float,
    interval: float,
) -> str:
    """Poll ``GET /admin/services/{name}`` until ``state == target_state``.

 Returns the last observed state; raises ``AssertionError`` on
 timeout or on an HTTP error response.
 """

    deadline = time.monotonic() + timeout
    last_state: str = "<unobserved>"
    while time.monotonic() < deadline:
        status_code, body = client.get_detail(name)
        assert status_code == 200, (
            f"GET /admin/services/{name} returned {status_code}; body={body!r}"
        )
        assert body is not None, "expected JSON body from GET /admin/services"
        last_state = str(body.get("state", "<missing>"))
        if last_state == target_state:
            return last_state
        time.sleep(interval)
    raise AssertionError(
        f"service {name!r} did not reach state {target_state!r} within "
        f"{timeout:.0f}s; last observed state: {last_state!r}"
    )


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_profile_gated_start_drives_redis_through_start_stop_cycle(
    request: pytest.FixtureRequest,
    repo_root: Path,
) -> None:
    """End-to-end: Boot_Bundle → start redis → poll running → stop ×2.

 """

    _require_docker_or_skip(request)

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot run profile-gated start."
    )

    client = _ServiceClient(base_url=ADMIN_API_BASE, bearer=DEV_BEARER_TOKEN)

    try:
        # ---- Boot the Boot_Bundle ----
        up = _compose_up_wait(repo_root)
        assert up.returncode == 0, (
            "`docker compose up -d --wait` failed:\n"
            f"  stdout: {up.stdout}\n"
            f"  stderr: {up.stderr}"
        )

        # The lifespan startup completes after the healthcheck flips
        # green; ``--wait`` already gates on that, but we belt-and-
        # braces with a /healthz probe before driving the API.
        _wait_for_admin_api()

        # ---- POST /admin/services/redis/start ----
        status_code, body = client.post_start(TARGET_SERVICE)
        if status_code == 401:
            pytest.skip(
                "admin-dashboard-api rejected the dev bearer token (401); "
                "AUTH_MODE is likely set to 'production' on this host. "
                "This smoke test only runs against the dev-mode default "
                "(see .",
            )
        assert status_code == 202, (
            f"expected 202 Accepted from /start; got {status_code} "
            f"with body {body!r}"
        )
        assert body is not None, "expected JSON body from /start"
        assert body.get("state") == "starting", (
            f"expected state='starting' on /start response, got {body!r}"
        )
        assert "correlation_id" in body, (
            f"expected correlation_id in /start response, got {body!r}"
        )

        # ---- Wait until state flips to running ----
        final_state = _wait_for_state(
            client,
            TARGET_SERVICE,
            target_state="running",
            timeout=START_TO_RUNNING_TIMEOUT_SECONDS,
            interval=STATE_POLL_INTERVAL_SECONDS,
        )
        assert final_state == "running"

        # ---- Cross-check: docker compose ps lists redis ----
        assert _compose_service_running(repo_root, TARGET_SERVICE), (
            "admin-dashboard-api reports redis=running but `docker compose "
            "ps -q redis` returned no container ID; orchestrator state "
            "diverged from Compose ground truth."
        )

        # ---- POST /admin/services/redis/stop (first call) ----
        status_code, body = client.post_stop(TARGET_SERVICE)
        assert status_code == 200, (
            f"expected 200 from first /stop; got {status_code} body={body!r}"
        )
        assert body is not None
        assert body.get("state") == "stopped", (
            f"expected state='stopped' after /stop; got {body!r}"
        )
        # First call MUST NOT be a no-op — the service was running.
        assert body.get("noop") is False, (
            f"first /stop call should not be a no-op; got body={body!r}"
        )

        # ---- POST /admin/services/redis/stop (second call) ----
        status_code, body = client.post_stop(TARGET_SERVICE)
        assert status_code == 200, (
            f"expected 200 from second /stop (idempotent); got {status_code} "
            f"body={body!r}"
        )
        assert body is not None
        assert body.get("state") == "stopped"
        assert body.get("noop") is True, (
            "the invariant: second /stop on an already-stopped service "
            f"MUST report noop=True; got body={body!r}"
        )
    finally:
        # ---- Teardown: stop any leftover managed services, then `down -v` ----
        for service in MANAGED_SERVICES_TO_TEARDOWN:
            try:
                client.post_stop(service)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        _compose_down(repo_root)
