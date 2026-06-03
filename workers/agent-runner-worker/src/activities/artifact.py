"""MinIO artifact storage activities for AgentRunnerWorkflow.

This module provides Temporal activities for uploading, downloading, and
deleting artifacts in MinIO (S3-compatible object storage). Artifacts are
stored in the ``ai-runs`` bucket by default, with key prefixes generated
by :mod:`temporal_shared.identifiers` helpers:

- ``agent_artifact_key(issue_key, iteration, filename)``
  → ``artifacts/{issue_key}/iter-{N}/{filename}``
- ``execution_artifact_key(workflow_id, name)``
  → ``executions/{workflow_id}/{name}``

The ``artifact_delete`` activity is idempotent: a 404 (NoSuchKey) response
is treated as success, making it safe for saga compensation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO

from temporalio import activity

try:
    from aiobotocore.session import get_session as _get_aio_session
except ImportError:  # pragma: no cover
    _get_aio_session = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default MinIO endpoint. Overridable via MINIO_ENDPOINT env var.
_DEFAULT_MINIO_ENDPOINT: str = "minio:9000"

#: Default bucket for all AI run artifacts.
DEFAULT_BUCKET: str = "ai-runs"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to an uploaded artifact in MinIO.

    Attributes
    ----------
    bucket : str
        The S3 bucket name.
    key : str
        The full object key within the bucket.
    etag : str
        The ETag returned by MinIO after upload (content hash).
    size_bytes : int
        Size of the uploaded data in bytes.
    """

    bucket: str
    key: str
    etag: str
    size_bytes: int


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ArtifactStorageError(RuntimeError):
    """Raised when an artifact storage operation fails unexpectedly."""

    def __init__(self, message: str, bucket: str, key: str) -> None:
        super().__init__(f"Artifact error [{bucket}/{key}]: {message}")
        self.bucket = bucket
        self.key = key


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_minio_config() -> dict[str, str]:
    """Read MinIO connection configuration from environment variables."""
    endpoint = os.environ.get("MINIO_ENDPOINT", _DEFAULT_MINIO_ENDPOINT)
    # Ensure endpoint has scheme for aiobotocore
    if not endpoint.startswith(("http://", "https://")):
        endpoint = f"http://{endpoint}"

    return {
        "endpoint_url": endpoint,
        "aws_access_key_id": os.environ.get("MINIO_ROOT_USER", "minio"),
        "aws_secret_access_key": os.environ.get(
            "MINIO_ROOT_PASSWORD", "miniosecret_dev_only"
        ),
        "region_name": os.environ.get("MINIO_REGION", "us-east-1"),
    }


async def _ensure_bucket_exists(client: object, bucket: str) -> None:
    """Create the bucket if it does not already exist (idempotent)."""
    try:
        await client.head_bucket(Bucket=bucket)  # type: ignore[union-attr]
    except client.exceptions.ClientError as exc:  # type: ignore[union-attr]
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchBucket"):
            await client.create_bucket(Bucket=bucket)  # type: ignore[union-attr]
        else:
            raise


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="artifact_upload")
async def artifact_upload(
    bucket: str,
    key: str,
    data: bytes,
    content_type: str,
) -> ArtifactRef:
    """Upload an artifact to MinIO.

    Parameters
    ----------
    bucket : str
        Target S3 bucket (default: ``ai-runs``).
    key : str
        Object key, typically produced by
        :func:`temporal_shared.identifiers.agent_artifact_key` or
        :func:`temporal_shared.identifiers.execution_artifact_key`.
    data : bytes
        Raw artifact content.
    content_type : str
        MIME type of the artifact (e.g. ``"text/plain"``,
        ``"application/octet-stream"``).

    Returns
    -------
    ArtifactRef
        Reference containing bucket, key, etag, and size.

    Raises
    ------
    ArtifactStorageError
        If the upload fails for any reason other than transient network
        errors (which Temporal retries handle).
    """
    if _get_aio_session is None:
        raise ArtifactStorageError(
            "aiobotocore is not installed", bucket, key
        )

    config = _get_minio_config()
    session = _get_aio_session()

    activity.heartbeat(f"uploading artifact to {bucket}/{key}")

    try:
        async with session.create_client("s3", **config) as client:
            await _ensure_bucket_exists(client, bucket)

            response = await client.put_object(
                Bucket=bucket,
                Key=key,
                Body=BytesIO(data),
                ContentLength=len(data),
                ContentType=content_type,
            )

            etag = response.get("ETag", "").strip('"')

            return ArtifactRef(
                bucket=bucket,
                key=key,
                etag=etag,
                size_bytes=len(data),
            )
    except Exception as exc:
        if "NoSuchBucket" in str(exc) or "AccessDenied" in str(exc):
            raise ArtifactStorageError(str(exc), bucket, key) from exc
        raise ArtifactStorageError(
            f"Upload failed: {exc}", bucket, key
        ) from exc


@activity.defn(name="artifact_download")
async def artifact_download(bucket: str, key: str) -> bytes:
    """Download an artifact from MinIO.

    Parameters
    ----------
    bucket : str
        Source S3 bucket.
    key : str
        Object key to download.

    Returns
    -------
    bytes
        The raw artifact content.

    Raises
    ------
    ArtifactStorageError
        If the object does not exist or the download fails.
    """
    if _get_aio_session is None:
        raise ArtifactStorageError(
            "aiobotocore is not installed", bucket, key
        )

    config = _get_minio_config()
    session = _get_aio_session()

    activity.heartbeat(f"downloading artifact from {bucket}/{key}")

    try:
        async with session.create_client("s3", **config) as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                data = await stream.read()
            return data
    except Exception as exc:
        error_str = str(exc)
        if "NoSuchKey" in error_str or "404" in error_str:
            raise ArtifactStorageError(
                f"Object not found: {key}", bucket, key
            ) from exc
        raise ArtifactStorageError(
            f"Download failed: {exc}", bucket, key
        ) from exc


@activity.defn(name="artifact_delete")
async def artifact_delete(bucket: str, key: str) -> None:
    """Delete an artifact from MinIO (idempotent).

    This activity is designed for saga compensation. If the object does
    not exist (404 / NoSuchKey), the operation is treated as a successful
    no-op — the desired end state (object absent) is already achieved.

    Parameters
    ----------
    bucket : str
        Source S3 bucket.
    key : str
        Object key to delete.

    Raises
    ------
    ArtifactStorageError
        If the deletion fails for a reason other than "not found".
    """
    if _get_aio_session is None:
        raise ArtifactStorageError(
            "aiobotocore is not installed", bucket, key
        )

    config = _get_minio_config()
    session = _get_aio_session()

    activity.heartbeat(f"deleting artifact {bucket}/{key}")

    try:
        async with session.create_client("s3", **config) as client:
            await client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        error_str = str(exc)
        # 404 / NoSuchKey is acceptable — compensation is idempotent
        if "NoSuchKey" in error_str or "404" in error_str:
            activity.logger.info(
                "artifact_delete: object already absent %s/%s (idempotent ok)",
                bucket,
                key,
            )
            return
        raise ArtifactStorageError(
            f"Delete failed: {exc}", bucket, key
        ) from exc
