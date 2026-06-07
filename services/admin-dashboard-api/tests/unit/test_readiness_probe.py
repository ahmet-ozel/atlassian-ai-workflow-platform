"""Unit tests for the readiness probe module.

These tests cover the readiness probe outcomes:

* PostgreSQL probe executes SELECT 1.
* Each probe has 3s timeout; timeout  unreachable.
* Any unreachable dependency returns 503 with failed_dependencies list.
* All reachable dependencies return 200 with {"status": "ready"}.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Bootstrap sys.path so ``import src.*`` resolves under direct
# ``pytest tests/unit`` invocations from the service root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.readiness import (
    DependencyProbeResult,
    PROBE_TIMEOUT_SECONDS,
    check_readiness,
    probe_postgres,
    probe_redis,
    probe_temporal,
    probe_vault,
    _parse_redis_url,
)


# ---------------------------------------------------------------------------
# _parse_redis_url helper tests
# ---------------------------------------------------------------------------


class TestParseRedisUrl:
    """Tests for the Redis URL parser helper."""

    def test_full_url_with_scheme(self) -> None:
        host, port = _parse_redis_url("redis://localhost:6379")
        assert host == "localhost"
        assert port == 6379

    def test_url_with_credentials(self) -> None:
        host, port = _parse_redis_url("redis://user:password@myhost:6380")
        assert host == "myhost"
        assert port == 6380

    def test_url_with_db_number(self) -> None:
        host, port = _parse_redis_url("redis://redis-host:6379/0")
        assert host == "redis-host"
        assert port == 6379

    def test_host_port_without_scheme(self) -> None:
        host, port = _parse_redis_url("redis:6379")
        assert host == "redis"
        assert port == 6379

    def test_host_only_defaults_port(self) -> None:
        host, port = _parse_redis_url("redis")
        assert host == "redis"
        assert port == 6379

    def test_url_with_scheme_no_port(self) -> None:
        host, port = _parse_redis_url("redis://myhost")
        assert host == "myhost"
        assert port == 6379


# ---------------------------------------------------------------------------
# probe_postgres tests
# ---------------------------------------------------------------------------


class TestProbePostgres:
    """Tests for the PostgreSQL probe."""

    def test_successful_probe(self) -> None:
        """SELECT 1 succeeds  reachable."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)
        mock_conn.close = AsyncMock()

        async def mock_connect(*args, **kwargs):
            return mock_conn

        with patch("asyncpg.connect", side_effect=mock_connect):
            result = asyncio.run(probe_postgres("postgresql://test:test@localhost/db"))

        assert result.name == "postgres"
        assert result.reachable is True
        assert result.latency_ms is not None
        assert result.latency_ms >= 0

    def test_connection_timeout(self) -> None:
        """Timeout  unreachable."""

        async def slow_connect(*args, **kwargs):
            await asyncio.sleep(10)

        with patch("asyncpg.connect", side_effect=slow_connect):
            result = asyncio.run(probe_postgres("postgresql://test:test@localhost/db"))

        assert result.name == "postgres"
        assert result.reachable is False
        assert result.latency_ms is None

    def test_connection_refused(self) -> None:
        """Connection error  unreachable."""

        async def fail_connect(*args, **kwargs):
            raise OSError("Connection refused")

        with patch("asyncpg.connect", side_effect=fail_connect):
            result = asyncio.run(probe_postgres("postgresql://test:test@localhost/db"))

        assert result.name == "postgres"
        assert result.reachable is False
        assert result.latency_ms is None


# ---------------------------------------------------------------------------
# probe_redis tests
# ---------------------------------------------------------------------------


class TestProbeRedis:
    """Tests for the Redis probe."""

    def test_successful_ping(self) -> None:
        """PING  PONG means reachable."""

        async def _run():
            # Create a mock server that responds with +PONG
            server_ready = asyncio.Event()

            async def handle_client(reader, writer):
                data = await reader.read(100)
                writer.write(b"+PONG\r\n")
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server_ready.set()

            try:
                result = await probe_redis(f"redis://127.0.0.1:{port}")
                assert result.name == "redis"
                assert result.reachable is True
                assert result.latency_ms is not None
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(_run())

    def test_connection_refused(self) -> None:
        """Connection refused  unreachable."""
        # Use a port that's almost certainly not listening
        result = asyncio.run(probe_redis("redis://127.0.0.1:1"))

        assert result.name == "redis"
        assert result.reachable is False
        assert result.latency_ms is None


