"""Kind-aware health probe for Managed_Service entries.

This module turns a :class:`~src.manifest.ManagedServiceEntry` into a
fresh :class:`HealthSnapshot` by routing the probe to one of three
strategies based on the entry's ``kind`` and ``health_endpoint``:

1. **Worker** (``kind == "worker"``): asks the Temporal cluster
   advertised by ``temporal_host`` to confirm that the worker's
   namespace/server is reachable via
   :py:meth:`temporalio.client.Client.connect`. A successful connect
   maps to ``healthz_status=200, state="healthy"``; any timeout or
   exception maps to ``healthz_status=-1, state="unhealthy"``.
   ``readyz_*`` fields are ``None`` because workers do not expose an HTTP
   readiness endpoint.

2. **No HTTP endpoint** (``health_endpoint is None`` and
   ``kind != "worker"``): falls through to ``_probe_assume_running``
   which shells out to ``docker inspect <container>
   --format '{{.State.Health.Status}}'`` (5 s timeout) and maps the
   native Docker healthcheck status to a :data:`HealthState`:
   ``"healthy"  "healthy"``, ``"unhealthy"  "unhealthy"``,
   ``"starting"  "starting"``, and the empty string / ``"<no value>"``
   / subprocess failure (timeout, missing ``docker`` binary, exit
   code != 0)  ``"running_unmonitored"`` (Compose ``healthcheck``
   block absent - the control plane records that the container is up
   but unobservable via Docker).

3. **HTTP service** (``health_endpoint is not None``): performs ``GET
   http://{compose_service_name}:{port}{health_endpoint}`` and
   ``GET http://{compose_service_name}:{port}/readyz`` over the Compose
   internal network. Both endpoints must return ``200`` for the service
   to be ``healthy``; any other status (or connection failure) flips
   the snapshot to ``unhealthy`` and the failing response body is
   surfaced via ``readyz_body`` truncated to 200 chars. ``kind == "infra"``
   entries with a health endpoint are
   healthz-only because several infrastructure helpers expose no
   platform ``/readyz`` contract.

The module is **pure asyncio + httpx**; it owns no global state and no
filesystem I/O. Port resolution is deferred to the caller through the
``compose_internal_ports`` constructor map so that this module does not
have to parse ``infra/docker-compose.yml`` itself (the
:class:`LifecycleService` wires the map at startup).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal, Mapping

import httpx

from ..manifest import ManagedServiceEntry

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


HealthState = Literal[
    "healthy",
    "unhealthy",
    "starting",
    "unknown",
    "running_unmonitored",
]


@dataclass(frozen=True)
class HealthSnapshot:
    """Single point-in-time health observation for a Managed_Service.

    Frozen so callers can hand the snapshot around the control plane
    (state cache, REST handlers, audit details) without worrying about
    accidental mutation. All timestamps are timezone-aware UTC.

    Attributes
    ----------
    ts:
        Probe completion time in UTC.
    healthz_status:
        HTTP status returned by the ``/healthz`` request. ``-1``
        indicates a connection-level failure (timeout, DNS, TCP reset)
        rather than an HTTP error response. For worker entries this is
        ``200`` on a successful Temporal ``Client.connect`` and ``-1``
        when the connect fails.
    healthz_body:
        Up to 200 characters of the ``/healthz`` response body (or the
        error message text on connection failure). Truncated to keep
        snapshots cheap to store and serialize.
    readyz_status:
        HTTP status of the ``/readyz`` request, or ``None`` for worker
        and assume-running entries which do not probe ``/readyz``.
    readyz_body:
        Up to 200 characters of the ``/readyz`` response body, or
        ``None`` when ``readyz_status`` is ``None``.
    state:
        Roll-up health verdict used by the UI and the lifecycle state
        machine. ``healthy`` requires both endpoints to return ``200``
        (HTTP) or a successful Temporal connect (worker); ``unhealthy``
        is any failure mode; ``starting`` reflects a Docker
        ``healthcheck`` that is still in its initial probe window;
        ``running_unmonitored`` is emitted for entries with no probe
        (``health_endpoint is None`` and ``kind != "worker"``) whose
        Compose container has no ``healthcheck`` block - the
        container is up but the control plane cannot observe its
        health. ``unknown`` is retained on the type for
        backwards compatibility with persisted snapshots and is not
        emitted by the current probe.
    """

    ts: datetime
    healthz_status: int
    healthz_body: str
    readyz_status: int | None
    readyz_body: str | None
    state: HealthState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Maximum number of characters retained from any ``/healthz`` or
#: ``/readyz`` response body. Bodies are truncated *after* decoding so
#: the snapshot stays bounded regardless of the upstream payload size
#: using the same truncation limit throughout the probe.
_MAX_BODY_CHARS: Final[int] = 200

#: Per-request timeout in seconds for both HTTP probes and the Temporal
#: ``Client.connect`` call. Matches the HTTP client contract ("httpx.AsyncClient
#: timeout 5s") and applies symmetrically to the worker path so a
#: hung Temporal frontend cannot block the probe loop.
_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0

#: Default port used when the caller did not supply an entry for a
#: given ``compose_service_name`` in ``compose_internal_ports``. ``80``
#: is the conventional HTTP container port used when no explicit mapping is
#: available.
_DEFAULT_INTERNAL_PORT: Final[int] = 80

#: Path probed alongside ``health_endpoint`` for HTTP services. The
#: standard HTTP services expose ``/readyz`` alongside their health endpoint.
_READYZ_PATH: Final[str] = "/readyz"


#: Per-call timeout in seconds for the ``docker inspect`` subprocess
#: used by :meth:`HealthProbe._probe_assume_running`. Matches the
#: short cap means a hung Docker daemon cannot block the lifecycle
#: state cache refresh loop, and the caller can deterministically
#: classify the missing reading as ``running_unmonitored``.
_DOCKER_INSPECT_TIMEOUT_SECONDS: Final[float] = 5.0


#: Mapping from ``docker inspect`` ``.State.Health.Status`` strings to
#: the :data:`HealthState` literal emitted by ``_probe_assume_running``.
#: Anything not in this map (empty string, ``"<no value>"``, or a
#: subprocess failure) is classified as ``"running_unmonitored"``.
_DOCKER_HEALTH_STATUS_MAP: Final[dict[str, HealthState]] = {
    "healthy": "healthy",
    "unhealthy": "unhealthy",
    "starting": "starting",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware ``datetime``."""

    return datetime.now(timezone.utc)


