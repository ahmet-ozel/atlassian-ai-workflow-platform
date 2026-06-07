"""MinIO  Jira binary attachment upload pipeline activity.

This module provides the :func:`upload_artifact_to_jira` Temporal
activity which bridges the agent-runner-worker's MinIO artifact storage
to the Jira ``jira_add_attachment`` MCP tool. End-to-end flow:

1. Download an artifact from MinIO (``artifact_download(bucket, key)``).
2. Validate file extension and size **before** any tempfile is touched.
3. Stage the bytes to a :mod:`tempfile.NamedTemporaryFile` whose suffix
   mirrors ``Path(file_name).suffix`` - Jira's content-type sniffing
   relies on the extension and the existing
   :class:`JiraAttachmentTool` validates it again on the server side.
4. Invoke the ``jira_add_attachment`` MCP tool over JSON-RPC with
   ``{issue_key, file_path: <temp>, file_name}``. The MCP server reads
   the local file and POSTs it to Jira's
   ``/rest/api/3/issue/{key}/attachments`` multipart endpoint.
5. **Always** unlink the tempfile in the ``finally`` block - this is the
   sole cleanup point for the staged binary regardless of whether the
   download succeeded, the MCP call raised, or Jira returned a 4xx.

The activity returns a plain ``dict`` (rather than a frozen dataclass)
so the success payload from the MCP tool - which already carries
``id``, ``filename``, ``size``, ``mimeType``, ``self`` - can be
forwarded verbatim to the caller for audit logging.

MCP routing
-----------

Every outbound Atlassian HTTP call goes through the
``atlassian_mcp_bitbucket`` MCP service. The activity does not
issue a raw ``httpx`` request to Jira itself - it invokes the
``jira_add_attachment`` MCP tool which in turn calls Jira. The MCP
plumbing helpers (``make_mcp_client``, ``with_atlassian_creds``) are
shared with the rest of ``activities/jira.py``.

"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity

from http_shared import make_mcp_client, with_atlassian_creds

from . import get_credential_resolver
from .artifact import artifact_download
from .mcp_tool import MCP_ACCEPT, decode_jsonrpc_payload

__all__ = (
    "ALLOWED_EXTENSIONS",
    "MAX_FILE_SIZE_BYTES",
    "UploadArtifactToJiraInput",
    "UploadArtifactToJiraResult",
    "upload_artifact_to_jira",
)

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration / validation constants
# ---------------------------------------------------------------------------

#: File extensions accepted for upload. Mirrors
#: the Atlassian MCP gateway's ``JiraAttachmentTool.ALLOWED_EXTENSIONS``
#: with the addition of ``.html`` (the agent-runner workflow can emit
#: HTML reports that the MCP tool already accepts).
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".md", ".csv", ".txt", ".json", ".html"}
)

#: 100 MB hard cap on the in-memory payload - matches the MCP-side
#: tool limit. Validated **before** the tempfile is opened so an
#: oversized artifact never pollutes /tmp.
MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024

#: MCP server endpoints (mirrors :mod:`activities.jira`).
_DEFAULT_MCP_BASE_URL: str = "http://atlassian-mcp:8090"
_MCP_PATH: str = "/mcp"


def _mcp_base_url() -> str:
    """Resolve the MCP base URL from the environment."""

    return os.environ.get("MCP_BASE_URL", _DEFAULT_MCP_BASE_URL)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadArtifactToJiraInput:
    """Input envelope for :func:`upload_artifact_to_jira`.

    Attributes
    ----------
    issue_key:
        Target Jira issue key (e.g. ``PAY-4211``).
    bucket:
        MinIO bucket holding the artifact (typically ``ai-runs``).
    key:
        Object key within the bucket - produced by
        :func:`temporal_shared.identifiers.agent_artifact_key` or
        :func:`temporal_shared.identifiers.execution_artifact_key`.
    file_name:
        Filename to advertise on the Jira attachment. The extension
        determines content-type sniffing on Jira's side and is
        validated against :data:`ALLOWED_EXTENSIONS`.
    dept_id:
        Department identifier used to resolve Atlassian credentials
        via :func:`http_shared.with_atlassian_creds`.
    """

    issue_key: str
    bucket: str
    key: str
    file_name: str
    dept_id: str


@dataclass(frozen=True)
class UploadArtifactToJiraResult:
    """Result payload for :func:`upload_artifact_to_jira`.

    The activity returns a plain ``dict`` for forward-compatibility
    with the MCP tool's response shape; this dataclass exists for
    type-hint clarity in callers that want to ``replace(...)`` the
    fields when constructing fakes in tests.
    """

    success: bool
    attachment_id: str | None = None
    filename: str | None = None
    error_code: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_extension(file_name: str) -> str | None:
    """Return an error message if *file_name*'s extension is rejected.

    Returns ``None`` when the extension is acceptable so the caller
    can keep the early-return idiom.
    """

    suffix = Path(file_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return (
            f"unsupported_format: File extension {suffix!r} is not allowed. "
            f"Supported extensions: {sorted(ALLOWED_EXTENSIONS)}"
        )
    return None


def _validate_size(content: bytes) -> str | None:
    """Return an error message if *content* exceeds the size cap."""

    size = len(content)
    if size > MAX_FILE_SIZE_BYTES:
        max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
        actual_mb = size / (1024 * 1024)
        return (
            f"file_too_large: Artifact size ({actual_mb:.2f} MB) exceeds "
            f"maximum allowed size ({max_mb:.0f} MB). "
            f"Size: {size} bytes, Max: {MAX_FILE_SIZE_BYTES} bytes"
        )
    return None


def _build_jsonrpc_request(arguments: dict[str, Any]) -> dict[str, Any]:
    """Build the JSON-RPC envelope for ``tools/call jira_add_attachment``."""

    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "jira_add_attachment",
            "arguments": arguments,
        },
    }


def _parse_mcp_attachment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the attachment payload from the MCP JSON-RPC envelope.

    The MCP tool returns its result wrapped in the standard JSON-RPC
    ``content`` array. The text content is a JSON-encoded string
    matching the Jira REST API's attachment response shape (or the
    error envelope produced by :class:`JiraAttachmentTool`). This
    helper unwraps both layers and returns the inner ``dict`` so the
    activity body can map it to a normalised result.
    """

    import json

    if "error" in payload:
        error = payload["error"] or {}
        message = (
            error.get("message", "unknown error")
            if isinstance(error, dict)
            else str(error)
        )
        return {"success": False, "error_code": "mcp_error", "error": str(message)}

    result = payload.get("result")
    if not isinstance(result, dict):
        return {
            "success": False,
            "error_code": "mcp_error",
            "error": "empty result envelope",
        }

    # MCP responses use a ``content`` list. Try to peel off the inner
    # text payload if present; otherwise fall through to the raw dict
    # so callers can still observe structured fields the tool may
    # surface in the future.
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            if isinstance(text, str) and text:
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    # Fall through to raw text surface - the tool may
                    # have returned an unstructured success message.
                    return {
                        "success": True,
                        "raw": text,
                    }

    # Defensive fallback - unwrap whatever shape we got.
    return result


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@activity.defn(name="upload_artifact_to_jira")
async def upload_artifact_to_jira(
    issue_key: str,
    bucket: str,
    key: str,
    file_name: str,
    dept_id: str,
) -> dict[str, Any]:
    """Push an artifact from MinIO to a Jira issue as an attachment.

    Pipeline (cross-references in section docstring):

    1. Validate the file extension up-front - short-circuits the
       MinIO download for obviously rejected formats.
    2. Download the artifact bytes via the shared
       :func:`activities.artifact.artifact_download` activity.
    3. Validate the in-memory size before opening a tempfile.
    4. Stage the bytes to a :mod:`tempfile.NamedTemporaryFile` whose
       suffix mirrors ``Path(file_name).suffix``.
    5. Call the ``jira_add_attachment`` MCP tool over JSON-RPC.
    6. **Always** unlink the tempfile in the ``finally`` block.

    Parameters
    ----------
    issue_key:
        Target Jira issue key (e.g. ``PAY-4211``).
    bucket:
        MinIO bucket holding the artifact.
    key:
        Object key within the bucket.
    file_name:
        Filename to advertise on the Jira attachment.
    dept_id:
        Department identifier for Atlassian credential resolution.

    Returns
    -------
    dict
        On success ::

            {"success": True, "issue_key": ..., "filename": ...,
             "id": ..., "size": ..., "mimeType": ..., "self": ...}

        On failure ::

            {"success": False, "error_code": ..., "error": ...}
    """

    if activity.in_activity():
        activity.heartbeat(
            f"upload_artifact_to_jira issue={issue_key} bucket={bucket} key={key}"
        )

    # 1. Pre-flight: extension validation. Cheap and protects MinIO
    # bandwidth on obvious rejects (.exe, .zip, …).
    ext_error = _validate_extension(file_name)
    if ext_error:
        _LOG.warning(
            "upload_artifact_to_jira: extension rejected file_name=%s issue=%s",
            file_name,
            issue_key,
        )
        return {
            "success": False,
            "error_code": "unsupported_format",
            "error": ext_error,
        }

    # 2. Download from MinIO.
    try:
        content = await artifact_download(bucket, key)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "upload_artifact_to_jira: artifact_download failed bucket=%s key=%s: %s",
            bucket,
            key,
            exc,
        )
        return {
            "success": False,
            "error_code": "artifact_download_failed",
            "error": f"Could not download artifact {bucket}/{key}: {exc}",
        }

    # 3. Size cap - performed in-memory before tempfile is created so
    # an oversized payload never touches disk.
    size_error = _validate_size(content)
    if size_error:
        _LOG.warning(
            "upload_artifact_to_jira: size rejected key=%s size=%d",
            key,
            len(content),
        )
        return {
            "success": False,
            "error_code": "file_too_large",
            "error": size_error,
        }

    # 4-6. tempfile staging + MCP call + guaranteed cleanup.
    suffix = Path(file_name).suffix
    temp_path: str | None = None
    try:
        # ``delete=False`` so we own the lifecycle and can hand the
        # path off to the MCP tool. Cleanup is the ``finally`` block.
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix,
            prefix="jira_upload_",
        ) as tmp:
            tmp.write(content)
            temp_path = tmp.name

        return await _invoke_mcp_attachment_tool(
            dept_id=dept_id,
            issue_key=issue_key,
            file_path=temp_path,
            file_name=file_name,
        )
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError as exc:
                # Cleanup is best-effort: if the file was already
                # removed (e.g. by a concurrent /tmp sweep) we just
                # log and move on. Failing here would mask the real
                # success/failure surfaced by the activity body.
                _LOG.warning(
                    "upload_artifact_to_jira: tempfile cleanup failed path=%s: %s",
                    temp_path,
                    exc,
                )


