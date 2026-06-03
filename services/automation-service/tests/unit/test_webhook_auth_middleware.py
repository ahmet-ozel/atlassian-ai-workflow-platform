"""Unit tests for ``src.middleware.webhook_auth``.

Covers the per-department webhook HMAC authentication middleware:

* Department hint extraction: header priority over URL path.
* HMAC-SHA256 timing-safe verification.
* Global fallback when department is unknown.
* 503 on Vault timeout.
* Per-department Vault path construction.
* 401 + security log on HMAC failure.
* 401 when global fallback undefined and dept unknown.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI, Request

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SERVICE_ROOT))

from middleware.webhook_auth import (  # noqa: E402
    DepartmentContext,
    VaultSecretReader,
    WebhookAuthMiddleware,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_signature(secret: str, body: bytes) -> str:
    """Compute a valid HMAC-SHA256 signature for testing."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Fake Vault reader
# ---------------------------------------------------------------------------


class FakeVaultReader:
    """In-memory Vault reader for tests.

    Attributes:
        secrets: Mapping of path → payload dict.
        timeout_paths: Set of paths that simulate a timeout.
        error_paths: Set of paths that raise a generic exception.
        read_calls: List of paths that were read (for assertions).
    """

    def __init__(
        self,
        secrets: dict[str, dict[str, str]] | None = None,
        timeout_paths: set[str] | None = None,
        error_paths: set[str] | None = None,
    ) -> None:
        self.secrets: dict[str, dict[str, str]] = secrets or {}
        self.timeout_paths: set[str] = timeout_paths or set()
        self.error_paths: set[str] = error_paths or set()
        self.read_calls: list[str] = []

    async def read_secret(self, path: str) -> dict[str, str]:
        self.read_calls.append(path)
        if path in self.timeout_paths:
            import asyncio
            raise asyncio.TimeoutError(f"timeout reading {path}")
        if path in self.error_paths:
            raise RuntimeError(f"vault error for {path}")
        if path not in self.secrets:
            raise KeyError(f"no secret at {path}")
        return self.secrets[path]


# ---------------------------------------------------------------------------
# App factory for tests
# ---------------------------------------------------------------------------


