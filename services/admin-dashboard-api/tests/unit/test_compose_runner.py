"""Unit tests for ``src.lifecycle.compose_runner``.
These tests exercise the public surface of :class:`ComposeRunner` as a
black box, with :func:`asyncio.create_subprocess_exec` patched out.
The patch lets us:
* Capture and assert the exact ``argv`` shape passed to the OS
  - argv list, ``shell=False``, no shell metacharacter
  expansion).
* Assert the subprocess ``env`` dict only contains the allow-listed
  host keys plus the operator-supplied overrides - never the host's
  arbitrary secrets such as ``VAULT_TOKEN`` or ``OPENAI_API_KEY``.
* Assert no temporary ``.env`` files are created under the workspace
  root during ``up``/``stop``/``restart``/``exec_test``.
The tests are deliberately mock-heavy because the production target is
the *argv shape* and *environment scrubbing*, not the behaviour of
``docker compose`` itself; we have no docker daemon in the unit-test
lane."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Hook ``src.*`` imports under direct ``pytest tests/unit`` invocation,
# matching the convention already used by ``test_env_parser.py``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.compose_runner import (  # noqa: E402
    ComposeFailureError,
    ComposeResult,
    ComposeRunner,
    TestResult,
)


# ---------------------------------------------------------------------------
# Fake subprocess plumbing
# ---------------------------------------------------------------------------


class _FakeStreamReader:
    """Minimal :class:`asyncio.StreamReader` substitute for ``logs --follow``."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProcess:
    """Stands in for the object returned by ``create_subprocess_exec``."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stream_lines: list[bytes] | None = None,
    ) -> None:
        self.returncode: int | None = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = (
            _FakeStreamReader(stream_lines) if stream_lines is not None else None
        )
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:  # pragma: no cover - safety net
        self.killed = True

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class _SpawnRecorder:
    """Records every ``create_subprocess_exec`` call and supplies fake processes."""

    def __init__(self, processes: list[_FakeProcess]) -> None:
        self._queue = list(processes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"argv": tuple(argv), "kwargs": kwargs})
        if not self._queue:
            # Default to a successful zero-exit process.
            return _FakeProcess()
        return self._queue.pop(0)


def _make_recorder(*processes: _FakeProcess) -> _SpawnRecorder:
    return _SpawnRecorder(list(processes))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


COMPOSE_FILE = Path("/repo/infra/docker-compose.yml")
WORKSPACE = Path("/repo")


def _runner() -> ComposeRunner:
    return ComposeRunner(compose_file=COMPOSE_FILE, workspace_root=WORKSPACE)


# ---------------------------------------------------------------------------
# argv shape - happy paths
# ---------------------------------------------------------------------------


def test_up_argv_shape_uses_profile_and_service_name() -> None:
    """``up`` builds the canonical Compose argv for a profiled service."""

    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"ok"))
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(
            _runner().up(
                profile="automation-service",
                service_name="automation-service",
                env_overrides={"PORT": "8080"},
            )
        )

    assert isinstance(result, ComposeResult)
    assert result.exit_code == 0
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "automation-service",
        "up",
        "-d",
        "--build",
        "automation-service",
    )


def test_up_does_not_pass_env_overrides_as_cli_flags() -> None:
    """env_overrides go through the env dict only.
    The recorded argv must NEVER contain ``--env`` flags constructed
    from the override map, and override values must never appear on
    the command line. They live solely in the subprocess's ``env``
    mapping."""

    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            _runner().up(
                profile="automation-service",
                service_name="automation-service",
                env_overrides={
                    "VAULT_TOKEN": "super-secret-do-not-leak",
                    "PORT": "8080",
                },
            )
        )

    argv = recorder.calls[0]["argv"]
    assert "--env-file" not in argv
    assert "--env" not in argv
    # And the secret value must not have leaked into the argv at all.
    assert all("super-secret-do-not-leak" not in token for token in argv)


def test_up_passes_workspace_env_file_when_present(tmp_path: Path) -> None:
    """Dashboard-started profiles use the same root ``.env`` as boot scripts."""

    workspace = tmp_path
    compose_file = workspace / "infra" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = workspace / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-test-value\n", encoding="utf-8")
    runner = ComposeRunner(compose_file=compose_file, workspace_root=workspace)

    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"ok"))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            runner.up(
                profile="streamlit-ui",
                service_name="streamlit-ui",
                env_overrides=None,
            )
        )

    argv = recorder.calls[0]["argv"]
    assert argv[:6] == (
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(compose_file),
    )
    assert all("sk-test-value" not in token for token in argv)


def test_up_layers_workspace_env_local_when_present(tmp_path: Path) -> None:
    """Machine-local secrets can override tracked defaults without argv leaks."""

    workspace = tmp_path
    compose_file = workspace / "infra" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = workspace / ".env"
    env_file.write_text("VAULT_BACKEND=hashicorp\n", encoding="utf-8")
    local_env_file = workspace / ".env.local"
    local_env_file.write_text("MAIL_SESSION_VAULT_LOCAL_KEY=secret\n", encoding="utf-8")
    runner = ComposeRunner(compose_file=compose_file, workspace_root=workspace)

    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"ok"))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            runner.up(
                profile="assistant-service",
                service_name="assistant-service",
                env_overrides=None,
            )
        )

    argv = recorder.calls[0]["argv"]
    assert argv[:8] == (
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--env-file",
        str(local_env_file),
        "-f",
        str(compose_file),
    )
    assert all("secret" not in token for token in argv)


def test_stop_argv_shape() -> None:
    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(_runner().stop(service_name="redis"))

    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "stop",
        "redis",
    )
    # No second invocation when remove_volumes is False.
    assert len(recorder.calls) == 1


def test_stop_with_remove_volumes_runs_rm_fv() -> None:
    """``remove_volumes=True`` follows ``stop`` with ``rm -fv``.
    Named volumes - ``pg_data``, ``minio_data``,
    ``agent_workspace``) are owned by the top-level ``volumes:`` block
    in ``docker-compose.yml``, so ``rm -fv <service>`` only purges the
    service's *anonymous* volumes. This test asserts
    the argv contract; the volume-ownership invariant lives in the
    Compose file itself."""

    recorder = _make_recorder(
        _FakeProcess(returncode=0),
        _FakeProcess(returncode=0),
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            _runner().stop(service_name="redis", remove_volumes=True)
        )

    assert len(recorder.calls) == 2
    assert recorder.calls[0]["argv"][-2:] == ("stop", "redis")
    assert recorder.calls[1]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "rm",
        "-fv",
        "redis",
    )


def test_restart_runs_stop_then_up() -> None:
    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # stop
        _FakeProcess(returncode=0),  # up
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            _runner().restart(
                profile="redis",
                service_name="redis",
                env_overrides={"REDIS_PASSWORD": "pw"},
            )
        )

    assert len(recorder.calls) == 2
    assert "stop" in recorder.calls[0]["argv"]
    # Second call is up with --profile and -d.
    second_argv = recorder.calls[1]["argv"]
    assert "up" in second_argv
    assert "--profile" in second_argv
    assert "-d" in second_argv


def test_logs_argv_without_follow() -> None:
    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"line1\nline2\n"))
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(
            _runner().logs(service_name="redis", tail=200, follow=False)
        )

    assert isinstance(result, ComposeResult)
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "logs",
        "--tail",
        "200",
        "--no-color",
        "redis",
    )


def test_logs_argv_with_follow_appends_flag_and_returns_iterator() -> None:
    fake = _FakeProcess(
        returncode=0,
        stream_lines=[b"first\n", b"second\n"],
    )
    recorder = _make_recorder(fake)

    async def _drive() -> list[str]:
        gen = await _runner().logs(service_name="redis", tail=10, follow=True)
        out: list[str] = []
        # ``gen`` is the async iterator returned by _stream_logs.
        async for line in gen:  # type: ignore[union-attr]
            out.append(line)
        return out

    with patch("asyncio.create_subprocess_exec", recorder):
        lines = asyncio.run(_drive())

    assert lines == ["first", "second"]
    argv = recorder.calls[0]["argv"]
    assert "--follow" in argv
    assert argv[-1] == "redis"


def test_exec_test_argv_uses_compose_exec_dash_capital_t() -> None:
    """``exec_test`` runs ``docker compose exec -T <svc> <argv...>``.
    The ``-T`` flag disables TTY allocation so the call is safe inside
    a non-interactive HTTP handler ."""

    recorder = _make_recorder(
        _FakeProcess(returncode=0, stdout=b"== 3 passed in 0.42s ==")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(
            _runner().exec_test(
                service_name="automation-service",
                argv=("pytest", "tests/integration/", "-v"),
                stream=False,
            )
        )

    assert isinstance(result, TestResult)
    assert result.exit_code == 0
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "automation-service",
        "pytest",
        "tests/integration/",
        "-v",
    )


# ---------------------------------------------------------------------------
# Failure mode - non-zero exit raises ComposeFailureError
# ---------------------------------------------------------------------------


def test_up_non_zero_exit_raises_compose_failure_error() -> None:
    """    The runner surfaces this as :class:`ComposeFailureError` so the
    router layer can render the canonical error envelope without
    duplicating exit-code checks."""

    recorder = _make_recorder(
        _FakeProcess(returncode=1, stdout=b"", stderr=b"image not found")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeFailureError) as excinfo:
            asyncio.run(
                _runner().up(
                    profile="redis",
                    service_name="redis",
                )
            )

    err = excinfo.value
    assert err.result.exit_code == 1
    assert err.result.stderr == "image not found"
    # The argv is preserved on the exception's ComposeResult so the
    # 502 response can include it for operator diagnostics.
    assert err.result.argv[:2] == ("docker", "compose")


def test_stop_non_zero_exit_raises_compose_failure_error() -> None:
    recorder = _make_recorder(
        _FakeProcess(returncode=2, stdout=b"", stderr=b"no such service")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeFailureError):
            asyncio.run(_runner().stop(service_name="redis"))


def test_stop_remove_volumes_failure_in_rm_step_raises() -> None:
    """The ``rm -fv`` follow-up is also guarded by the failure check."""

    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # stop succeeds
        _FakeProcess(returncode=3, stderr=b"rm failed"),  # rm fails
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeFailureError) as excinfo:
            asyncio.run(
                _runner().stop(service_name="redis", remove_volumes=True)
            )

    assert excinfo.value.result.exit_code == 3


# ---------------------------------------------------------------------------
# Environment scrubbing - host secrets must NOT leak
# ---------------------------------------------------------------------------


def test_environ_is_scrubbed_to_allowlist_only() -> None:
    """Only ``PATH``, ``HOME``, ``DOCKER_HOST`` (when set on the host) are forwarded.

    The arbitrary host secrets (``VAULT_TOKEN``, ``OPENAI_API_KEY``,
    ``AWS_SECRET_ACCESS_KEY``, the user's PowerShell profile vars, ...)
    must never appear in the env dict that lands on the spawned
    subprocess. Operator-supplied ``env_overrides`` *are* applied on
    top - that is the only sanctioned path for non-allow-listed keys.
    """

    recorder = _make_recorder(_FakeProcess(returncode=0))
    fake_environ = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/home/admin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        # These must be filtered out:
        "VAULT_TOKEN": "host-side-vault-token",
        "OPENAI_API_KEY": "sk-host-leak",
        "AWS_SECRET_ACCESS_KEY": "abc123",
        "PSModulePath": r"C:\Modules",  # Windows-side noise
    }
    with patch.dict(os.environ, fake_environ, clear=True):
        with patch("asyncio.create_subprocess_exec", recorder):
            asyncio.run(
                _runner().up(
                    profile="automation-service",
                    service_name="automation-service",
                    env_overrides={"PORT": "8080"},
                )
            )

    spawned_env = recorder.calls[0]["kwargs"]["env"]
    assert spawned_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert spawned_env["HOME"] == "/home/admin"
    assert spawned_env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert spawned_env["PORT"] == "8080"  # operator override applied

    # Host-side secrets must be absent.
    assert "VAULT_TOKEN" not in spawned_env
    assert "OPENAI_API_KEY" not in spawned_env
    assert "AWS_SECRET_ACCESS_KEY" not in spawned_env
    assert "PSModulePath" not in spawned_env


def test_env_overrides_are_passed_via_subprocess_env_dict() -> None:
    """``env_overrides`` reach the child *only* through the env mapping.
    This enforces that Vault-sourced secrets never touch the disk and never appear
    on the command line."""

    recorder = _make_recorder(_FakeProcess(returncode=0))
    overrides = {
        "DB_PASSWORD": "v3ry-secret",
        "OAUTH_CLIENT_SECRET": "oauth-secret",
        "PORT": "9000",
    }
    with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
        with patch("asyncio.create_subprocess_exec", recorder):
            asyncio.run(
                _runner().up(
                    profile="assistant-service",
                    service_name="assistant-service",
                    env_overrides=overrides,
                )
            )

    spawned_env = recorder.calls[0]["kwargs"]["env"]
    for key, value in overrides.items():
        assert spawned_env[key] == value

    # And no override value bleeds into the argv.
    argv = recorder.calls[0]["argv"]
    for value in overrides.values():
        assert all(value not in token for token in argv)


def test_overrides_can_shadow_allowlisted_host_keys() -> None:
    """An override always wins over a host-side allow-listed value.

    This is the deliberate ordering documented in
    ``ComposeRunner._scrubbed_environ``: the scrub seeds with host
    keys, *then* overrides apply on top.
    """

    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch.dict(
        os.environ,
        {"PATH": "/host/path", "HOME": "/home/host"},
        clear=True,
    ):
        with patch("asyncio.create_subprocess_exec", recorder):
            asyncio.run(
                _runner().up(
                    profile="redis",
                    service_name="redis",
                    env_overrides={"PATH": "/override/path"},
                )
            )

    spawned_env = recorder.calls[0]["kwargs"]["env"]
    assert spawned_env["PATH"] == "/override/path"
    assert spawned_env["HOME"] == "/home/host"


# ---------------------------------------------------------------------------
# surface - no temporary.env files written under workspace
# ---------------------------------------------------------------------------


def test_no_temp_env_files_written_under_workspace_root(tmp_path: Path) -> None:
    """No file is created under ``workspace_root`` during ``up`` / ``stop``.
    This complements the env-scrubbing test by walking the workspace
    tree before and after the subprocess boundary and asserting that
    no new files (especially nothing matching ``.env*``) appeared.
    Combined with the env-dict assertion above, this is the unit-test
    surface of."""

    # Materialise a synthetic workspace so we can walk it.
    (tmp_path / "infra").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "services").mkdir()
    compose_file = tmp_path / "infra" / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    runner = ComposeRunner(compose_file=compose_file, workspace_root=tmp_path)

    before = {p for p in tmp_path.rglob("*") if p.is_file()}

    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # up
        _FakeProcess(returncode=0),  # stop
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            runner.up(
                profile="automation-service",
                service_name="automation-service",
                env_overrides={
                    "VAULT_TOKEN": "super-secret",
                    "DB_PASSWORD": "another-secret",
                },
            )
        )
        asyncio.run(runner.stop(service_name="automation-service"))

    after = {p for p in tmp_path.rglob("*") if p.is_file()}
    new_files = after - before

    # No new files - and especially nothing matching ``.env`` patterns.
    assert new_files == set(), f"unexpected files written: {sorted(new_files)}"
    for path in after:
        name = path.name.lower()
        assert not name.startswith(".env"), (
            f"workspace contains a stray dotenv file: {path}"
        )


# ---------------------------------------------------------------------------
# Subprocess invocation primitives - shell=False is implicit but checkable
# ---------------------------------------------------------------------------


def test_create_subprocess_exec_called_with_pipes_and_no_shell_kwarg() -> None:
    """We rely on ``create_subprocess_exec`` (argv) - never ``..._shell``.
    This is the structural guarantee that no shell metacharacter
    expansion can occur : the API used here takes an
    argv list and forwards it to ``execve`` directly, without an
    intermediate ``/bin/sh -c`` invocation.
    ``create_subprocess_exec`` does not accept a ``shell`` kwarg at
    all, so the assertion is "we used this function, and no caller
    passed a stray ``shell=True``-style kwarg"."""

    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(
            _runner().up(profile="redis", service_name="redis")
        )

    call = recorder.calls[0]
    assert "shell" not in call["kwargs"]
    assert call["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert call["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    assert isinstance(call["kwargs"]["env"], dict)