async def _invoke_mcp_attachment_tool(
    *,
    dept_id: str,
    issue_key: str,
    file_path: str,
    file_name: str,
) -> dict[str, Any]:
    """Call the ``jira_add_attachment`` MCP tool and normalise the result.

    Extracted as a separate coroutine so unit tests can patch this
    seam to assert the (issue_key, file_path, file_name) contract
    without rebuilding the entire activity pipeline.
    """

    resolver = get_credential_resolver()
    base_url = _mcp_base_url()
    request_body = _build_jsonrpc_request(
        {
            "issue_key": issue_key,
            "file_path": file_path,
            "file_name": file_name,
        }
    )

    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=120.0,
        base_url=base_url,
        headers={"Accept": MCP_ACCEPT},
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service="jira",
            credential_resolver=resolver,
        ) as authed_client:
            response = await authed_client.post(_MCP_PATH, json=request_body)

            if response.status_code >= 400:
                detail = response.text[:500] if response.text else ""
                _LOG.warning(
                    "upload_artifact_to_jira: MCP HTTP %d issue=%s: %s",
                    response.status_code,
                    issue_key,
                    detail,
                )
                return {
                    "success": False,
                    "error_code": f"http_{response.status_code}",
                    "error": (
                        f"MCP server returned HTTP {response.status_code}: "
                        f"{detail}"
                    ),
                }

            try:
                payload = decode_jsonrpc_payload(response, "jira_add_attachment")
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": False,
                    "error_code": "mcp_error",
                    "error": f"non-JSON/SSE MCP response: {exc}",
                }

    parsed = _parse_mcp_attachment_payload(payload)

    # Normalise the result so downstream consumers see a stable shape
    # regardless of whether the MCP tool returned the Jira REST body
    # verbatim or wrapped it in its own success/failure envelope.
    if parsed.get("success") is False:
        return {
            "success": False,
            "error_code": parsed.get("error_code") or "upload_failed",
            "error": parsed.get("error") or "unknown MCP failure",
        }

    # Successful upload - preserve the upstream fields the Jira tool
    # already populates so audit logs can link to the attachment.
    result: dict[str, Any] = {
        "success": True,
        "issue_key": parsed.get("issue_key", issue_key),
        "filename": parsed.get("filename", file_name),
    }
    for optional_field in ("id", "size", "mimeType", "self"):
        if optional_field in parsed:
            result[optional_field] = parsed[optional_field]
    return result
