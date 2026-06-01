"""OpenCode sidecar activity for code generation.

This module provides the ``opencode_generate_code`` Temporal activity that
communicates with the OpenCode sidecar (``opencode-sidecar:4096``) to
generate code changes based on a plan.

The OpenCode sidecar is a headless HTTP server (``opencode serve``) that
provides session-based code generation with LSP integration, symbol search,
and diff extraction. Each task gets its own session for isolation.

The caller workflow (AgentRunnerWorkflow) is responsible for setting the
``start_to_close_timeout`` to 5 minutes via activity options.

Design reference: design.md §3.3, MIMARI §8 (OpenCode Sidecar)
Requirements: 7.5
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from temporalio import activity

from http_shared import make_mcp_client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Default OpenCode sidecar endpoint. Overridable via OPENCODE_ENDPOINT env var.
_DEFAULT_OPENCODE_ENDPOINT: str = "http://opencode-sidecar:4096"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodePlan:
    """Input plan describing what code to generate.

    Attributes
    ----------
    issue_key : str
        The Jira issue key (e.g. ``PAY-4211``).
    prompt : str
        The detailed instruction for code generation.
    model : str | None
        Optional LLM model override (e.g. ``vllm/qwen2.5-coder``).
        If None, the sidecar uses its configured default.
    """

    issue_key: str
    prompt: str
    model: str | None = None


@dataclass(frozen=True)
class FileChange:
    """A single file change produced by OpenCode.

    Attributes
    ----------
    path : str
        Relative file path within the workspace.
    action : str
        One of ``"created"``, ``"modified"``, ``"deleted"``.
    """

    path: str
    action: str
    content: str = ""


@dataclass(frozen=True)
class CodeResult:
    """Result of a code generation session.

    Attributes
    ----------
    files_changed : list[FileChange]
        List of files that were created, modified, or deleted.
    diff_content : str
        Unified diff of all changes.
    session_id : str
        The OpenCode session ID used (for traceability).
    """

    files_changed: list[FileChange]
    diff_content: str
    session_id: str
    files: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OpenCodeError(RuntimeError):
    """Raised when the OpenCode sidecar returns an unexpected error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# File extraction helpers
# ---------------------------------------------------------------------------


def _safe_relative_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ""
    return path


def _normalise_action(value: object) -> str:
    action = str(value or "modified").strip().lower()
    if action in {"created", "create", "added", "add"}:
        return "create"
    if action in {"deleted", "delete", "remove", "removed"}:
        return "delete"
    return "update"