def _truncate_body(text: str) -> str:
    """Clamp ``text`` to :data:`_MAX_BODY_CHARS` characters.

    Slicing on a Python ``str`` is character-safe (operates on code
    points), so this is sufficient for the body-truncation rule in
    the probe body limit - there is no need to worry about splitting a UTF-8
    multibyte sequence mid-codepoint.
    """

    if len(text) <= _MAX_BODY_CHARS:
        return text
    return text[:_MAX_BODY_CHARS]


def _resolve_port(
    compose_service_name: str,
    compose_internal_ports: Mapping[str, int],
) -> int:
    """Look up the in-network port for ``compose_service_name``.

    Returns the caller-supplied port when present, falling back to
    :data:`_DEFAULT_INTERNAL_PORT` otherwise. Keeping the resolver here
    (rather than in :class:`HealthProbe.__init__`) means the same
    instance can serve a manifest that grows new entries between
    process restarts, as long as the caller updates the map.
    """

    return compose_internal_ports.get(compose_service_name, _DEFAULT_INTERNAL_PORT)


# ---------------------------------------------------------------------------
# HealthProbe
# ---------------------------------------------------------------------------


class HealthProbe:
    """Probe a single Managed_Service and return a :class:`HealthSnapshot`.

    Stateless aside from the shared ``httpx.AsyncClient``, the Temporal
    target host string, and the immutable port-resolution map. Multiple
    :meth:`probe` calls can run concurrently; each one issues fresh
    HTTP requests and (for workers) a fresh ``Client.connect``.

    Parameters
    ----------
    http_client:
        Pre-configured :class:`httpx.AsyncClient`. The probe uses
        :data:`_PROBE_TIMEOUT_SECONDS` per request via
        :class:`httpx.Timeout`, so the client itself does **not** need
        a matching default timeout - but it is the caller's
        responsibility to manage the client's lifecycle (open at
        startup, close at shutdown).
    temporal_host:
        ``host:port`` string handed to
        :py:meth:`temporalio.client.Client.connect` for the worker
        probe. Workers in the same Compose network typically reach
        ``temporal:7233``.
    compose_internal_ports:
        Optional map from ``compose_service_name`` to the in-network
        port the service listens on (e.g. ``{"automation-service":
        8080}``). Lookups missing from the map default to
        :data:`_DEFAULT_INTERNAL_PORT`. The caller (typically
        :class:`~src.lifecycle.service.LifecycleService`) wires this
        from a hard-coded mapping or by inspecting
        ``infra/docker-compose.yml`` at startup.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        temporal_host: str,
        compose_internal_ports: Mapping[str, int] | None = None,
        prefer_docker_health: bool = False,
    ) -> None:
        self._http_client = http_client
        self._temporal_host = temporal_host
        self._prefer_docker_health = prefer_docker_health
        # Defensive copy  the snapshot of ports the probe sees stays
        # stable for the lifetime of the instance, even if the caller
        # mutates the original map after construction.
        self._compose_internal_ports: dict[str, int] = dict(
            compose_internal_ports or {}
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        """Return a fresh :class:`HealthSnapshot` for ``entry``.

        Dispatch order:

        1. ``kind == "worker"``  :meth:`_probe_temporal_worker`.
        2. ``health_endpoint is None``  :meth:`_probe_assume_running`.
        3. otherwise  :meth:`_probe_http`.

        The method never raises; every failure mode (timeout, DNS,
        non-2xx) is captured inside the returned snapshot so the
        caller can persist a deterministic record per probe.
        """

        if entry.kind == "worker":
            return await self._probe_temporal_worker(entry)
        if entry.health_endpoint is None:
            return await self._probe_assume_running(entry)
        return await self._probe_http(entry)

    # ------------------------------------------------------------------
    # Worker probe
    # ------------------------------------------------------------------

    async def _probe_temporal_worker(
        self, entry: ManagedServiceEntry
    ) -> HealthSnapshot:
        """Confirm a Temporal worker can reach the cluster.

        We deliberately import :mod:`temporalio.client` lazily inside
        the method so that:

        * the module remains importable in environments where
          ``temporalio`` is missing (e.g. a thin CI image running only
          :func:`parse_env_example` tests), and
        * unit tests can monkeypatch ``temporalio.client.Client``
          before any probe runs.

        A successful ``Client.connect`` is treated as the ping itself -
        the gRPC handshake exercises the same path the worker does on
        startup, which is the worker reachability check. Any
        :class:`asyncio.TimeoutError` or other exception flips the
        snapshot to ``unhealthy`` with ``healthz_status=-1``.
        """

        has_container, container_body = await self._compose_service_has_running_container(
            entry.compose_service_name
        )
        if not has_container:
            return HealthSnapshot(
                ts=_utcnow(),
                healthz_status=-1,
                healthz_body=_truncate_body(container_body),
                readyz_status=None,
                readyz_body=None,
                state="unhealthy",
            )

        # Local import keeps this module importable even when
        # ``temporalio`` is absent and lets tests patch the symbol.
        from temporalio.client import Client  # type: ignore[import-not-found]

        try:
            await asyncio.wait_for(
                Client.connect(self._temporal_host),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return HealthSnapshot(
                ts=_utcnow(),
                healthz_status=-1,
                healthz_body=_truncate_body(
                    f"temporal connect timed out after "
                    f"{_PROBE_TIMEOUT_SECONDS:.1f}s "
                    f"(host={self._temporal_host})"
                ),
                readyz_status=None,
                readyz_body=None,
                state="unhealthy",
            )
        except Exception as exc:  # noqa: BLE001 - record any failure shape.
            return HealthSnapshot(
                ts=_utcnow(),
                healthz_status=-1,
                healthz_body=_truncate_body(
                    f"temporal connect failed: {type(exc).__name__}: {exc}"
                ),
                readyz_status=None,
                readyz_body=None,
                state="unhealthy",
            )

        return HealthSnapshot(
            ts=_utcnow(),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=None,
            readyz_body=None,
            state="healthy",
        )

    async def _compose_service_has_running_container(
        self,
        compose_service_name: str,
    ) -> tuple[bool, str]:
        """Return whether Docker has a running container for a Compose service."""

        names, body = await self._compose_service_running_container_names(
            compose_service_name
        )
        if not names:
            return False, body
        return True, "running container(s): " + ", ".join(names[:3])

    async def _compose_service_running_container_names(
        self,
        compose_service_name: str,
    ) -> tuple[list[str], str]:
        """Return running Docker container names for a Compose service."""

        cmd = (
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.service={compose_service_name}",
            "--filter",
            "status=running",
            "--format",
            "{{.Names}}",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, (
                "docker ps unavailable: 'docker' binary not found "
                f"on PATH (service={compose_service_name!r})"
            )
        except OSError as exc:  # pragma: no cover - defensive
            return False, (
                f"docker ps spawn failed: {type(exc).__name__}: {exc} "
                f"(service={compose_service_name!r})"
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=_DOCKER_INSPECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass
            return False, (
                "docker ps timed out after "
                f"{_DOCKER_INSPECT_TIMEOUT_SECONDS:.1f}s "
                f"(service={compose_service_name!r})"
            )

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return False, (
                f"docker ps exited {proc.returncode} "
                f"(service={compose_service_name!r}): {stderr_text}"
            )

        names = [
            line.strip()
            for line in stdout_bytes.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not names:
            return [], (
                "no running Docker container found for Compose service "
                f"{compose_service_name!r}"
            )
        return names, "running container(s): " + ", ".join(names[:3])

    # ------------------------------------------------------------------
    # Assume-running probe - docker inspect
    # ------------------------------------------------------------------

    async def _probe_assume_running(
        self, entry: ManagedServiceEntry
    ) -> HealthSnapshot:
        """Read ``docker inspect`` for entries without an HTTP probe.

        Manifest entries with ``health_endpoint=null`` and
        ``kind != "worker"`` (``redis``, ``minio``, ``temporal-ui``,
        …) cannot be polled over HTTP and are not Temporal workers,
        but the Compose container itself may still expose a native
        Docker ``healthcheck`` block. We shell out to::

            docker inspect <compose_service_name> \\
                --format '{{.State.Health.Status}}'

        with a :data:`_DOCKER_INSPECT_TIMEOUT_SECONDS` cap and map the
        single-line stdout per :data:`_DOCKER_HEALTH_STATUS_MAP`:

        * ``"healthy"``  ``state="healthy"``
        * ``"unhealthy"``  ``state="unhealthy"``
        * ``"starting"``  ``state="starting"``

        Any other shape - empty string (no ``Health`` block in the
        container's state), the literal ``"<no value>"`` (Go template
        rendering of a missing field), an unknown status, or a
        subprocess failure (timeout, missing ``docker`` binary, exit
        code != 0, decode error) - is classified as
        ``state="running_unmonitored"``: the container appears to be
        up but the control plane has no signal to confirm it
        (Compose ``healthcheck`` block likely absent). This honours
        The fallback behavior does not fabricate a green tick, but also does not
        flag the service as unhealthy when the operator's intent was
        "assume running".

        The probe never raises; every error path is folded into the
        snapshot so the caller (the lifecycle state cache) can
        record a deterministic reading per cycle.
        """

        status, body = await self._docker_inspect_health_status(
            entry.compose_service_name
        )
        state: HealthState = _DOCKER_HEALTH_STATUS_MAP.get(
            status, "running_unmonitored"
        )

        return HealthSnapshot(
            ts=_utcnow(),
            healthz_status=-1,
            healthz_body=_truncate_body(body),
            readyz_status=None,
            readyz_body=None,
            state=state,
        )

    async def _docker_inspect_health_status(
        self, container_name: str
    ) -> tuple[str, str]:
        """Run ``docker inspect`` and return ``(status, diagnostic_body)``.

        Returns
        -------
        tuple[str, str]
            ``status`` is the trimmed stdout (the value of
            ``.State.Health.Status``) on success and the empty string
            on any failure path. ``diagnostic_body`` is a short,
            human-readable string suitable for ``healthz_body`` so the
            UI tooltip can explain *why* the snapshot landed on
            ``running_unmonitored`` (timeout, missing binary,
            non-zero exit, etc.). Both values are unbounded here;
            :meth:`_probe_assume_running` truncates the body before
            building the snapshot.
        """

        cmd = (
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{.State.Health.Status}}",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # No ``docker`` binary on PATH - typical in unit-test
            # environments and CI containers without Docker-in-Docker.
            return "", (
                "docker inspect unavailable: 'docker' binary not found "
                f"on PATH (container={container_name!r})"
            )
        except OSError as exc:  # pragma: no cover - defensive
            return "", (
                f"docker inspect spawn failed: {type(exc).__name__}: {exc} "
                f"(container={container_name!r})"
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=_DOCKER_INSPECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # The subprocess is still alive - try to terminate it so we
            # don't leak a process. ``kill`` is best-effort; if it
            # fails (proc already exited, or we lack permissions) we
            # swallow the error because the caller's only contract is
            # the (status, body) tuple.
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover - race
                pass
            return "", (
                "docker inspect timed out after "
                f"{_DOCKER_INSPECT_TIMEOUT_SECONDS:.1f}s "
                f"(container={container_name!r})"
            )

        if proc.returncode != 0:
            # Most common cause: container with that name does not
            # exist (operator stopped it out-of-band). The stderr
            # line from Docker is the useful diagnostic.
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return "", (
                f"docker inspect exited {proc.returncode} "
                f"(container={container_name!r}): {stderr_text}"
            )

        try:
            status = stdout_bytes.decode("utf-8", errors="replace").strip()
        except Exception as exc:  # pragma: no cover - decode is lenient
            return "", (
                f"docker inspect output decode failed: "
                f"{type(exc).__name__}: {exc}"
            )

        # Empty string and the Go-template "<no value>" sentinel both
        # mean "no Health block" - i.e. the Compose service has no
        # ``healthcheck`` directive. Surface a stable diagnostic so
        # operators can tell the difference between "healthcheck not
        # configured" and "healthcheck running".
        if status in ("", "<no value>"):
            return "", (
                f"container {container_name!r} has no healthcheck "
                "(running_unmonitored)"
            )

        return status, f"docker healthcheck status: {status}"

    async def _docker_inspect_compose_service_health_status(
        self, compose_service_name: str
    ) -> tuple[str, str]:
        """Return Docker health status for the running container of a service."""

        names, body = await self._compose_service_running_container_names(
            compose_service_name
        )
        if not names:
            return "", body
        return await self._docker_inspect_health_status(names[0])

    async def _probe_docker_health(
        self, entry: ManagedServiceEntry
    ) -> HealthSnapshot | None:
        """Prefer Docker's native healthcheck when a running container has one."""

        status, body = await self._docker_inspect_compose_service_health_status(
            entry.compose_service_name
        )
        if status not in _DOCKER_HEALTH_STATUS_MAP:
            return None

        state = _DOCKER_HEALTH_STATUS_MAP[status]
        return HealthSnapshot(
            ts=_utcnow(),
            healthz_status=200 if state == "healthy" else -1,
            healthz_body=_truncate_body(body),
            readyz_status=None,
            readyz_body=None,
            state=state,
        )

    # ------------------------------------------------------------------
    # HTTP probe
    # ------------------------------------------------------------------

    async def _probe_http(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        """Probe ``/healthz`` and ``/readyz`` over the Compose network.

        URL shape: ``http://{compose_service_name}:{port}{path}``. The
        service hostname is the Compose service key - Docker's internal
        DNS resolves it within the project network, no host-side port
        publish required.

        Both probes share the same per-request timeout. If either
        request fails to connect, the snapshot uses ``status=-1`` and
        the truncated exception text as the body, mirroring the worker
        path so log scrapers can pattern-match on the same sentinel.
        """

        assert entry.health_endpoint is not None  # narrowed by ``probe``

        if self._prefer_docker_health:
            docker_snapshot = await self._probe_docker_health(entry)
            if docker_snapshot is not None:
                return docker_snapshot

        port = _resolve_port(entry.compose_service_name, self._compose_internal_ports)
        base_url = f"http://{entry.compose_service_name}:{port}"

        healthz_status, healthz_body = await self._fetch(
            f"{base_url}{entry.health_endpoint}"
        )
        if entry.kind in {"infra", "ui", "sidecar"}:
            return HealthSnapshot(
                ts=_utcnow(),
                healthz_status=healthz_status,
                healthz_body=healthz_body,
                readyz_status=None,
                readyz_body=None,
                state="healthy" if healthz_status == 200 else "unhealthy",
            )

        readyz_status, readyz_body = await self._fetch(f"{base_url}{_READYZ_PATH}")

        # Roll-up: both endpoints must return 200 for the service to be
        # considered healthy. Any other shape (non-2xx, ``-1`` connect
        # failure) flips us to unhealthy. The failing-side body
        # already carries the diagnostic; we surface it as-is on the
        # readyz field so the UI tooltip can render it.
        state: HealthState = (
            "healthy" if healthz_status == 200 and readyz_status == 200 else "unhealthy"
        )

        return HealthSnapshot(
            ts=_utcnow(),
            healthz_status=healthz_status,
            healthz_body=healthz_body,
            readyz_status=readyz_status,
            readyz_body=readyz_body,
            state=state,
        )

    async def _fetch(self, url: str) -> tuple[int, str]:
        """GET ``url`` with the probe timeout, returning ``(status, body)``.

        On a connection-level failure (timeout, DNS, refused, reset)
        the status is ``-1`` and the body is a short, truncated
        diagnostic of the form ``"<ExceptionName>: <message>"``. This
        keeps :meth:`_probe_http` exception-free and lets it
        deterministically build the snapshot.
        """

        timeout = httpx.Timeout(_PROBE_TIMEOUT_SECONDS)
        try:
            response = await self._http_client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            return -1, _truncate_body(f"{type(exc).__name__}: {exc}")
        # ``response.text`` decodes the body using the response's
        # declared charset (or a sensible default), which is what the
        # UI ultimately renders. Truncate *after* decoding to keep the
        # rule "≤200 characters" from accidentally counting bytes.
        return response.status_code, _truncate_body(response.text)


__all__ = ("HealthSnapshot", "HealthState", "HealthProbe")
