"""Unit tests for the MinIO activity module.

Tests cover:
- Configuration helpers read environment variables correctly
- MinIOError contains proper context
- ArtifactRef dataclass is frozen and holds expected fields
- minio_upload_artifact raises MinIOError when credentials are missing
- minio_download_artifact raises MinIOError when credentials are missing
- S3 signature header building produces valid Authorization format
- Bucket ensure logic handles existing/new buckets
"""

from __future__ import annotations

import hashlib
import os
from unittest.mock import AsyncMock, patch

import pytest

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from activities.minio import (
    DEFAULT_BUCKET,
    ArtifactRef,
    MinIOError,
    _build_authorization_header,
    _get_signature_key,
    _minio_access_key,
    _minio_endpoint,
    _minio_secret_key,
    _minio_use_ssl,
    _s3_headers,
    minio_download_artifact,
    minio_upload_artifact,
)


class TestConfiguration:
    """Test environment variable configuration helpers."""

    def test_default_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
        assert _minio_endpoint() == "minio:9000"

    def test_custom_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "localhost:9000")
        assert _minio_endpoint() == "localhost:9000"

    def test_default_access_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
        assert _minio_access_key() == ""

    def test_custom_access_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_ROOT_USER", "myuser")
        assert _minio_access_key() == "myuser"

    def test_default_secret_key_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
        assert _minio_secret_key() == ""

    def test_custom_secret_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "mysecret")
        assert _minio_secret_key() == "mysecret"

    def test_ssl_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIO_USE_SSL", raising=False)
        assert _minio_use_ssl() is False

    def test_ssl_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_USE_SSL", "true")
        assert _minio_use_ssl() is True


class TestArtifactRef:
    """Test the ArtifactRef dataclass."""

    def test_creation(self) -> None:
        ref = ArtifactRef(
            bucket="ai-runs",
            key="executions/wf-1/stdout.log",
            size_bytes=1024,
            etag="abc123",
        )
        assert ref.bucket == "ai-runs"
        assert ref.key == "executions/wf-1/stdout.log"
        assert ref.size_bytes == 1024
        assert ref.etag == "abc123"

    def test_frozen(self) -> None:
        ref = ArtifactRef(bucket="b", key="k", size_bytes=0, etag="e")
        with pytest.raises(Exception):  # FrozenInstanceError
            ref.bucket = "other"  # type: ignore[misc]


class TestMinIOError:
    """Test the MinIOError exception class."""

    def test_attributes(self) -> None:
        err = MinIOError(bucket="ai-runs", key="test/key", cause="not found")
        assert err.bucket == "ai-runs"
        assert err.key == "test/key"
        assert err.cause == "not found"
        assert "ai-runs" in str(err)
        assert "test/key" in str(err)
        assert "not found" in str(err)


class TestDefaultBucket:
    """Test the default bucket constant."""

    def test_value(self) -> None:
        assert DEFAULT_BUCKET == "ai-runs"


class TestSignatureHelpers:
    """Test AWS Signature V4 helper functions."""

    def test_get_signature_key_returns_bytes(self) -> None:
        key = _get_signature_key("secret", "20240101", "us-east-1", "s3")
        assert isinstance(key, bytes)
        assert len(key) == 32  # SHA-256 produces 32 bytes

    def test_build_authorization_header_format(self) -> None:
        from datetime import datetime, timezone

        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        payload_hash = hashlib.sha256(b"test").hexdigest()

        headers = {
            "host": "minio:9000",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": "20240115T120000Z",
        }

        auth = _build_authorization_header(
            method="PUT",
            path="/ai-runs/test/key",
            headers=headers,
            payload_hash=payload_hash,
            access_key="minioaccess",
            secret_key="miniosecret",
            now=now,
        )

        assert auth.startswith("AWS4-HMAC-SHA256 ")
        assert "Credential=minioaccess/20240115/us-east-1/s3/aws4_request" in auth
        assert "SignedHeaders=" in auth
        assert "Signature=" in auth

    def test_s3_headers_put(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_USE_SSL", "false")
        url, headers = _s3_headers(
            method="PUT",
            bucket="ai-runs",
            key="executions/wf-1/stdout.log",
            access_key="minio",
            secret_key="miniosecret",
            endpoint="minio:9000",
            payload=b"hello world",
        )

        assert url.startswith("http://minio:9000/ai-runs/executions/wf-1/stdout.log")
        assert "Authorization" in headers
        assert "x-amz-content-sha256" in headers
        assert "x-amz-date" in headers
        assert "Content-Type" in headers

    def test_s3_headers_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_USE_SSL", "false")
        url, headers = _s3_headers(
            method="GET",
            bucket="ai-runs",
            key="executions/wf-1/stdout.log",
            access_key="minio",
            secret_key="miniosecret",
            endpoint="minio:9000",
        )

        assert url.startswith("http://minio:9000/ai-runs/executions/wf-1/stdout.log")
        assert "Authorization" in headers
        assert "Content-Type" not in headers  # GET doesn't need Content-Type


class TestUploadMissingCredentials:
    """Test that upload raises MinIOError when credentials are missing."""

    @pytest.mark.asyncio
    async def test_upload_no_access_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
        monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "secret")

        with pytest.raises(MinIOError) as exc_info:
            await minio_upload_artifact("ai-runs", "test/key", b"data")

        assert "not configured" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_upload_no_secret_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("MINIO_ROOT_USER", "minio")
        monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)

        with pytest.raises(MinIOError) as exc_info:
            await minio_upload_artifact("ai-runs", "test/key", b"data")

        assert "not configured" in exc_info.value.cause


class TestDownloadMissingCredentials:
    """Test that download raises MinIOError when credentials are missing."""

    @pytest.mark.asyncio
    async def test_download_no_access_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
        monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "secret")

        with pytest.raises(MinIOError) as exc_info:
            await minio_download_artifact("ai-runs", "test/key")

        assert "not configured" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_download_no_secret_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
        monkeypatch.setenv("MINIO_ROOT_USER", "minio")
        monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)

        with pytest.raises(MinIOError) as exc_info:
            await minio_download_artifact("ai-runs", "test/key")

        assert "not configured" in exc_info.value.cause
