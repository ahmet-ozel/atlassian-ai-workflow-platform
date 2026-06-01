"""Unit tests for the artifact activity module.

Tests the ``artifact_upload``, ``artifact_download``, and ``artifact_delete``
activity functions by verifying data models, configuration helpers, and
error handling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worker src is importable
_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Ensure libs are importable
_PLATFORM_ROOT = _WORKER_ROOT.parent.parent
_LIBS_HTTP_SHARED = _PLATFORM_ROOT / "libs" / "http-shared" / "src"
_LIBS_TEMPORAL_SHARED = _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
if str(_LIBS_HTTP_SHARED) not in sys.path:
    sys.path.insert(0, str(_LIBS_HTTP_SHARED))
if str(_LIBS_TEMPORAL_SHARED) not in sys.path:
    sys.path.insert(0, str(_LIBS_TEMPORAL_SHARED))

from activities.artifact import (
    DEFAULT_BUCKET,
    ArtifactRef,
    ArtifactStorageError,
    _get_minio_config,
)


# ---------------------------------------------------------------------------
# Tests: Data models
# ---------------------------------------------------------------------------


class TestArtifactRef:
    """Tests for the ArtifactRef dataclass."""

    def test_frozen(self) -> None:
        ref = ArtifactRef(
            bucket="ai-runs",
            key="artifacts/PAY-4211/iter-1/diff.patch",
            etag="abc123",
            size_bytes=1024,
        )
        with pytest.raises(AttributeError):
            ref.bucket = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        ref = ArtifactRef(
            bucket="ai-runs",
            key="artifacts/PAY-4211/iter-1/diff.patch",
            etag="abc123",
            size_bytes=512,
        )
        assert ref.bucket == "ai-runs"
        assert ref.key == "artifacts/PAY-4211/iter-1/diff.patch"
        assert ref.etag == "abc123"
        assert ref.size_bytes == 512


class TestArtifactStorageError:
    """Tests for the ArtifactStorageError exception."""

    def test_message_includes_bucket_and_key(self) -> None:
        err = ArtifactStorageError("upload failed", "ai-runs", "test/key.txt")
        assert "ai-runs" in str(err)
        assert "test/key.txt" in str(err)
        assert "upload failed" in str(err)

    def test_attributes(self) -> None:
        err = ArtifactStorageError("msg", "bucket-x", "key-y")
        assert err.bucket == "bucket-x"
        assert err.key == "key-y"


# ---------------------------------------------------------------------------
# Tests: Configuration
# ---------------------------------------------------------------------------


class TestMinioConfig:
    """Tests for the _get_minio_config helper."""

    def test_default_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
        monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
        monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
        config = _get_minio_config()
        assert config["endpoint_url"] == "http://minio:9000"
        assert config["aws_access_key_id"] == "minio"
        assert config["aws_secret_access_key"] == "miniosecret_dev_only"

    def test_custom_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "http://custom-minio:9001")
        config = _get_minio_config()
        assert config["endpoint_url"] == "http://custom-minio:9001"

    def test_endpoint_without_scheme_gets_http(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "my-minio:9000")
        config = _get_minio_config()
        assert config["endpoint_url"] == "http://my-minio:9000"

    def test_https_endpoint_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MINIO_ENDPOINT", "https://secure-minio:9000")
        config = _get_minio_config()
        assert config["endpoint_url"] == "https://secure-minio:9000"

    def test_custom_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MINIO_ROOT_USER", "admin")
        monkeypatch.setenv("MINIO_ROOT_PASSWORD", "supersecret")
        config = _get_minio_config()
        assert config["aws_access_key_id"] == "admin"
        assert config["aws_secret_access_key"] == "supersecret"


# ---------------------------------------------------------------------------
# Tests: Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module-level constants."""

    def test_default_bucket(self) -> None:
        assert DEFAULT_BUCKET == "ai-runs"


# ---------------------------------------------------------------------------
# Tests: Integration with identifiers module
# ---------------------------------------------------------------------------


class TestArtifactKeyIntegration:
    """Tests that artifact keys from temporal_shared.identifiers work
    correctly with the artifact module's expected key format."""

    def test_agent_artifact_key_format(self) -> None:
        from temporal_shared.identifiers import agent_artifact_key

        key = agent_artifact_key("PAY-4211", 1, "diff.patch")
        assert key == "artifacts/PAY-4211/iter-1/diff.patch"
        assert key.startswith("artifacts/")

    def test_execution_artifact_key_format(self) -> None:
        from temporal_shared.identifiers import execution_artifact_key

        key = execution_artifact_key(
            "exec-agent-automation-jira-PAY-4211-iter-1-1700000000",
            "stdout.log",
        )
        assert key == (
            "executions/exec-agent-automation-jira-PAY-4211-iter-1-1700000000"
            "/stdout.log"
        )
        assert key.startswith("executions/")
