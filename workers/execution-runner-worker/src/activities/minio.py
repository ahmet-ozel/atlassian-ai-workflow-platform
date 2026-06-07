"""MinIO activity module for the execution-runner-worker.

Provides :func:`minio_upload_artifact` and :func:`minio_download_artifact`,
Temporal activities that store and retrieve execution artifacts (stdout,
stderr, exit_code) in MinIO object storage.

MinIO is accessed via its S3-compatible HTTP API using AWS Signature V4
authentication. Each activity invocation creates a fresh HTTP client
(no persistent session) per the same pattern as vault.py.

Default bucket: ``ai-runs``
Key format: ``executions/{workflow_id}/{name}`` (via
``temporal_shared.identifiers.execution_artifact_key``)

Retry policy (3x with exponential backoff) is configured by the caller
workflow via Temporal activity options - the activity itself does not
implement internal retries.

Requirements: 8.3, 8.7
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from temporalio import activity

__all__ = [
    "ArtifactRef",
    "MinIOError",
    "minio_upload_artifact",
    "minio_download_artifact",
    "DEFAULT_BUCKET",
]


#: Default bucket for execution artifacts.
DEFAULT_BUCKET: str = "ai-runs"


class MinIOError(RuntimeError):
    """Raised when a MinIO operation fails.

    Attributes
    ----------
    bucket : str
        The target bucket.
    key : str
        The object key.
    cause : str
        Human-readable description of what went wrong.
    """

    def __init__(self, bucket: str, key: str, cause: str) -> None:
        self.bucket = bucket
        self.key = key
        self.cause = cause
        super().__init__(
            f"minio operation failed: bucket={bucket}, key={key}: {cause}"
        )


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to an uploaded artifact in MinIO.

    Attributes
    ----------
    bucket : str
        The bucket where the artifact is stored.
    key : str
        The object key within the bucket.
    size_bytes : int
        The size of the uploaded data in bytes.
    etag : str
        The ETag returned by MinIO (typically MD5 hex digest).
    """

    bucket: str
    key: str
    size_bytes: int
    etag: str


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _minio_endpoint() -> str:
    """Read MinIO endpoint from environment.

    Returns the endpoint without protocol prefix. The activity prepends
    ``http://`` since dev/P0 MinIO runs without TLS.
    """
    return os.environ.get("MINIO_ENDPOINT", "minio:9000")


def _minio_access_key() -> str:
    """Read MinIO access key (root user) from environment."""
    return os.environ.get("MINIO_ROOT_USER", "")


def _minio_secret_key() -> str:
    """Read MinIO secret key (root password) from environment."""
    return os.environ.get("MINIO_ROOT_PASSWORD", "")


def _minio_use_ssl() -> bool:
    """Whether to use HTTPS for MinIO connections."""
    return os.environ.get("MINIO_USE_SSL", "false").lower() == "true"


# ---------------------------------------------------------------------------
# AWS Signature V4 helpers (minimal subset for S3-compatible MinIO)
# ---------------------------------------------------------------------------

_AWS_REGION: str = "us-east-1"
_AWS_SERVICE: str = "s3"


def _sign(key: bytes, msg: str) -> bytes:
    """HMAC-SHA256 sign a message with the given key."""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(
    secret_key: str, date_stamp: str, region: str, service: str
) -> bytes:
    """Derive the AWS Signature V4 signing key."""
    k_date = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(
        k_region, service.encode("utf-8"), hashlib.sha256
    ).digest()
    k_signing = hmac.new(
        k_service, b"aws4_request", hashlib.sha256
    ).digest()
    return k_signing


