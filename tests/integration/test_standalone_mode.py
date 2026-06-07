"""Integration smoke test: Standalone Mode build and run for one HTTP service
and one Temporal worker.

Validates the Standalone Mode guarantees:

* Every Component can be built in isolation with ``docker build .`` from
 its own directory, with no parent traversal required.
* Every Component can be run with ``docker run --env-file .env`` using
 only its own ``.env.example``, with no external orchestrator needed.

To keep wall-clock time bounded the test exercises **one** representative
of each Component runtime profile rather than the full manifest:

* HTTP service: ``services/automation-service`` (FastAPI on port 8080).
* Temporal worker: ``workers/agent-runner-worker`` (no exposed port).

These two cover the divergent code paths in the Dockerfiles (HTTP
``EXPOSE`` + ``curl`` healthcheck vs worker no-port + Temporal client
healthcheck) and in ``.env.example`` (HTTP service block vs worker
block). The other Components share the same shape, so a
green run on these two is a strong signal that the entire project
satisfies the Standalone-Mode contract.

Gating
------

The test is gated behind the ``--run-docker`` pytest flag (registered
in ``tests/conftest.py``). When the flag is absent, the test is
**skipped** so the default fast-lane suite stays self-contained and
runs without a Docker daemon. With ``--run-docker`` the test
additionally verifies that the ``docker`` CLI is available before
attempting any build.

Behavior
--------

For the **HTTP service**:

1. Stage ``.env`` by copying ``.env.example`` the
 ``.env.example`` is the only env file the repo ships).
2. ``docker build`` from the Component directory with no parent
 context. Build success alone validates (Standalone
 Mode build) and the invariant (no ``COPY ../...`` escape).
3. ``docker run -d -p 18080:8080 --env-file .env`` with a host port
 chosen high enough to avoid colliding with any Compose-published
 port (8080 is taken by ``automation-service`` in the stack itself).
4. Poll ``GET http://localhost:18080/healthz`` until it returns 200
 within a bounded timeout. The endpoint is the only contract the
 project guarantees in isolation - ``/readyz`` would also return
 200 today because ``Settings.dependencies_reachable`` is a stub
  that returns ``True`` unconditionally; future dependency checks can tighten
 this once the readiness probe wires up real dependency checks.

For the **Temporal worker**:

1. Stage ``.env`` by copying ``.env.example``.
2. ``docker build`` from the worker directory.
3. ``docker run --env-file .env`` with ``TEMPORAL_HOST=invalid:1`` so
 the entry point's ``Client.connect`` call fails fast. We then wait
 for the container to exit and assert the exit code is non-zero
 non-zero on connect failure so a supervisor can
 restart the process). This covers both the build path *and* the
 "the entrypoint at least tries to connect" behavior.

All resources (containers, images, ``.env`` files) are cleaned up in a
``finally`` block so a failed run does not leave dangling state on the
developer's machine.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StandaloneTarget:
    """A single Component to exercise in Standalone Mode."""

    #: Component path relative to the workspace root (Compose-free build
    #: context per the invariant / .
    path: str

    #: Image tag used for the smoke build. The ``:smoke`` suffix keeps
    #: the test image distinct from any locally cached production-style
    #: tag the developer might have built previously.
    image_tag: str

    #: Container name for the smoke run. ``--name`` is set so the
    #: teardown step can ``docker rm -f`` deterministically even if the
    #: container is still alive when an assertion fires.
    container_name: str


# Representative HTTP service: FastAPI ``automation-service`` on :8080.
HTTP_TARGET: StandaloneTarget = StandaloneTarget(
    path="services/automation-service",
    image_tag="automation-service:smoke",
    container_name="automation-service-smoke",
)

# Representative Temporal worker: ``agent-runner-worker`` (no port).
WORKER_TARGET: StandaloneTarget = StandaloneTarget(
    path="workers/agent-runner-worker",
    image_tag="agent-runner-worker:smoke",
    container_name="agent-runner-worker-smoke",
)

#: Host port chosen for the HTTP smoke run. Deliberately distinct from
#: the Compose-published 8080 so the test does not collide with a
#: developer's existing stack on the same machine.
HTTP_HOST_PORT: int = 18080

#: Container-internal port for the HTTP target (matches its EXPOSE /
#: ``Settings.port`` default).
HTTP_CONTAINER_PORT: int = 8080

#: Maximum wall-clock time to wait for the HTTP container's ``/healthz``
#: endpoint to start returning 200. Cold-cache builds skew the first
#: probe by several seconds; 60s leaves ample headroom while still
#: failing fast on a genuinely broken image.
HTTP_HEALTH_TIMEOUT_SECONDS: float = 60.0

#: Maximum wall-clock time to wait for the worker container to **exit**
#: after we start it with an unreachable Temporal host. The temporalio
#: client's default connect timeout is well under 30s, so 60s is a
#: defensive ceiling.
WORKER_EXIT_TIMEOUT_SECONDS: float = 60.0

#: Polling cadence shared by both the HTTP-health and worker-exit
#: wait loops. 1s keeps the docker daemon load negligible without
#: making the test wall-clock dominated by sleep latency.
POLL_INTERVAL_SECONDS: float = 1.0

#: Per-Docker-CLI subprocess timeout. Build steps (image download +
#: pip install) can legitimately take a few minutes on a cold cache.
BUILD_TIMEOUT_SECONDS: float = 600.0

#: Per-``docker run``/``docker stop``/``docker rm`` subprocess timeout.
#: The actual container lifetime is governed by the wait loops above;
#: this only bounds the CLI call itself.
DOCKER_CLI_TIMEOUT_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Skip-gating helpers (kept in sync with test_compose_boot_default_profile.py
# so both integration tests share the same opt-in semantics).
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


def _stage_env_file(component_dir: Path) -> Path | None:
    """Copy ``.env.example``  ``.env`` inside ``component_dir`` if no
 ``.env`` already exists.

 Returns the path of the file this call created (so teardown can
 remove only files it staged itself, leaving any pre-existing
 developer override untouched), or ``None`` when ``.env`` already
 existed.
 """

    env_file = component_dir / ".env"
    env_example = component_dir / ".env.example"

    if env_file.exists():
        return None
    if not env_example.is_file():
        raise FileNotFoundError(
            f"missing .env.example for Standalone Mode target: {env_example}"
        )

    env_file.write_bytes(env_example.read_bytes())
    return env_file


# ---------------------------------------------------------------------------
# Docker CLI helpers
# ---------------------------------------------------------------------------


def _docker_build(component_dir: Path, image_tag: str) -> subprocess.CompletedProcess:
    """``docker build -t <image_tag> .`` inside ``component_dir``.

 The build context is the Component directory itself (no parent
 traversal) - Standalone Mode (the invariant) requires that
 ``docker build .`` works from inside the Component folder without
 any ``..`` escape.
 """

    return subprocess.run(
        ["docker", "build", "-t", image_tag, "."],
        cwd=component_dir,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_run_http(
    component_dir: Path,
    target: StandaloneTarget,
    host_port: int,
    container_port: int,
) -> subprocess.CompletedProcess:
    """Start the HTTP service container in detached mode with port mapping
 and the staged ``.env`` file.

 ``--rm`` ensures the container is removed on stop so we don't have
 to do a separate ``docker rm`` round-trip in the happy path.
 """

    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            target.container_name,
            "-p",
            f"{host_port}:{container_port}",
            "--env-file",
            ".env",
            target.image_tag,
        ],
        cwd=component_dir,
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_run_worker(
    component_dir: Path,
    target: StandaloneTarget,
) -> subprocess.CompletedProcess:
    """Start the worker container in detached mode with the staged
 ``.env`` file plus a ``TEMPORAL_HOST`` override that is guaranteed
 to be unreachable.

 The override drives the entry point's ``Client.connect`` call into
 its failure branch so we can observe the
 "non-zero exit on connect failure" behaviour from the host.
 The ``--rm`` flag is intentionally **not** used here so we can
 inspect the exit code via ``docker inspect`` after the container
 terminates.
 """

    return subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            target.container_name,
            "--env-file",
            ".env",
            "-e",
            "TEMPORAL_HOST=invalid-host-for-standalone-smoke:1",
            target.image_tag,
        ],
        cwd=component_dir,
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_stop(container_name: str) -> None:
    """Best-effort ``docker stop`` of the named container."""

    subprocess.run(
        ["docker", "stop", container_name],
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_rm(container_name: str) -> None:
    """Best-effort ``docker rm -f`` of the named container."""

    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_rmi(image_tag: str) -> None:
    """Best-effort ``docker rmi -f`` of the smoke-test image."""

    subprocess.run(
        ["docker", "rmi", "-f", image_tag],
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )


def _docker_logs(container_name: str) -> str:
    """Return the captured stdout+stderr of ``container_name``.

 Used for diagnostics when an assertion fails; never raises.
 """

    try:
        result = subprocess.run(
            ["docker", "logs", container_name],
            capture_output=True,
            text=True,
            timeout=DOCKER_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"<failed to fetch logs: {exc}>"
    return f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _docker_inspect_state(container_name: str) -> dict[str, str]:
    """Parse ``docker inspect`` and return a small dict with the
 container's running flag and exit code.

 Keys: ``status`` ("running"/"exited"/"created"/...), ``exit_code``
 (string form of the integer), ``running`` ("true"/"false"). All
 values are strings so the helper has zero JSON-parsing dependency.
 """

    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Running}}",
            container_name,
        ],
        capture_output=True,
        text=True,
        timeout=DOCKER_CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        return {"status": "unknown", "exit_code": "", "running": "false"}
    parts = result.stdout.strip().split("|")
    if len(parts) != 3:
        return {"status": "unknown", "exit_code": "", "running": "false"}
    return {"status": parts[0], "exit_code": parts[1], "running": parts[2]}


# ---------------------------------------------------------------------------
# Wait loops
# ---------------------------------------------------------------------------


def _wait_for_http_healthy(url: str, timeout: float, interval: float) -> str | None:
    """Poll ``url`` until it returns 2xx or the timeout expires.

 Returns ``None`` on success, or the last error string (for use in
 the assertion message) on timeout.
 """

    import httpx  # local import keeps module import cheap when skipped

    deadline = time.monotonic() + timeout
    last_error: str = "not yet probed"

    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - any transport error means "not yet up"
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if 200 <= response.status_code < 300:
                return None
            last_error = f"HTTP {response.status_code}: {response.text[:120]}"
        time.sleep(interval)

    return last_error


def _wait_for_container_exit(
    container_name: str, timeout: float, interval: float
) -> dict[str, str] | None:
    """Poll ``docker inspect`` until ``State.Running`` is ``false`` or the
 timeout expires.

 Returns the final inspect dict on success, or ``None`` if the
 container was still running when the timeout fired.
 """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _docker_inspect_state(container_name)
        if state["running"].lower() == "false":
            return state
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_standalone_mode_builds_and_runs_for_http_service_and_worker(
    request: pytest.FixtureRequest, repo_root: Path
) -> None:
    """One HTTP service and one Temporal worker can be built and run in
 Standalone Mode using only their own ``.env.example``.

 Validates and 15.3.

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
            "cannot run Standalone Mode smoke test."
        )

    http_dir = repo_root / HTTP_TARGET.path
    worker_dir = repo_root / WORKER_TARGET.path
    assert http_dir.is_dir(), f"HTTP target missing: {http_dir}"
    assert worker_dir.is_dir(), f"worker target missing: {worker_dir}"

    staged_envs: list[Path] = []

    try:
        # --- HTTP service ---------------------------------------------------
        http_env = _stage_env_file(http_dir)
        if http_env is not None:
            staged_envs.append(http_env)

        http_build = _docker_build(http_dir, HTTP_TARGET.image_tag)
        assert http_build.returncode == 0, (
            f"`docker build` failed for {HTTP_TARGET.path} "
            "(Standalone Mode / the invariant requires a parent-free build "
            "context):\n"
            f"  stdout: {http_build.stdout[-2000:]}\n"
            f"  stderr: {http_build.stderr[-2000:]}"
        )

        http_run = _docker_run_http(
            http_dir,
            HTTP_TARGET,
            host_port=HTTP_HOST_PORT,
            container_port=HTTP_CONTAINER_PORT,
        )
        assert http_run.returncode == 0, (
            f"`docker run` failed for {HTTP_TARGET.path}:\n"
            f"  stdout: {http_run.stdout}\n"
            f"  stderr: {http_run.stderr}"
        )

        health_url = f"http://localhost:{HTTP_HOST_PORT}/healthz"
        last_error = _wait_for_http_healthy(
            health_url,
            timeout=HTTP_HEALTH_TIMEOUT_SECONDS,
            interval=POLL_INTERVAL_SECONDS,
        )
        if last_error is not None:
            logs = _docker_logs(HTTP_TARGET.container_name)
            pytest.fail(
                f"{HTTP_TARGET.path} did not respond 2xx on {health_url} "
                f"within {HTTP_HEALTH_TIMEOUT_SECONDS:.0f}s; "
                f"last error: {last_error}\n"
                f"container logs:\n{logs}"
            )

        # The HTTP container is still running here; the cleanup block
        # below will stop it. We *don't* assert on /readyz: the
        # ``Settings.dependencies_reachable`` stub returns ``True``
        # unconditionally in this project (see
        # services/automation-service/src/config.py), so /readyz would
        # return 200 today rather than the dependency-failure status.
        # Future dependency probes can extend
        # this assertion.

        _docker_stop(HTTP_TARGET.container_name)

        # --- Temporal worker ------------------------------------------------
        worker_env = _stage_env_file(worker_dir)
        if worker_env is not None:
            staged_envs.append(worker_env)

        worker_build = _docker_build(worker_dir, WORKER_TARGET.image_tag)
        assert worker_build.returncode == 0, (
            f"`docker build` failed for {WORKER_TARGET.path} "
            "(Standalone Mode requires a parent-free build "
            "context):\n"
            f"  stdout: {worker_build.stdout[-2000:]}\n"
            f"  stderr: {worker_build.stderr[-2000:]}"
        )

        worker_run = _docker_run_worker(worker_dir, WORKER_TARGET)
        assert worker_run.returncode == 0, (
            f"`docker run` failed for {WORKER_TARGET.path}:\n"
            f"  stdout: {worker_run.stdout}\n"
            f"  stderr: {worker_run.stderr}"
        )

        final_state = _wait_for_container_exit(
            WORKER_TARGET.container_name,
            timeout=WORKER_EXIT_TIMEOUT_SECONDS,
            interval=POLL_INTERVAL_SECONDS,
        )
        if final_state is None:
            logs = _docker_logs(WORKER_TARGET.container_name)
            pytest.fail(
                f"{WORKER_TARGET.path} did not exit within "
                f"{WORKER_EXIT_TIMEOUT_SECONDS:.0f}s after being started "
                "with an unreachable TEMPORAL_HOST; the entry point should "
                "fail fast on connect error per \n"
                f"container logs:\n{logs}"
            )

        try:
            exit_code = int(final_state["exit_code"])
        except (KeyError, TypeError, ValueError):
            exit_code = -1

        assert exit_code != 0, (
            f"{WORKER_TARGET.path} exited with code {exit_code}; "
            "mandates a non-zero exit code when the worker "
            "cannot reach Temporal so a supervisor can restart it.\n"
            f"docker inspect state: {final_state}\n"
            f"container logs:\n{_docker_logs(WORKER_TARGET.container_name)}"
        )

    finally:
        # Always tear down both containers, both images, and any .env
        # files this test staged. ``-f`` / best-effort calls keep the
        # cleanup quiet when a step never created the resource in the
        # first place.
        _docker_rm(HTTP_TARGET.container_name)
        _docker_rm(WORKER_TARGET.container_name)
        _docker_rmi(HTTP_TARGET.image_tag)
        _docker_rmi(WORKER_TARGET.image_tag)
        for env_file in staged_envs:
            try:
                env_file.unlink(missing_ok=True)
            except OSError:
                # Cleanup is best-effort; a leftover .env is matched by
                # the workspace .gitignore (``*.env`` per .
                pass
