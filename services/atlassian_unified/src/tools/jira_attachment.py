"""Jira Attachment Upload MCP Tool.

Provides the `jira_add_attachment` tool for uploading files to Jira issues
via the REST API. Implements file extension validation, size limits,
retry logic, and proper error handling.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("mcp-jira-attachment")


class JiraAttachmentTool:
    """MCP tool: jira_add_attachment

    Uploads a file to a Jira issue via the REST API
    `/rest/api/3/issue/{issueKey}/attachments` endpoint.

    Parameters:
        issue_key: Jira issue key (e.g., 'PROJ-123')
        file_path: Path to the file to upload
        file_name: Optional override for the uploaded file name.
                   If not provided, derived from file_path.

    Validations:
        - File extension must be in ALLOWED_EXTENSIONS
        - File size must not exceed MAX_FILE_SIZE_BYTES (100 MB)
        - Issue must be accessible (exists and user has write permission)

    Retry Policy:
        - Connection errors: 3 attempts, 2s interval
        - Upload timeout: 120 seconds
    """

    ALLOWED_EXTENSIONS: set[str] = {".pdf", ".md", ".csv", ".txt", ".json"}
    MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB
    UPLOAD_TIMEOUT_SECONDS: int = 120
    MAX_RETRIES: int = 3
    RETRY_INTERVAL_SECONDS: float = 2.0

    def __init__(
        self,
        jira_base_url: str,
        jira_email: str,
        jira_api_token: str,
    ) -> None:
        """Initialize the Jira Attachment tool.

        Args:
            jira_base_url: Base URL of the Jira instance (e.g., https://myorg.atlassian.net)
            jira_email: Email for Jira API authentication
            jira_api_token: API token for Jira authentication
        """
        self.jira_base_url = jira_base_url.rstrip("/")
        self.jira_email = jira_email
        self.jira_api_token = jira_api_token

    @property
    def tool_name(self) -> str:
        """Return the MCP tool name."""
        return "jira_add_attachment"

    @property
    def tool_description(self) -> str:
        """Return the MCP tool description."""
        return (
            "Upload a file attachment to a Jira issue. "
            "Supports PDF, MD, CSV, TXT, and JSON files up to 100 MB."
        )

    def _validate_extension(self, file_name: str) -> str | None:
        """Validate file extension against allowed list.

        Args:
            file_name: Name of the file to validate.

        Returns:
            None if valid, error message string if invalid.
        """
        ext = Path(file_name).suffix.lower()
        if ext not in self.ALLOWED_EXTENSIONS:
            return (
                f"unsupported_format: File extension '{ext}' is not allowed. "
                f"Supported extensions: {sorted(self.ALLOWED_EXTENSIONS)}"
            )
        return None

    def _validate_file_size(self, file_path: str) -> str | None:
        """Validate file size against maximum limit.

        Args:
            file_path: Path to the file to check.

        Returns:
            None if valid, error message string if file is too large.
        """
        try:
            file_size = os.path.getsize(file_path)
        except OSError as e:
            return f"file_not_found: Cannot access file at '{file_path}': {e}"

        if file_size > self.MAX_FILE_SIZE_BYTES:
            max_mb = self.MAX_FILE_SIZE_BYTES / (1024 * 1024)
            actual_mb = file_size / (1024 * 1024)
            return (
                f"file_too_large: File size ({actual_mb:.2f} MB) exceeds "
                f"maximum allowed size ({max_mb:.0f} MB). "
                f"File: {file_path}, Size: {file_size} bytes, "
                f"Max: {self.MAX_FILE_SIZE_BYTES} bytes"
            )
        return None

    async def _check_issue_accessible(
        self, client: httpx.AsyncClient, issue_key: str
    ) -> str | None:
        """Check if the issue exists and is accessible.

        Args:
            client: HTTP client instance.
            issue_key: Jira issue key to check.

        Returns:
            None if accessible, error message string if not.
        """
        url = f"{self.jira_base_url}/rest/api/3/issue/{issue_key}"
        try:
            response = await client.get(
                url,
                params={"fields": "summary"},
                timeout=30.0,
            )
            if response.status_code == 404:
                return (
                    f"issue_not_accessible: Issue '{issue_key}' does not exist "
                    f"or is not accessible."
                )
            if response.status_code == 403:
                return (
                    f"issue_not_accessible: No write permission for issue "
                    f"'{issue_key}'."
                )
            if response.status_code == 401:
                return (
                    f"issue_not_accessible: Authentication failed when accessing "
                    f"issue '{issue_key}'."
                )
            response.raise_for_status()
        except httpx.ConnectError:
            return (
                "jira_connection_error: Cannot connect to Jira API. "
                "Please check the Jira URL and network connectivity."
            )
        except httpx.TimeoutException:
            return (
                "jira_connection_error: Timeout while checking issue accessibility."
            )
        except httpx.HTTPStatusError as e:
            return (
                f"issue_not_accessible: Cannot access issue '{issue_key}'. "
                f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            )
        return None

    async def _upload_with_retry(
        self,
        client: httpx.AsyncClient,
        issue_key: str,
        file_path: str,
        file_name: str,
    ) -> dict[str, Any]:
        """Upload file to Jira with retry logic for connection errors.

        Args:
            client: HTTP client instance.
            issue_key: Target Jira issue key.
            file_path: Path to the file to upload.
            file_name: Name for the uploaded file.

        Returns:
            Dict with upload result (success/error info).
        """
        url = f"{self.jira_base_url}/rest/api/3/issue/{issue_key}/attachments"
        last_error: str | None = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                with open(file_path, "rb") as f:
                    files = {"file": (file_name, f, "application/octet-stream")}
                    response = await client.post(
                        url,
                        files=files,
                        headers={"X-Atlassian-Token": "no-check"},
                        timeout=self.UPLOAD_TIMEOUT_SECONDS,
                    )

                if response.status_code == 404:
                    return {
                        "success": False,
                        "error_code": "issue_not_accessible",
                        "error": (
                            f"Issue '{issue_key}' not found or not accessible "
                            f"during upload."
                        ),
                    }
                if response.status_code == 403:
                    return {
                        "success": False,
                        "error_code": "issue_not_accessible",
                        "error": (
                            f"No permission to add attachments to issue "
                            f"'{issue_key}'."
                        ),
                    }

                response.raise_for_status()

                # Parse response
                result_data = response.json()
                attachment_info = (
                    result_data[0] if isinstance(result_data, list) else result_data
                )

                return {
                    "success": True,
                    "issue_key": issue_key,
                    "filename": file_name,
                    "id": attachment_info.get("id"),
                    "size": attachment_info.get("size"),
                    "mimeType": attachment_info.get("mimeType"),
                    "self": attachment_info.get("self"),
                }

            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = f"jira_connection_error: Connection failed: {e}"
                logger.warning(
                    f"Upload attempt {attempt}/{self.MAX_RETRIES} failed "
                    f"(connection error): {e}"
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_INTERVAL_SECONDS)

            except httpx.TimeoutException as e:
                last_error = (
                    f"jira_connection_error: Upload timed out after "
                    f"{self.UPLOAD_TIMEOUT_SECONDS}s: {e}"
                )
                logger.warning(
                    f"Upload attempt {attempt}/{self.MAX_RETRIES} failed "
                    f"(timeout): {e}"
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_INTERVAL_SECONDS)

            except httpx.HTTPStatusError as e:
                # Non-retryable HTTP errors
                return {
                    "success": False,
                    "error_code": "upload_failed",
                    "error": (
                        f"Upload failed with HTTP {e.response.status_code}: "
                        f"{e.response.text[:300]}"
                    ),
                }

        # All retries exhausted
        return {
            "success": False,
            "error_code": "jira_connection_error",
            "error": (
                f"Upload failed after {self.MAX_RETRIES} attempts. "
                f"Last error: {last_error}"
            ),
        }

    async def execute(
        self,
        issue_key: str,
        file_path: str,
        file_name: str | None = None,
    ) -> dict[str, Any]:
        """Execute the jira_add_attachment tool.

        Uploads a file to the specified Jira issue after validating
        file extension and size constraints.

        Args:
            issue_key: Jira issue key (e.g., 'PROJ-123'). Required.
            file_path: Path to the file to upload. Required.
            file_name: Optional name for the uploaded file.
                       If not provided, derived from file_path basename.

        Returns:
            Dict with result:
                - On success: {"success": True, "issue_key": ..., "filename": ..., ...}
                - On error: {"success": False, "error_code": ..., "error": ...}
        """
        # Derive file_name if not provided
        if not file_name:
            file_name = os.path.basename(file_path)

        # Validate file extension
        ext_error = self._validate_extension(file_name)
        if ext_error:
            logger.warning(f"Extension validation failed: {ext_error}")
            return {
                "success": False,
                "error_code": "unsupported_format",
                "error": ext_error,
            }

        # Validate file size (before any network transfer)
        size_error = self._validate_file_size(file_path)
        if size_error:
            logger.warning(f"Size validation failed: {size_error}")
            return {
                "success": False,
                "error_code": "file_too_large",
                "error": size_error,
            }

        # Create HTTP client with basic auth
        auth = httpx.BasicAuth(
            username=self.jira_email,
            password=self.jira_api_token,
        )

        async with httpx.AsyncClient(auth=auth) as client:
            # Check issue accessibility
            access_error = await self._check_issue_accessible(client, issue_key)
            if access_error:
                logger.warning(f"Issue access check failed: {access_error}")
                error_code = "issue_not_accessible"
                if "connection_error" in access_error:
                    error_code = "jira_connection_error"
                return {
                    "success": False,
                    "error_code": error_code,
                    "error": access_error,
                }

            # Upload with retry
            result = await self._upload_with_retry(
                client, issue_key, file_path, file_name
            )

        return result

    async def execute_from_temp(
        self,
        issue_key: str,
        content: bytes,
        file_name: str,
    ) -> dict[str, Any]:
        """Upload content from memory via a temporary file.

        Creates a temporary file, writes content, uploads to Jira,
        and ensures cleanup regardless of success or failure.
        This satisfies Requirement 4.4 (temporary file cleanup guarantee).

        Args:
            issue_key: Jira issue key.
            content: File content as bytes.
            file_name: Name for the uploaded file.

        Returns:
            Dict with upload result.
        """
        temp_path: str | None = None
        try:
            # Create temp file with proper extension
            suffix = Path(file_name).suffix
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, prefix="jira_upload_"
            ) as tmp:
                tmp.write(content)
                temp_path = tmp.name

            # Execute upload using the temp file
            return await self.execute(
                issue_key=issue_key,
                file_path=temp_path,
                file_name=file_name,
            )
        finally:
            # Guarantee cleanup of temporary file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    logger.debug(f"Cleaned up temporary file: {temp_path}")
                except OSError as e:
                    logger.warning(
                        f"Failed to clean up temporary file {temp_path}: {e}"
                    )


# --- MCP Tool Registry Integration ---


def register_jira_attachment_tool(
    registry: dict[str, Any],
    jira_base_url: str,
    jira_email: str,
    jira_api_token: str,
) -> JiraAttachmentTool:
    """Register the jira_add_attachment tool in the MCP server tool registry.

    Args:
        registry: The MCP server tool registry dict.
        jira_base_url: Jira instance base URL.
        jira_email: Jira authentication email.
        jira_api_token: Jira API token.

    Returns:
        The configured JiraAttachmentTool instance.
    """
    tool = JiraAttachmentTool(
        jira_base_url=jira_base_url,
        jira_email=jira_email,
        jira_api_token=jira_api_token,
    )

    registry[tool.tool_name] = {
        "name": tool.tool_name,
        "description": tool.tool_description,
        "parameters": {
            "issue_key": {
                "type": "string",
                "description": "Jira issue key (e.g., 'PROJ-123')",
                "required": True,
            },
            "file_path": {
                "type": "string",
                "description": "Path to the file to upload",
                "required": True,
            },
            "file_name": {
                "type": "string",
                "description": (
                    "Optional override for the uploaded file name. "
                    "If not provided, derived from file_path."
                ),
                "required": False,
            },
        },
        "handler": tool.execute,
        "instance": tool,
    }

    logger.info(f"Registered MCP tool: {tool.tool_name}")
    return tool
