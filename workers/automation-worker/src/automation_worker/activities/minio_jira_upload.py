"""Upload MinIO execution artifacts to Jira.

The output-action path receives SSH runner artifacts as ``bucket``/``key``
pairs. This helper downloads the object from MinIO and uploads it to Jira
using the department's Jira bot credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

_AWS_REGION = "us-east-1"
_AWS_SERVICE = "s3"


class ArtifactUploadError(RuntimeError):
    """Raised when a MinIO-to-Jira artifact upload fails."""


def _cred_value(cred: Any, *names: str) -> str | None:
    for name in names:
        if isinstance(cred, Mapping):
            value = cred.get(name)
        else:
            value = getattr(cred, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _minio_endpoint() -> tuple[str, str]:
    raw = os.environ.get("MINIO_ENDPOINT", "minio:9000").strip()
    use_ssl = os.environ.get("MINIO_USE_SSL", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if raw.startswith("http://"):
        return "http", raw[len("http://") :].strip("/")
    if raw.startswith("https://"):
        return "https", raw[len("https://") :].strip("/")
    return ("https" if use_ssl else "http"), raw.strip("/")


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret_key: str, date_stamp: str) -> bytes:
    k_date = hmac.new(
        f"AWS4{secret_key}".encode("utf-8"),
        date_stamp.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    k_region = _sign(k_date, _AWS_REGION)
    k_service = _sign(k_region, _AWS_SERVICE)
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def _s3_get_headers(
    *,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    endpoint: str,
) -> tuple[str, dict[str, str]]:
    now = datetime.now(timezone.utc)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    payload_hash = hashlib.sha256(b"").hexdigest()
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    path = f"/{bucket}/{encoded_key}"
    headers_to_sign = {
        "host": endpoint,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    signed_keys = sorted(headers_to_sign)
    canonical_headers = "".join(
        f"{name}:{headers_to_sign[name]}\n" for name in signed_keys
    )
    signed_headers = ";".join(signed_keys)
    canonical_request = (
        "GET\n"
        f"{path}\n\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    credential_scope = (
        f"{date_stamp}/{_AWS_REGION}/{_AWS_SERVICE}/aws4_request"
    )
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signature = hmac.new(
        _signature_key(secret_key, date_stamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    scheme, _ = _minio_endpoint()
    return (
        f"{scheme}://{endpoint}{path}",
        {
            "Authorization": authorization,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        },
    )


async def _download_minio_object(
    *,
    bucket: str,
    key: str,
    timeout: float,
) -> bytes:
    access_key = os.environ.get("MINIO_ROOT_USER", "")
    secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "")
    if not access_key or not secret_key:
        raise ArtifactUploadError(
            "MINIO_ROOT_USER or MINIO_ROOT_PASSWORD is not configured"
        )
    _, endpoint = _minio_endpoint()
    url, headers = _s3_get_headers(
        bucket=bucket,
        key=key,
        access_key=access_key,
        secret_key=secret_key,
        endpoint=endpoint,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers)
    if response.status_code == 404:
        raise ArtifactUploadError(f"MinIO object not found: {bucket}/{key}")
    if response.status_code >= 400:
        raise ArtifactUploadError(
            f"MinIO download failed with HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    return response.content


async def upload_minio_artifact_to_jira(
    params: dict[str, Any],
    *,
    dept_id: str,
    credential_resolver: Any,
    timeout: float,
) -> dict[str, Any]:
    issue_key = str(params.get("issue_key") or "").strip()
    bucket = str(params.get("bucket") or "").strip()
    key = str(params.get("key") or "").strip()
    file_name = str(params.get("file_name") or os.path.basename(key)).strip()
    if not issue_key or not bucket or not key or not file_name:
        raise ArtifactUploadError(
            "issue_key, bucket, key and file_name are required"
        )

    data = await _download_minio_object(
        bucket=bucket,
        key=key,
        timeout=max(timeout, 60.0),
    )
    cred = await credential_resolver.get(dept_id, "jira", scope="org")
    jira_url = _cred_value(cred, "url", "base_url")
    username = _cred_value(cred, "username", "email", "user")
    token = _cred_value(cred, "api_token", "personal_token", "pat", "token")
    if not jira_url or not username or not token:
        raise ArtifactUploadError("Jira credential is incomplete")

    upload_url = (
        f"{jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/attachments"
    )
    files = {"file": (file_name, data, "application/octet-stream")}
    async with httpx.AsyncClient(
        auth=(username, token),
        timeout=max(timeout, 120.0),
    ) as client:
        response = await client.post(
            upload_url,
            headers={"X-Atlassian-Token": "no-check"},
            files=files,
        )
    if response.status_code >= 400:
        raise ArtifactUploadError(
            f"Jira attachment upload failed with HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text
    return {
        "success": True,
        "issue_key": issue_key,
        "filename": file_name,
        "size_bytes": len(data),
        "jira_response": payload,
    }
