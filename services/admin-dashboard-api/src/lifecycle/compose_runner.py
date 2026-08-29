"""Subprocess wrapper around the ``docker compose`` CLI.

This module is the **only** code path inside the admin-dashboard
control plane that shells out to ``docker compose``. Every public
method assembles an explicit ``argv`` list and hands it to
:func:`asyncio.create_subprocess_exec` with ``shell=False`` so the
operating system is responsible for argument tokenisation - there is
no intermediate shell that could expand metacharacters from
manifest-supplied or operator-supplied strings.

Environment overrides are injected via the spawned subprocess's ``env`` dict
and are never written to a temporary ``.env`` file. Non-zero Compose exits are
surfaced as :class:`ComposeFailureError` so the router can render a consistent
upstream error envelope.

Public surface
--------------
``ComposeResult``       Result of a non-streaming compose invocation.
``TestResult``          Result of a service-level test invocation.
``ComposeFailureError`` Raised when ``docker compose`` exits non-zero.
``ComposeRunner``       The subprocess wrapper itself.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Mapping, Sequence

#: Host environment variables that the spawned ``docker compose``
#: process is allowed to inherit. Anything else (including
#: ``VAULT_TOKEN``, ``OPENAI_API_KEY``, the operator's shell history,
#: ...) must stay on the API host. The intent is *not* to fully
#: sandbox the child - Docker still has access to its own credential
#: helpers - but to make sure that no host secret leaks into the
#: managed service's environment by way of subprocess inheritance.
#:
#: * ``PATH`` is required so Linux/macOS/Windows can locate the
#:   ``docker`` binary (and the ``docker-compose`` plugin shipped with
#:   it).
#: * ``HOME`` lets the Docker CLI find ``~/.docker/config.json`` for
#:   credential helpers.
#: * ``DOCKER_HOST`` is honoured by the Docker CLI when set; it is
#:   harmless when absent.
_ALLOWED_HOST_ENV_KEYS: frozenset[str] = frozenset({"PATH", "HOME", "DOCKER_HOST"})


@dataclass(frozen=True)
class ComposeResult:
    """Outcome of a single ``docker compose`` invocation.

    Attributes
    ----------
    exit_code:
        The child process's return code. ``0`` on success.
    stdout:
        Captured stdout, decoded as UTF-8 with ``errors="replace"`` so
        a stray non-UTF-8 byte from a Compose plugin does not raise.
    stderr:
        Captured stderr (same decoding).
    argv:
        The exact ``argv`` list that was executed. Stored verbatim so
        unit tests can assert the argv shape and so audit/error log
        rendering can include the resolved command line.
    """

    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class TestResult:
    """Outcome of an ``exec_test`` invocation.

    Distinct from :class:`ComposeResult` because the upstream
    ``LifecycleService.run_tests`` flow parses the captured ``stdout``
    into a structured pytest summary (see prompt git wiring) and the router
    surfaces it under a different schema.
    """

    # Hint to pytest: this is a result *type*, not a test class. Without
    # it pytest's collection layer warns about the ``Test`` name prefix.
    __test__ = False

    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]


class ComposeFailureError(Exception):
    """Raised when a ``docker compose`` invocation exits non-zero.

    The exception captures the assembled :class:`ComposeResult` so
    callers (the lifecycle service / router) can surface the failing
    argv plus stderr in their 502 response and audit ``details_json``
    on failure.
    """

    def __init__(self, message: str, *, result: ComposeResult) -> None:
        super().__init__(message)
        self.result = result


def _decode(stream: bytes | None) -> str:
    """Decode subprocess output bytes with replacement for invalid UTF-8."""

    if stream is None:
        return ""
    return stream.decode("utf-8", errors="replace")


def _spawn_error_message(argv: Sequence[str], exc: OSError) -> str:
    executable = argv[0] if argv else "<unknown>"
    return f"failed to spawn {executable!r}: {type(exc).__name__}: {exc}"


class ComposeRunner:
    """Async wrapper around ``docker compose`` invocations.

    Parameters
    ----------
    compose_file:
        Absolute (or workspace-relative) path to the Compose file the
        runner should pin every command to via ``-f``. Validated in
        :func:`load_manifest` upstream - this class trusts the value.
    workspace_root:
        Workspace root path. Stored so callers can confirm the runner never
        writes inside it (no ``.env`` files,
        no override files), but the runner itself does not perform any
        filesystem writes.
    """

    def __init__(self, *, compose_file: Path, workspace_root: Path) -> None:
        self._compose_file = compose_file
        self._workspace_root = workspace_root

    def _compose_prefix(self) -> list[str]:
        """Return the shared ``docker compose`` argv prefix.

        The dashboard runs inside a container with ``WORKSPACE_ROOT=/app``.
        Mounting and passing ``/app/.env`` explicitly keeps Compose variable
        interpolation aligned with ``scripts/up.ps1`` without inheriting
        arbitrary host environment secrets. ``.env.local`` is layered after it
        when present so machine-local secrets stay out of tracked defaults.
        """

        argv = ["docker", "compose"]
        env_file = self._workspace_root / ".env"
        if env_file.is_file():
            argv.extend(["--env-file", str(env_file)])
        local_env_file = self._workspace_root / ".env.local"
        if local_env_file.is_file():
            argv.extend(["--env-file", str(local_env_file)])
        argv.extend(["-f", str(self._compose_file)])
        return argv

    # ------------------------------------------------------------------
    # Environment scrubbing
    # ------------------------------------------------------------------

    def _scrubbed_environ(self) -> dict[str, str]:
        """Return a minimal env dict for a child ``docker compose`` process.

        Only the keys in :data:`_ALLOWED_HOST_ENV_KEYS` are forwarded
        from :data:`os.environ`; everything else is dropped. Callers
        layer ``env_overrides`` on top of the returned dict - the
        scrub deliberately runs *before* the override step so a
        manifest-driven override always wins over a host-side leak of
        the same key.
        """

        env: dict[str, str] = {}
        for key in _ALLOWED_HOST_ENV_KEYS:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        return env

    def _build_env(self, env_overrides: Mapping[str, str] | None) -> dict[str, str]:
        """Combine the scrubbed host env with caller-supplied overrides."""

        env = self._scrubbed_environ()
        if env_overrides:
            for key, value in env_overrides.items():
                # The values are passed through verbatim. Validation /
                # masking lives in LifecycleService and the form layer
                # - by the time a value reaches the runner it has
                # already been schema-checked against the
                # ``.env.example`` LHS set.
                env[key] = value
        return env

    # ------------------------------------------------------------------
    # Internal subprocess primitives
    # ------------------------------------------------------------------

    async def _run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
    ) -> ComposeResult:
        """Spawn ``argv`` with ``shell=False`` and capture stdout/stderr.

        The argv list is forwarded **as-is** to
        :func:`asyncio.create_subprocess_exec`. No shell is involved -
        there is no opportunity for ``$(...)``, ``;``, ``|``, ``&&``
        or any other metacharacter to be re-interpreted.
        """

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ComposeResult(
                exit_code=127,
                stdout="",
                stderr=_spawn_error_message(argv, exc),
                argv=tuple(argv),
            )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return ComposeResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=_decode(stdout_bytes),
            stderr=_decode(stderr_bytes),
            argv=tuple(argv),
        )

    @staticmethod
    def _raise_on_failure(result: ComposeResult, *, action: str) -> ComposeResult:
        """Helper: raise :class:`ComposeFailureError` on non-zero exit."""

        if result.exit_code != 0:
            raise ComposeFailureError(
                f"docker compose {action} failed with exit code "
                f"{result.exit_code}",
                result=result,
            )
        return result

    # ------------------------------------------------------------------
    # Lifecycle commands
    # ------------------------------------------------------------------

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: Mapping[str, str] | None = None,
    ) -> ComposeResult:
        """Run ``docker compose -f F --profile P up -d --build S``.

        ``env_overrides`` are placed into the subprocess environment.
        They are **never** written to a temporary ``.env`` file or
        any other on-disk artefact.

        ``--build`` is intentional: operators start profiled services
        from the dashboard after pulling repository updates, and Compose
        otherwise reuses an old local image for services such as
        ``streamlit-ui``. Rebuilding here keeps dashboard-launched
        services aligned with the checked-out code.
        """

        argv: list[str] = [
            *self._compose_prefix(),
            "--profile",
            profile,
            "up",
            "-d",
            "--build",
            service_name,
        ]
        env = self._build_env(env_overrides)
        result = await self._run(argv, env=env)
        return self._raise_on_failure(result, action="up")

    async def stop(
        self,
        *,
        service_name: str,
        remove_volumes: bool = False,
    ) -> ComposeResult:
        """Run ``docker compose -f F stop S`` (and optionally ``rm -fv S``).

        When ``remove_volumes=True`` the runner additionally invokes
        ``docker compose -f F rm -fv S`` to tear down the stopped
        container and any **anonymous** volumes attached to it. Named
        volumes (``pg_data``, ``minio_data``, ``agent_workspace``) are owned by
        the top-level ``volumes:`` block
        in ``infra/docker-compose.yml`` and are not affected by
        ``rm -fv`` on a per-service basis.
        """

        # No env_overrides for stop - there are no Component-defined
        # variables to inject when tearing a service down.
        env = self._build_env(None)

        stop_argv: list[str] = [
            *self._compose_prefix(),
            "stop",
            service_name,
        ]
        stop_result = await self._run(stop_argv, env=env)
        self._raise_on_failure(stop_result, action="stop")

        if not remove_volumes:
            return stop_result

        rm_argv: list[str] = [
            *self._compose_prefix(),
            "rm",
            "-fv",
            service_name,
        ]
        rm_result = await self._run(rm_argv, env=env)
        # Surface the rm result so callers can inspect the final argv.
        # rm failures are reported the same way as stop failures -
        # the operator gets a 502 with the failing argv in the audit
        # detail.
        return self._raise_on_failure(rm_result, action="rm")

    async def restart(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: Mapping[str, str] | None = None,
    ) -> ComposeResult:
        """Stop then start the service, returning the *up* result.

        The semantics match Compose's own ``restart``: the container
        is torn down and brought back up, with any new
        ``env_overrides`` taking effect on the second boot. We
        intentionally invoke ``stop`` then ``up`` separately rather
        than calling ``docker compose restart`` so the override path
        stays consistent with :meth:`up`.
        """

        await self.stop(service_name=service_name, remove_volumes=False)
        return await self.up(
            profile=profile,
            service_name=service_name,
            env_overrides=env_overrides,
        )

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    async def logs(
        self,
        *,
        service_name: str,
        tail: int,
        follow: bool,
    ) -> ComposeResult | AsyncIterator[str]:
        """Run ``docker compose logs`` against ``service_name``.

        When ``follow=False`` the runner blocks until ``docker compose
        logs`` exits and returns a :class:`ComposeResult`. When
        ``follow=True`` the runner appends ``--follow`` and returns an
        async iterator that yields decoded stdout lines as they arrive
        - the caller is responsible for cancelling the iterator (and
        thus the subprocess) when the SSE client disconnects.
        """

        argv: list[str] = [
            *self._compose_prefix(),
            "logs",
            "--tail",
            str(tail),
            "--no-color",
        ]
        if follow:
            argv.append("--follow")
        argv.append(service_name)

        env = self._build_env(None)

        if not follow:
            return await self._run(argv, env=env)

        return self._stream_logs(argv, env=env)

    async def _stream_logs(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
    ) -> AsyncIterator[str]:
        """Async generator yielding decoded stdout lines for ``logs --follow``.

        We start the subprocess directly (still ``shell=False``) and
        loop on :meth:`StreamReader.readline` so the caller sees lines
        as Docker emits them. Cancelling the iterator (e.g. when the
        SSE client closes the connection) terminates the child.
        """

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            yield _spawn_error_message(argv, exc)
            return
        assert proc.stdout is not None  # PIPE configured above
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                yield raw.decode("utf-8", errors="replace").rstrip("\n")
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                # Best-effort wait - avoid leaking zombies.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:  # pragma: no cover - defensive
                    proc.kill()
                    await proc.wait()

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    async def exec_test(
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> TestResult:
        """Run ``docker compose exec -T <service> <argv...>``.

        The ``-T`` flag disables Compose's TTY allocation so the
        invocation is safe inside an HTTP request (no pseudo-terminal
        is needed; pytest's output is captured verbatim). The
        ``stream`` parameter is accepted for symmetry with the
        :class:`LifecycleService.run_tests` API surface; in this v1
        wrapper the output is always collected at process exit and
        returned via :class:`TestResult`. The router layer is
        responsible for line-by-line SSE relaying when ``stream=True``
        when streaming is requested.
        """

        full_argv: list[str] = [
            *self._compose_prefix(),
            "exec",
            "-T",
            service_name,
            *argv,
        ]
        env = self._build_env(None)
        # The ``stream`` flag is reserved for future enhancement; for
        # now it has no on-the-wire effect on the subprocess itself.
        del stream

        try:
            proc = await asyncio.create_subprocess_exec(
                *full_argv,
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return TestResult(
                exit_code=127,
                stdout="",
                stderr=_spawn_error_message(full_argv, exc),
                argv=tuple(full_argv),
            )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return TestResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=_decode(stdout_bytes),
            stderr=_decode(stderr_bytes),
            argv=tuple(full_argv),
        )


__all__ = (
    "ComposeFailureError",
    "ComposeResult",
    "ComposeRunner",
    "TestResult",
)