# ---------------------------------------------------------------------------
# probe_temporal tests
# ---------------------------------------------------------------------------


class TestProbeTemporal:
    """Tests for the Temporal probe."""

    def test_successful_connect(self) -> None:
        """gRPC connect succeeds  reachable."""

        async def mock_connect(host, **kwargs):
            return MagicMock()

        with patch("temporalio.client.Client.connect", side_effect=mock_connect):
            result = asyncio.run(probe_temporal("temporal:7233"))

        assert result.name == "temporal"
        assert result.reachable is True
        assert result.latency_ms is not None

    def test_connect_timeout(self) -> None:
        """Timeout  unreachable."""

        async def slow_connect(host, **kwargs):
            await asyncio.sleep(10)

        with patch("temporalio.client.Client.connect", side_effect=slow_connect):
            result = asyncio.run(probe_temporal("temporal:7233"))

        assert result.name == "temporal"
        assert result.reachable is False
        assert result.latency_ms is None

    def test_connect_error(self) -> None:
        """Connection error  unreachable."""

        async def fail_connect(host, **kwargs):
            raise OSError("Connection refused")

        with patch("temporalio.client.Client.connect", side_effect=fail_connect):
            result = asyncio.run(probe_temporal("temporal:7233"))

        assert result.name == "temporal"
        assert result.reachable is False
        assert result.latency_ms is None


# ---------------------------------------------------------------------------
# probe_vault tests
# ---------------------------------------------------------------------------


class TestProbeVault:
    """Tests for the Vault probe."""

    def test_successful_health_check(self) -> None:
        """Vault responds with HTTP 200  reachable."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = asyncio.run(probe_vault("http://vault:8200"))

        assert result.name == "vault"
        assert result.reachable is True
        assert result.latency_ms is not None

    def test_vault_sealed_still_reachable(self) -> None:
        """Vault responds with HTTP 503 (sealed)  still reachable."""
        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_response)
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = asyncio.run(probe_vault("http://vault:8200"))

        assert result.name == "vault"
        assert result.reachable is True

    def test_connection_timeout(self) -> None:
        """Timeout  unreachable."""
        import httpx as httpx_mod

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(side_effect=httpx_mod.ConnectTimeout("timeout"))
            instance.__aenter__ = AsyncMock(return_value=instance)
            instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = instance

            result = asyncio.run(probe_vault("http://vault:8200"))

        assert result.name == "vault"
        assert result.reachable is False
        assert result.latency_ms is None


# ---------------------------------------------------------------------------
# check_readiness tests
# ---------------------------------------------------------------------------


class TestCheckReadiness:
    """Tests for the readiness aggregation function."""

    def test_all_dependencies_reachable(self) -> None:
        """All reachable  ready."""

        async def probe_ok_1():
            return DependencyProbeResult(name="postgres", reachable=True, latency_ms=1.5)

        async def probe_ok_2():
            return DependencyProbeResult(name="vault", reachable=True, latency_ms=2.0)

        all_ready, details = asyncio.run(check_readiness([probe_ok_1, probe_ok_2]))

        assert all_ready is True
        assert details == {"status": "ready"}

    def test_one_dependency_unreachable(self) -> None:
        """One unreachable  not_ready with failed list."""

        async def probe_ok():
            return DependencyProbeResult(name="postgres", reachable=True, latency_ms=1.5)

        async def probe_fail():
            return DependencyProbeResult(name="redis", reachable=False, latency_ms=None)

        all_ready, details = asyncio.run(check_readiness([probe_ok, probe_fail]))

        assert all_ready is False
        assert details["status"] == "not_ready"
        assert "redis" in details["failed_dependencies"]
        assert "postgres" not in details["failed_dependencies"]

    def test_multiple_dependencies_unreachable(self) -> None:
        """Multiple failures  all listed in failed_dependencies."""

        async def probe_fail_pg():
            return DependencyProbeResult(name="postgres", reachable=False, latency_ms=None)

        async def probe_fail_redis():
            return DependencyProbeResult(name="redis", reachable=False, latency_ms=None)

        async def probe_ok_vault():
            return DependencyProbeResult(name="vault", reachable=True, latency_ms=3.0)

        all_ready, details = asyncio.run(
            check_readiness([probe_fail_pg, probe_fail_redis, probe_ok_vault])
        )

        assert all_ready is False
        assert details["status"] == "not_ready"
        assert set(details["failed_dependencies"]) == {"postgres", "redis"}

    def test_empty_dependencies_is_ready(self) -> None:
        """No dependencies configured  ready."""
        all_ready, details = asyncio.run(check_readiness([]))

        assert all_ready is True
        assert details == {"status": "ready"}

    def test_probes_run_in_parallel(self) -> None:
        """Probes should run concurrently, not sequentially."""
        import time

        async def slow_probe_1():
            await asyncio.sleep(0.1)
            return DependencyProbeResult(name="dep1", reachable=True, latency_ms=100)

        async def slow_probe_2():
            await asyncio.sleep(0.1)
            return DependencyProbeResult(name="dep2", reachable=True, latency_ms=100)

        start = time.monotonic()
        all_ready, details = asyncio.run(
            check_readiness([slow_probe_1, slow_probe_2])
        )
        elapsed = time.monotonic() - start

        assert all_ready is True
        # If run in parallel, total time should be ~0.1s, not ~0.2s
        assert elapsed < 0.18  # generous margin for CI


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module constants."""

    def test_probe_timeout_is_3_seconds(self) -> None:
        """3 second timeout."""
        assert PROBE_TIMEOUT_SECONDS == 3.0