def _build_authorization_header(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    now: datetime,
) -> str:
    """Build the AWS Signature V4 Authorization header value.

    Parameters
    ----------
    method:
        HTTP method (GET, PUT, DELETE).
    path:
        URL path (e.g., ``/ai-runs/executions/wf-1/stdout.log``).
    headers:
        Headers to sign (must include ``host`` and ``x-amz-date``).
    payload_hash:
        SHA-256 hex digest of the request body.
    access_key:
        MinIO access key.
    secret_key:
        MinIO secret key.
    now:
        Current UTC time for signing.

    Returns
    -------
    str
        The full ``Authorization`` header value.
    """
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")

    # Canonical request
    signed_header_keys = sorted(headers.keys())
    canonical_headers = "".join(
        f"{k}:{headers[k]}\n" for k in signed_header_keys
    )
    signed_headers_str = ";".join(signed_header_keys)

    canonical_request = (
        f"{method}\n"
        f"{path}\n"
        f"\n"  # empty query string
        f"{canonical_headers}\n"
        f"{signed_headers_str}\n"
        f"{payload_hash}"
    )

    # String to sign
    credential_scope = f"{date_stamp}/{_AWS_REGION}/{_AWS_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    # Signing key and signature
    signing_key = _get_signature_key(
        secret_key, date_stamp, _AWS_REGION, _AWS_SERVICE
    )
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, "
        f"Signature={signature}"
    )