def _create_test_app(
    vault_reader: VaultSecretReader,
    global_fallback_secret: str | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with the webhook auth middleware."""
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/webhooks/jira")
    async def webhook_jira(request: Request):
        ctx = getattr(request.state, "dept_context", None)
        return {
            "received": True,
            "dept_id": ctx.dept_id if ctx else None,
            "source": ctx.source if ctx else None,
        }

    @app.post("/webhooks/jira/PROJ-123/event")
    async def webhook_jira_project(request: Request):
        ctx = getattr(request.state, "dept_context", None)
        return {
            "received": True,
            "dept_id": ctx.dept_id if ctx else None,
            "source": ctx.source if ctx else None,
        }

    app.add_middleware(
        WebhookAuthMiddleware,
        vault_reader=vault_reader,
        global_fallback_secret=global_fallback_secret,
    )

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_bypasses_auth():
    """Health probe paths skip authentication entirely."""
    reader = FakeVaultReader()
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert reader.read_calls == []


@pytest.mark.asyncio
async def test_missing_signature_returns_401():
    """Request without any signature header gets 401."""
    reader = FakeVaultReader()
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/webhooks/jira", content=b"payload")

    assert resp.status_code == 401
    assert resp.json()["error"] == "missing_signature"


@pytest.mark.asyncio
async def test_dept_header_priority_over_url_path():
    """X-Department-Key header takes priority over URL path hint."""
    dept_secret = "header-dept-secret"
    body = b'{"event":"issue_created"}'
    sig = _compute_signature(dept_secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/header-dept/secret": {"hmac_secret": dept_secret},
            "secret/webhook/proj/secret": {"hmac_secret": "url-dept-secret"},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira/PROJ-123/event",
            content=body,
            headers={
                "X-Department-Key": "header-dept",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["dept_id"] == "header-dept"
    assert data["source"] == "header"


@pytest.mark.asyncio
async def test_url_path_dept_extraction():
    """Department extracted from URL path when header is absent."""
    dept_secret = "proj-secret"
    body = b'{"event":"push"}'
    sig = _compute_signature(dept_secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/proj/secret": {"hmac_secret": dept_secret},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira/PROJ-123/event",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["dept_id"] == "proj"
    assert data["source"] == "url_path"


@pytest.mark.asyncio
async def test_valid_hmac_passes():
    """Valid HMAC-SHA256 signature passes authentication."""
    secret = "my-webhook-secret"
    body = b'{"action":"created"}'
    sig = _compute_signature(secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/payments/secret": {"hmac_secret": secret},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Department-Key": "payments",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    assert resp.json()["dept_id"] == "payments"


@pytest.mark.asyncio
async def test_invalid_hmac_returns_401():
    """Invalid HMAC signature returns 401."""
    reader = FakeVaultReader(
        secrets={
            "secret/webhook/payments/secret": {"hmac_secret": "real-secret"},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=b"payload",
            headers={
                "X-Department-Key": "payments",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_vault_timeout_returns_503():
    """Vault timeout returns 503."""
    reader = FakeVaultReader(
        timeout_paths={"secret/webhook/payments/secret"}
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=b"payload",
            headers={
                "X-Department-Key": "payments",
                "X-Hub-Signature-256": "sha256=abc123",
            },
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "vault_unavailable"


@pytest.mark.asyncio
async def test_global_fallback_when_dept_unknown():
    """Global fallback secret used when dept cannot be determined."""
    fallback_secret = "global-secret"
    body = b'{"event":"test"}'
    sig = _compute_signature(fallback_secret, body)

    reader = FakeVaultReader()
    app = _create_test_app(reader, global_fallback_secret=fallback_secret)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["dept_id"] is None
    assert data["source"] == "global_fallback"


@pytest.mark.asyncio
async def test_global_fallback_from_vault():
    """Global fallback read from Vault when not pre-loaded."""
    fallback_secret = "vault-global-secret"
    body = b'{"event":"test"}'
    sig = _compute_signature(fallback_secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/global/secret": {"hmac_secret": fallback_secret},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature-256": sig},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["dept_id"] is None
    assert data["source"] == "global_fallback"


@pytest.mark.asyncio
async def test_no_fallback_and_no_dept_returns_401():
    """401 when global fallback undefined and dept unknown."""
    reader = FakeVaultReader()  # No secrets at all
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=b"payload",
            headers={"X-Hub-Signature-256": "sha256=abc123"},
        )

    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_vault_path_construction():
    """Vault path follows secret/webhook/{dept_id}/secret pattern."""
    reader = FakeVaultReader(
        secrets={
            "secret/webhook/engineering/secret": {"hmac_secret": "eng-secret"},
        }
    )
    app = _create_test_app(reader)
    body = b"test"
    sig = _compute_signature("eng-secret", body)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Department-Key": "engineering",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    # Verify the correct path was queried
    assert "secret/webhook/engineering/secret" in reader.read_calls


@pytest.mark.asyncio
async def test_dept_secret_not_found_falls_back_to_global():
    """When dept secret not in Vault, falls back to global."""
    fallback_secret = "global-fallback"
    body = b'{"data":"test"}'
    sig = _compute_signature(fallback_secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/global/secret": {"hmac_secret": fallback_secret},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Department-Key": "unknown-dept",
                "X-Hub-Signature-256": sig,
            },
        )

    assert resp.status_code == 200
    # The dept secret path was attempted first
    assert "secret/webhook/unknown-dept/secret" in reader.read_calls
    # Then the global fallback
    assert "secret/webhook/global/secret" in reader.read_calls


@pytest.mark.asyncio
async def test_x_webhook_signature_header_supported():
    """X-Webhook-Signature header is also accepted."""
    secret = "alt-secret"
    body = b"alt-body"
    sig = _compute_signature(secret, body)

    reader = FakeVaultReader(
        secrets={
            "secret/webhook/dept-a/secret": {"hmac_secret": secret},
        }
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Department-Key": "dept-a",
                "X-Webhook-Signature": sig,
            },
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_vault_generic_error_returns_503():
    """Generic Vault errors return 503."""
    reader = FakeVaultReader(
        error_paths={"secret/webhook/broken/secret"}
    )
    app = _create_test_app(reader)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/webhooks/jira",
            content=b"payload",
            headers={
                "X-Department-Key": "broken",
                "X-Hub-Signature-256": "sha256=abc",
            },
        )

    assert resp.status_code == 503
    assert resp.json()["error"] == "vault_unavailable"