# ---------------------------------------------------------------------------
# Credential guard integration (Req 1.3 / 11.5 - 503 when blocked)
# ---------------------------------------------------------------------------


class TestCredentialGuardIntegration:
    """Readiness system returns 503 when credential guard blocks boot.

    When ``app.state.credential_blocked`` is True, the ``/healthz``
    endpoint must return HTTP 503 with
    ``{"status": "not_ready", "reason": "insecure_credentials"}``.
    This prevents traffic from being routed to an instance that
    refused to boot due to insecure credentials.

    The tests cover the credential guard interaction.
    """

    def test_healthz_503_when_credential_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/healthz  503 when credential_blocked is True."""
        from starlette.testclient import TestClient
        import src.main as main_module
        from src.main import app
        from src.lifecycle.credential_guard import CredentialGuardResult

        # Patch check_credentials at module level so the lifespan
        # sets credential_blocked = True during app startup.
        def _blocked_check(platform_env, env_vars, **kwargs):
            return CredentialGuardResult(
                blocked=True,
                violations=["Dev-only Postgres password"],
            )

        monkeypatch.setattr(main_module, "check_credentials", _blocked_check)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/healthz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["reason"] == "insecure_credentials"

    def test_healthz_200_when_credential_not_blocked(self) -> None:
        """Normal state: /healthz  200 when credential_blocked is False."""
        from starlette.testclient import TestClient
        from src.main import app

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"

    def test_readyz_still_probes_dependencies_when_not_blocked(self) -> None:
        """When credential guard is not blocking, /readyz performs real probes."""
        from starlette.testclient import TestClient
        from src.main import app
        from src.lifecycle import readiness as readiness_mod

        # Mock check_readiness to simulate all dependencies healthy
        async def mock_check_readiness(dependencies):
            return True, {"status": "ready"}

        with patch.object(readiness_mod, "check_readiness", mock_check_readiness):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readyz_503_when_dependency_fails_and_not_blocked(self) -> None:
        """When credential guard is not blocking but a dependency fails, /readyz  503."""
        from starlette.testclient import TestClient
        from src.main import app
        from src.lifecycle import readiness as readiness_mod

        # Mock check_readiness to simulate a failed dependency
        async def mock_check_readiness(dependencies):
            return False, {
                "status": "not_ready",
                "failed_dependencies": ["postgres"],
            }

        with patch.object(readiness_mod, "check_readiness", mock_check_readiness):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/readyz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert "postgres" in body["failed_dependencies"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_return(value):
    """Create a coroutine function that returns the given value."""

    async def _coro(*args, **kwargs):
        return value

    return _coro