def _s3_headers(
    *,
    method: str,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    endpoint: str,
    payload: bytes = b"",
    content_type: str = "application/octet-stream",
) -> tuple[str, dict[str, str]]:
    """Build signed S3 request headers for MinIO.

    Returns
    -------
    tuple[str, dict[str, str]]
        The full URL and the headers dict (including Authorization).
    """
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(payload).hexdigest()

    # URL-encode the key parts (preserve slashes)
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    path = f"/{bucket}/{encoded_key}"

    host = endpoint  # e.g. "minio:9000"

    headers_to_sign: dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    if method == "PUT":
        headers_to_sign["content-type"] = content_type

    authorization = _build_authorization_header(
        method=method,
        path=path,
        headers=headers_to_sign,
        payload_hash=payload_hash,
        access_key=access_key,
        secret_key=secret_key,
        now=now,
    )

    # Build the actual request headers (include Authorization)
    request_headers: dict[str, str] = {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if method == "PUT":
        request_headers["Content-Type"] = content_type

    scheme = "https" if _minio_use_ssl() else "http"
    url = f"{scheme}://{endpoint}{path}"

    return url, request_headers


# ---------------------------------------------------------------------------
# Bucket auto-creation helper
# ---------------------------------------------------------------------------


async def _ensure_bucket_exists(
    client: httpx.AsyncClient,
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str,
) -> None:
    """Create the bucket if it does not already exist (idempotent).

    Uses a HEAD request to check existence, then PUT to create if needed.
    409 Conflict (BucketAlreadyOwnedByYou) is treated as success.
    """
    scheme = "https" if _minio_use_ssl() else "http"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(b"").hexdigest()
    path = f"/{bucket}"

    headers_to_sign: dict[str, str] = {
        "host": endpoint,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    authorization = _build_authorization_header(
        method="HEAD",
        path=path,
        headers=headers_to_sign,
        payload_hash=payload_hash,
        access_key=access_key,
        secret_key=secret_key,
        now=now,
    )

    head_headers = {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    url = f"{scheme}://{endpoint}{path}"
    resp = await client.head(url, headers=head_headers)

    if resp.status_code == 200:
        return  # Bucket exists

    # Create the bucket
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    headers_to_sign = {
        "host": endpoint,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    authorization = _build_authorization_header(
        method="PUT",
        path=path,
        headers=headers_to_sign,
        payload_hash=payload_hash,
        access_key=access_key,
        secret_key=secret_key,
        now=now,
    )

    put_headers = {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }

    resp = await client.put(url, headers=put_headers, content=b"")
    # 200 = created, 409 = already exists - both are fine
    if resp.status_code not in (200, 409):
        activity.logger.warning(
            "Failed to create bucket %s: HTTP %d", bucket, resp.status_code
        )


# ---------------------------------------------------------------------------
# Temporal activities
# ---------------------------------------------------------------------------


@activity.defn(name="minio_upload_artifact")
async def minio_upload_artifact(
    bucket: str,
    key: str,
    data: bytes,
) -> dict[str, Any]:
    """Upload an artifact to MinIO and return an ArtifactRef as a dict.

    The caller workflow configures retry policy (3x with exponential
    backoff) via Temporal activity options. This activity does not
    implement internal retries.

    Parameters
    ----------
    bucket:
        Target bucket name. Defaults to ``ai-runs`` when called by
        ExecutionRunWorkflow.
    key:
        Object key within the bucket. Typically produced by
        ``execution_artifact_key(workflow_id, name)``.
    data:
        Raw bytes to upload.

    Returns
    -------
    dict
        Serializable representation of :class:`ArtifactRef` with keys:
        ``bucket``, ``key``, ``size_bytes``, ``etag``.

    Raises
    ------
    MinIOError
        If the upload fails after the HTTP request completes with a
        non-success status code or a transport error occurs.
    """
    activity.logger.info(
        "Uploading artifact to MinIO: bucket=%s, key=%s, size=%d bytes",
        bucket,
        key,
        len(data),
    )

    endpoint = _minio_endpoint()
    access_key = _minio_access_key()
    secret_key = _minio_secret_key()

    if not access_key or not secret_key:
        raise MinIOError(
            bucket=bucket,
            key=key,
            cause="MINIO_ROOT_USER or MINIO_ROOT_PASSWORD not configured",
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Ensure bucket exists (idempotent)
        await _ensure_bucket_exists(client, endpoint, bucket, access_key, secret_key)

        # Upload the object
        url, headers = _s3_headers(
            method="PUT",
            bucket=bucket,
            key=key,
            access_key=access_key,
            secret_key=secret_key,
            endpoint=endpoint,
            payload=data,
        )

        try:
            response = await client.put(url, headers=headers, content=data)
        except httpx.HTTPError as exc:
            raise MinIOError(
                bucket=bucket,
                key=key,
                cause=f"transport error: {exc.__class__.__name__}: {exc}",
            ) from exc

        if not (200 <= response.status_code < 300):
            raise MinIOError(
                bucket=bucket,
                key=key,
                cause=f"upload failed with HTTP {response.status_code}: "
                f"{response.text[:200]}",
            )

    etag = response.headers.get("ETag", "").strip('"')

    ref = ArtifactRef(
        bucket=bucket,
        key=key,
        size_bytes=len(data),
        etag=etag,
    )

    activity.logger.info(
        "Upload complete: bucket=%s, key=%s, etag=%s",
        bucket,
        key,
        etag,
    )

    # Return as dict for Temporal serialization
    return {
        "bucket": ref.bucket,
        "key": ref.key,
        "size_bytes": ref.size_bytes,
        "etag": ref.etag,
    }


@activity.defn(name="minio_download_artifact")
async def minio_download_artifact(bucket: str, key: str) -> bytes:
    """Download an artifact from MinIO.

    Parameters
    ----------
    bucket:
        Source bucket name.
    key:
        Object key within the bucket.

    Returns
    -------
    bytes
        The raw artifact content.

    Raises
    ------
    MinIOError
        If the download fails (object not found, transport error, etc.).
    """
    activity.logger.info(
        "Downloading artifact from MinIO: bucket=%s, key=%s", bucket, key
    )

    endpoint = _minio_endpoint()
    access_key = _minio_access_key()
    secret_key = _minio_secret_key()

    if not access_key or not secret_key:
        raise MinIOError(
            bucket=bucket,
            key=key,
            cause="MINIO_ROOT_USER or MINIO_ROOT_PASSWORD not configured",
        )

    async with httpx.AsyncClient(timeout=60.0) as client:
        url, headers = _s3_headers(
            method="GET",
            bucket=bucket,
            key=key,
            access_key=access_key,
            secret_key=secret_key,
            endpoint=endpoint,
        )

        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise MinIOError(
                bucket=bucket,
                key=key,
                cause=f"transport error: {exc.__class__.__name__}: {exc}",
            ) from exc

        if response.status_code == 404:
            raise MinIOError(
                bucket=bucket,
                key=key,
                cause="object not found (HTTP 404)",
            )

        if not (200 <= response.status_code < 300):
            raise MinIOError(
                bucket=bucket,
                key=key,
                cause=f"download failed with HTTP {response.status_code}: "
                f"{response.text[:200]}",
            )

    activity.logger.info(
        "Download complete: bucket=%s, key=%s, size=%d bytes",
        bucket,
        key,
        len(response.content),
    )

    return response.content