def _paths_from_unified_diff(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in diff_text.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = _safe_relative_path(line[6:])
        if path and path not in paths:
            paths.append(path)
    return paths


def _read_workspace_file(workspace_path: str, rel_path: str) -> str:
    if not rel_path:
        return ""
    root = Path(workspace_path).resolve()
    target = (root / rel_path).resolve()
    try:
        if not target.is_relative_to(root) or not target.is_file():
            return ""
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _recent_workspace_entries(workspace_path: str, since_ts: float) -> list[dict]:
    root = Path(workspace_path).resolve()
    entries: list[dict] = []
    try:
        candidates = root.rglob("*")
    except OSError:
        return entries

    for target in candidates:
        try:
            if not target.is_file() or ".git" in target.parts:
                continue
            if target.stat().st_mtime < since_ts:
                continue
            rel_path = target.relative_to(root).as_posix()
        except OSError:
            continue
        path = _safe_relative_path(rel_path)
        if path:
            entries.append({"path": path, "action": "modified"})
        if len(entries) >= 50:
            break
    return entries


def _message_entries(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    candidates: list[object] = [payload]
    for key in ("result", "message", "data", "output"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        files = item.get("files") or item.get("files_changed")
        if isinstance(files, list):
            return [entry for entry in files if isinstance(entry, dict)]
    return []


def _model_payload(model: str | None) -> dict[str, str] | None:
    value = (model or os.environ.get("OPENCODE_MODEL", "")).strip()
    if not value or "/" not in value:
        return None
    provider_id, model_id = value.split("/", 1)
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


@activity.defn(name="opencode_generate_code")
async def opencode_generate_code(plan: CodePlan, workspace_path: str) -> CodeResult:
    """Generate code changes via the OpenCode sidecar.

    This activity:
    1. Creates a new session on the OpenCode sidecar.
    2. Sends the code generation prompt as a message.
    3. Retrieves the diff of all changes made.
    4. Cleans up the session.

    The caller workflow MUST set ``start_to_close_timeout=timedelta(minutes=5)``
    in the activity options.

    Parameters
    ----------
    plan : CodePlan
        The code generation plan containing issue key, prompt, and optional
        model override.
    workspace_path : str
        Absolute path to the workspace directory where the code resides
        (shared volume between worker and sidecar).

    Returns
    -------
    CodeResult
        The generated code changes including file list and unified diff.

    Raises
    ------
    OpenCodeError
        If the sidecar returns an error at any step.
    """
    endpoint = os.environ.get("OPENCODE_ENDPOINT", _DEFAULT_OPENCODE_ENDPOINT)

    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=280.0,  # Just under the 5-min activity timeout
        base_url=endpoint,
    )

    session_id: str | None = None
    session_cleaned = False

    try:
        async with client:
            # 1. Create a new session
            session_payload: dict = {
                "title": plan.issue_key,
                "permission": [
                    {
                        "permission": "*",
                        "pattern": "*",
                        "action": "allow",
                    }
                ],
            }
            model_payload = _model_payload(plan.model)
            if model_payload:
                session_payload["model"] = {
                    "id": model_payload["modelID"],
                    "providerID": model_payload["providerID"],
                }

            session_resp = await client.post("/session", json=session_payload)
            if session_resp.status_code != 200:
                raise OpenCodeError(
                    f"Failed to create session: {session_resp.text}",
                    status_code=session_resp.status_code,
                )
            session_data = session_resp.json()
            session_id = session_data["id"]

            # 2. Send the code generation message
            message_payload: dict = {
                "parts": [{"type": "text", "text": plan.prompt}],
                "agent": "build",
                "tools": {"write": True, "edit": True, "bash": True},
            }
            if model_payload:
                message_payload["model"] = model_payload

            # Heartbeat before the potentially long-running LLM call.
            if activity.in_activity():
                activity.heartbeat("sending message to opencode")

            message_started_at = time.time()
            message_resp = await client.post(
                f"/session/{session_id}/message",
                json=message_payload,
            )
            if message_resp.status_code != 200:
                raise OpenCodeError(
                    f"Failed to send message: {message_resp.text}",
                    status_code=message_resp.status_code,
                )
            message_data = None
            try:
                message_data = message_resp.json()
            except ValueError:
                message_data = None

            # Heartbeat after message processing.
            if activity.in_activity():
                activity.heartbeat("message processed, retrieving diff")

            # 3. Retrieve the diff of changes
            diff_resp = await client.get(f"/session/{session_id}/diff")
            if diff_resp.status_code != 200:
                raise OpenCodeError(
                    f"Failed to retrieve diff: {diff_resp.text}",
                    status_code=diff_resp.status_code,
                )

            diff_content = diff_resp.text
            diff_data = None
            if diff_resp.headers.get("content-type", "").startswith(
                "application/json"
            ):
                try:
                    diff_data = diff_resp.json()
                except ValueError:
                    diff_data = None

            # Parse file changes from diff response
            files_changed: list[FileChange] = []
            raw_entries: list[dict] = []
            message_entries = _message_entries(message_data)
            if message_entries:
                raw_entries = message_entries
            elif diff_data and isinstance(diff_data, list):
                raw_entries = [entry for entry in diff_data if isinstance(entry, dict)]
            elif diff_data and isinstance(diff_data, dict):
                raw_entries = [
                    entry
                    for entry in diff_data.get("files", [])
                    if isinstance(entry, dict)
                ]
                diff_content = diff_data.get("diff", diff_content)

            if not raw_entries:
                raw_entries = [
                    {"path": path, "action": "modified"}
                    for path in _paths_from_unified_diff(diff_content)
                ]
            if not raw_entries:
                raw_entries = _recent_workspace_entries(
                    workspace_path,
                    message_started_at,
                )

            commit_files: list[dict[str, str]] = []
            for entry in raw_entries:
                path = _safe_relative_path(entry.get("path", ""))
                if not path:
                    continue
                action = _normalise_action(entry.get("action", "modified"))
                content = str(entry.get("content") or entry.get("code") or "")
                if action != "delete" and not content:
                    content = _read_workspace_file(workspace_path, path)
                files_changed.append(
                    FileChange(path=path, action=action, content=content)
                )
                if action == "delete" or content:
                    commit_files.append(
                        {"path": path, "action": action, "content": content}
                    )

            # 4. Clean up the session (best-effort)
            try:
                await client.delete(f"/session/{session_id}")
                session_cleaned = True
            except httpx.HTTPError:
                # Session cleanup is best-effort; don't fail the activity
                activity.logger.warning(
                    "Failed to delete OpenCode session %s", session_id
                )

            return CodeResult(
                files_changed=files_changed,
                diff_content=diff_content,
                session_id=session_id,
                files=commit_files,
            )

    except httpx.HTTPError as exc:
        raise OpenCodeError(
            f"HTTP error communicating with OpenCode sidecar: {exc}"
        ) from exc
    finally:
        # If we created a session but failed before cleanup, attempt cleanup
        if session_id and not session_cleaned:
            try:
                cleanup_client = make_mcp_client(
                    client_source="agent-runner-worker",
                    timeout=10.0,
                    base_url=endpoint,
                )
                async with cleanup_client:
                    await cleanup_client.delete(f"/session/{session_id}")
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
