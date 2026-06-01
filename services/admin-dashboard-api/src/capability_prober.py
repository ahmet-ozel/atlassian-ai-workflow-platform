"""Live capability probes for admin-dashboard-api."""

from __future__ import annotations

import asyncio
import io
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from .routers.capabilities import ProbeResult


_STATUS_TO_ROUTER = {
    "ok": "healthy",
    "error": "unhealthy",
    "not_configured": "not_configured",
}
_STATUS_FROM_ROUTER = {
    "healthy": "ok",
    "unhealthy": "error",
    "not_configured": "not_configured",
}


@dataclass(frozen=True, slots=True)
class _Runner:
    runner_id: str
    host: str
    port: int
    username: str
    vault_path: str


class AsyncpgCapabilityProbeStore:
    """Persist latest capability probe rows in ``shared.capability_probes``."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def upsert(self, result: ProbeResult) -> None:
        await self._pool.execute(
            """
            INSERT INTO shared.capability_probes
                (dept_id, service, status, error, latency_ms, probed_at)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
            ON CONFLICT (dept_id, service) DO UPDATE SET
                status = EXCLUDED.status,
                error = EXCLUDED.error,
                latency_ms = EXCLUDED.latency_ms,
                probed_at = EXCLUDED.probed_at
            """,
            result.dept_id,
            result.service,
            _STATUS_FROM_ROUTER[result.status],
            result.error,
            result.latency_ms,
            result.probed_at,
        )

    async def get_all(self) -> list[ProbeResult]:
        rows = await self._pool.fetch(
            """
            SELECT dept_id, service, status, error, latency_ms, probed_at
            FROM shared.capability_probes
            ORDER BY dept_id, service
            """
        )
        return [_row_to_result(row) for row in rows]

    async def get_one(self, *, dept_id: str, service: str) -> ProbeResult | None:
        row = await self._pool.fetchrow(
            """
            SELECT dept_id, service, status, error, latency_ms, probed_at
            FROM shared.capability_probes
            WHERE dept_id = $1 AND service = $2
            """,
            dept_id,
            service,
        )
        return _row_to_result(row) if row else None


class LiveCapabilityProber:
    """Run concrete external-service probes for the capability matrix."""

    def __init__(
        self,
        *,
        pool: Any,
        http_client: httpx.AsyncClient,
        vault_addr: str,
        vault_token: str,
        kv_mount: str = "secret",
        ssh_timeout_s: float = 20.0,
    ) -> None:
        self._pool = pool
        self._http = http_client
        self._vault_addr = vault_addr.rstrip("/")
        self._vault_token = vault_token
        self._kv_mount = kv_mount.strip("/")
        self._ssh_timeout_s = ssh_timeout_s

    async def probe(self, *, dept_id: str, service: str) -> ProbeResult:
        started = time.perf_counter()
        try:
            if service in {"ssh", "docker"}:
                await self._probe_runner(dept_id=dept_id, service=service)
            elif service in {"jira", "bitbucket", "confluence"}:
                await self._probe_atlassian(dept_id=dept_id, service=service)
            elif service == "llm":
                await self._pool.fetchval("SELECT 1")
            else:
                return ProbeResult(dept_id=dept_id, service=service, status="not_configured")
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                dept_id=dept_id,
                service=service,
                status="unhealthy",
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=_latency_ms(started),
                probed_at=datetime.now(timezone.utc),
            )
        return ProbeResult(
            dept_id=dept_id,
            service=service,
            status="healthy",
            error=None,
            latency_ms=_latency_ms(started),
            probed_at=datetime.now(timezone.utc),
        )

    async def _probe_runner(self, *, dept_id: str, service: str) -> None:
        runner = await self._resolve_runner(dept_id)
        secret = await self._read_vault(runner.vault_path)
        host = str(secret.get("host") or runner.host)
        port = int(secret.get("port") or runner.port)
        user = str(secret.get("user") or secret.get("username") or runner.username)
        private_key = str(secret.get("private_key") or secret.get("private_pem") or "")
        if not host or not user or not private_key:
            raise RuntimeError("runner SSH secret is missing host/user/private_key")
        command = "true" if service == "ssh" else "docker info >/dev/null"
        await asyncio.to_thread(
            _ssh_exec,
            host,
            port,
            user,
            private_key,
            command,
            self._ssh_timeout_s,
        )

    async def _resolve_runner(self, dept_id: str) -> _Runner:
        row = await self._pool.fetchrow(
            """
            SELECT r.runner_id, r.host, r.port, r.username, r.vault_path
            FROM infrastructure.ssh_runners r
            JOIN infrastructure.dept_ssh_assignments a
              ON a.runner_id = r.runner_id
            WHERE a.dept_id = $1 AND r.status = 'active'
            ORDER BY a.priority ASC, r.runner_id ASC
            LIMIT 1
            """,
            dept_id,
        )
        if row is None:
            row = await self._pool.fetchrow(
                """
                SELECT runner_id, host, port, username, vault_path
                FROM infrastructure.ssh_runners
                WHERE status = 'active'
                ORDER BY runner_id ASC
                LIMIT 1
                """
            )
        if row is None:
            raise RuntimeError("no active SSH runner configured")
        return _Runner(
            runner_id=str(row["runner_id"]),
            host=str(row["host"]),
            port=int(row["port"]),
            username=str(row["username"]),
            vault_path=str(row["vault_path"]),
        )

    async def _probe_atlassian(self, *, dept_id: str, service: str) -> None:
        cred_ref = await self._pool.fetchval(
            """
            SELECT credential_ref
            FROM automation.department_bots
            WHERE department_id = $1 AND service = $2
            """,
            dept_id,
            service,
        )
        if not cred_ref:
            raise RuntimeError(f"{service} credential_ref is not configured")
        credential = await self._read_vault(str(cred_ref))
        url = str(credential.get("url") or "").rstrip("/")
        username = str(credential.get("username") or credential.get("email") or "")
        token = str(
            credential.get("api_token")
            or credential.get("personal_token")
            or credential.get("token")
            or ""
        )
        if not url or not token:
            raise RuntimeError(f"{service} credential is incomplete")
        if service == "jira":
            response = await self._http.get(f"{url}/rest/api/3/myself", auth=(username, token))
        elif service == "confluence":
            base = url[:-5] if url.endswith("/wiki") else url
            response = await self._http.get(f"{base}/wiki/rest/api/space?limit=1", auth=(username, token))
        else:
            if username:
                response = await self._http.get(
                    "https://api.bitbucket.org/2.0/user",
                    auth=(username, token),
                )
            else:
                response = await self._http.get(
                    "https://api.bitbucket.org/2.0/user",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code == 403:
                repo_parts = [
                    part for part in urlparse(url).path.split("/") if part
                ]
                if len(repo_parts) >= 2 and username:
                    response = await self._http.get(
                        "https://api.bitbucket.org/2.0/repositories/"
                        f"{repo_parts[0]}/{repo_parts[1]}",
                        auth=(username, token),
                    )
        response.raise_for_status()

    async def _read_vault(self, raw_path: str) -> Mapping[str, str]:
        path = raw_path[len("vault:") :] if raw_path.startswith("vault:") else raw_path
        url = f"{self._vault_addr}/v1/{self._kv_mount}/data/{path.strip('/')}"
        response = await self._http.get(
            url,
            headers={"X-Vault-Token": self._vault_token},
        )
        if response.status_code == 404:
            raise KeyError(raw_path)
        response.raise_for_status()
        data = response.json().get("data", {}).get("data", {})
        if not isinstance(data, Mapping):
            return {}
        return {str(k): str(v) for k, v in data.items()}


def _row_to_result(row: Any) -> ProbeResult:
    return ProbeResult(
        dept_id=str(row["dept_id"]),
        service=str(row["service"]),
        status=_STATUS_TO_ROUTER.get(str(row["status"]), "unhealthy"),
        error=row["error"],
        latency_ms=row["latency_ms"],
        probed_at=row["probed_at"],
    )


def _latency_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _ssh_exec(
    host: str,
    port: int,
    user: str,
    private_key: str,
    command: str,
    timeout_s: float,
) -> None:
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        key = _parse_private_key(private_key)
        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=key,
            timeout=timeout_s,
            banner_timeout=timeout_s,
            auth_timeout=timeout_s,
            allow_agent=False,
            look_for_keys=False,
        )
        _, stdout, stderr = client.exec_command(command, timeout=timeout_s)
        code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace")[:300]
        if code != 0:
            raise RuntimeError(f"command failed with exit={code}: {err}")
    finally:
        client.close()


def _parse_private_key(private_key: str) -> Any:
    import paramiko

    last_error: Exception | None = None
    for key_class in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        key_file = io.StringIO(private_key)
        try:
            return key_class.from_private_key(key_file)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"unable to parse private key: {last_error}")
