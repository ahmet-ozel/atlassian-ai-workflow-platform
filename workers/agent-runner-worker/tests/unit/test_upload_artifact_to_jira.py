"""Unit tests for the ``upload_artifact_to_jira`` activity.

Validates the MinIO → tempfile → MCP-jira pipeline contract:

* Happy path - download, stage, MCP-call, cleanup.
* Extension rejection - ``.exe`` and friends short-circuit before
  any download is attempted.
* Size cap - payloads above 100 MB return ``file_too_large`` and
  the tempfile is never opened.
* tempfile cleanup is guaranteed even when the MCP call raises.
* MCP HTTP 4xx responses propagate as ``error_code`` on the result.

Each test patches the two seams the activity exposes:

* :func:`activities.jira_attachment_pipe.artifact_download` - replaced
  with an in-memory fake to avoid touching MinIO.
* :func:`activities.jira_attachment_pipe._invoke_mcp_attachment_tool` -
  replaced with a fake that records the ``file_path`` it received and
  returns a scripted result.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors the rest of the agent-runner unit tests.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_LIBS_HTTP_SHARED: Path = _PLATFORM_ROOT / "libs" / "http-shared" / "src"
_LIBS_TEMPORAL_SHARED: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_SRC_DIR, _LIBS_HTTP_SHARED, _LIBS_TEMPORAL_SHARED):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

# noqa: E402 - imports after sys.path bootstrap.

from activities import jira_attachment_pipe as pipe_mod  # noqa: E402
from activities.jira_attachment_pipe import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    UploadArtifactToJiraInput,
    upload_artifact_to_jira,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _DownloadFake:
    """Records the (bucket, key) pair and returns scripted bytes."""

    def __init__(self, payload: bytes | Exception) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, bucket: str, key: str) -> bytes:
        self.calls.append((bucket, key))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _MCPInvokeFake:
    """Captures the ``file_path`` passed to the MCP invocation seam.

    The fake lets each test assert that:

    * the path it received exists at call time (i.e. the tempfile was
      written before the MCP call);
    * the path's basename carries the suffix derived from
      ``file_name``;
    * the (issue_key, file_name) contract was honoured.

    A scripted result (``dict`` or :class:`Exception`) drives the
    return path.
    """

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.observed_path_existed: list[bool] = []

    async def __call__(
        self,
        *,
        dept_id: str,
        issue_key: str,
        file_path: str,
        file_name: str,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "dept_id": dept_id,
                "issue_key": issue_key,
                "file_path": file_path,
                "file_name": file_name,
            }
        )
        # Snapshot whether the tempfile was on disk while the MCP
        # call was in flight - the cleanup invariant is that it is
        # there during the call and gone after the activity returns.
        self.observed_path_existed.append(os.path.exists(file_path))

        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def patch_pipe(monkeypatch: pytest.MonkeyPatch):
    """Provide pre-wired download + MCP fakes for each test.

    Returns a small namespace exposing the two fakes so tests can
    swap their scripted behaviour and inspect call records.
    """

    download = _DownloadFake(payload=b"")
    mcp = _MCPInvokeFake(result={"success": True})

    monkeypatch.setattr(pipe_mod, "artifact_download", download)
    monkeypatch.setattr(
        pipe_mod, "_invoke_mcp_attachment_tool", mcp
    )

    class _Namespace:
        pass

    ns = _Namespace()
    ns.download = download  # type: ignore[attr-defined]
    ns.mcp = mcp  # type: ignore[attr-defined]
    return ns


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Full pipeline: download → temp write → MCP call → cleanup."""

    def test_pipeline_returns_success_payload(self, patch_pipe) -> None:
        patch_pipe.download.payload = b"# report\nbody\n"
        patch_pipe.mcp.result = {
            "success": True,
            "issue_key": "PAY-4211",
            "filename": "report.md",
            "id": "10042",
            "size": len(b"# report\nbody\n"),
            "mimeType": "text/markdown",
            "self": "https://example.atlassian.net/rest/api/3/attachment/10042",
        }

        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-4211",
                bucket="ai-runs",
                key="artifacts/PAY-4211/iter-1/report.md",
                file_name="report.md",
                dept_id="payments",
            )
        )

        assert result["success"] is True
        assert result["issue_key"] == "PAY-4211"
        assert result["filename"] == "report.md"
        assert result["id"] == "10042"

    def test_download_called_with_bucket_and_key(self, patch_pipe) -> None:
        patch_pipe.download.payload = b"data"
        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-2/notes.md",
                file_name="notes.md",
                dept_id="payments",
            )
        )

        assert patch_pipe.download.calls == [
            ("ai-runs", "artifacts/PAY-1/iter-2/notes.md")
        ]

    def test_mcp_called_with_correct_arguments(self, patch_pipe) -> None:
        patch_pipe.download.payload = b"content"
        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-7",
                bucket="ai-runs",
                key="artifacts/PAY-7/iter-1/out.pdf",
                file_name="out.pdf",
                dept_id="payments",
            )
        )

        assert len(patch_pipe.mcp.calls) == 1
        call = patch_pipe.mcp.calls[0]
        assert call["issue_key"] == "PAY-7"
        assert call["file_name"] == "out.pdf"
        assert call["dept_id"] == "payments"
        # The tempfile must carry the original suffix so Jira's
        # content-type sniffing picks up the right MIME.
        assert call["file_path"].endswith(".pdf")
        # The path must have existed during the MCP call …
        assert patch_pipe.mcp.observed_path_existed == [True]
        # … and must be cleaned up afterwards.
        assert not os.path.exists(call["file_path"])

    def test_tempfile_contains_downloaded_payload(
        self, patch_pipe, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def _capture(*, dept_id, issue_key, file_path, file_name):
            with open(file_path, "rb") as fh:
                captured["bytes"] = fh.read()
            return {"success": True}

        patch_pipe.download.payload = b"hello-jira"
        # Re-patch the seam through ``monkeypatch`` so the module
        # binding is restored after the test (avoids leaking the
        # capture coroutine into other tests in the same module).
        monkeypatch.setattr(pipe_mod, "_invoke_mcp_attachment_tool", _capture)

        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/notes.md",
                file_name="notes.md",
                dept_id="payments",
            )
        )

        assert captured["bytes"] == b"hello-jira"


# ---------------------------------------------------------------------------
# 2. Extension validation
# ---------------------------------------------------------------------------


class TestExtensionRejection:
    """Disallowed extensions short-circuit before any I/O."""

    def test_exe_rejected_with_unsupported_format(self, patch_pipe) -> None:
        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/payload.exe",
                file_name="payload.exe",
                dept_id="payments",
            )
        )

        assert result["success"] is False
        assert result["error_code"] == "unsupported_format"
        assert ".exe" in result["error"]

    def test_extension_rejection_does_not_download(self, patch_pipe) -> None:
        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/binary.dll",
                file_name="binary.dll",
                dept_id="payments",
            )
        )
        assert patch_pipe.download.calls == []
        assert patch_pipe.mcp.calls == []

    @pytest.mark.parametrize(
        "extension", sorted(ALLOWED_EXTENSIONS)
    )
    def test_allowed_extensions_pass_validation(
        self, patch_pipe, extension: str
    ) -> None:
        patch_pipe.download.payload = b"data"
        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key=f"artifacts/PAY-1/iter-1/file{extension}",
                file_name=f"file{extension}",
                dept_id="payments",
            )
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# 3. Size limit
# ---------------------------------------------------------------------------


class TestSizeLimit:
    """Payloads above the 100 MB cap are rejected before tempfile staging."""

    def test_default_size_cap_is_100_mb(self) -> None:
        """The hard cap matches the MCP-side ``JiraAttachmentTool`` limit."""

        assert MAX_FILE_SIZE_BYTES == 100 * 1024 * 1024

    def test_oversized_payload_returns_file_too_large(
        self, patch_pipe, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the cap to 1 KB so the test does not allocate 100 MB
        # just to exercise the boundary check. The validation logic
        # (``len(content) > MAX_FILE_SIZE_BYTES``) is independent of
        # the absolute value, so a smaller cap exercises the same
        # code path.
        monkeypatch.setattr(pipe_mod, "MAX_FILE_SIZE_BYTES", 1024)
        patch_pipe.download.payload = b"x" * (1024 + 1)

        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/big.pdf",
                file_name="big.pdf",
                dept_id="payments",
            )
        )

        assert result["success"] is False
        assert result["error_code"] == "file_too_large"
        # The MCP call must NOT have been issued.
        assert patch_pipe.mcp.calls == []

    def test_payload_at_limit_is_accepted(
        self, patch_pipe, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pipe_mod, "MAX_FILE_SIZE_BYTES", 1024)
        patch_pipe.download.payload = b"x" * 1024
        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/limit.pdf",
                file_name="limit.pdf",
                dept_id="payments",
            )
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# 4. tempfile cleanup guarantee
# ---------------------------------------------------------------------------


class TestTempfileCleanup:
    """The staged tempfile is always removed in the ``finally`` block."""

    def test_tempfile_removed_on_mcp_exception(self, patch_pipe) -> None:
        """Download succeeded, MCP raised - tempfile still gone."""

        patch_pipe.download.payload = b"data"
        patch_pipe.mcp.result = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(
                upload_artifact_to_jira(
                    issue_key="PAY-1",
                    bucket="ai-runs",
                    key="artifacts/PAY-1/iter-1/report.md",
                    file_name="report.md",
                    dept_id="payments",
                )
            )

        # Snapshot recorded that the file existed during the MCP call …
        assert patch_pipe.mcp.observed_path_existed == [True]
        # … and now does not exist (the ``finally`` block ran).
        assert len(patch_pipe.mcp.calls) == 1
        assert not os.path.exists(patch_pipe.mcp.calls[0]["file_path"])

    def test_tempfile_removed_on_mcp_success(self, patch_pipe) -> None:
        patch_pipe.download.payload = b"data"
        patch_pipe.mcp.result = {"success": True}

        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/report.md",
                file_name="report.md",
                dept_id="payments",
            )
        )

        assert len(patch_pipe.mcp.calls) == 1
        assert not os.path.exists(patch_pipe.mcp.calls[0]["file_path"])

    def test_no_tempfile_when_extension_invalid(self, patch_pipe, tmp_path) -> None:
        """Extension rejection happens before any tempfile is opened.

        The fixture-level fakes already assert ``download.calls == []``
        in :class:`TestExtensionRejection`; this test additionally
        verifies that no leaked file under the system temp directory
        carries the rejected suffix from this run.
        """

        before = set(Path(tempfile.gettempdir()).glob("jira_upload_*"))
        asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/x.exe",
                file_name="x.exe",
                dept_id="payments",
            )
        )
        after = set(Path(tempfile.gettempdir()).glob("jira_upload_*"))
        # No new ``jira_upload_*`` files leaked from this run.
        assert after == before


# ---------------------------------------------------------------------------
# 5. MCP error propagation
# ---------------------------------------------------------------------------


class TestMCPErrorPropagation:
    """MCP-level failures are surfaced as ``error_code`` on the result."""

    def test_mcp_returns_failure_dict(self, patch_pipe) -> None:
        patch_pipe.download.payload = b"data"
        patch_pipe.mcp.result = {
            "success": False,
            "error_code": "issue_not_accessible",
            "error": "Issue 'PAY-9999' not found",
        }

        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-9999",
                bucket="ai-runs",
                key="artifacts/PAY-9999/iter-1/report.md",
                file_name="report.md",
                dept_id="payments",
            )
        )

        assert result["success"] is False
        assert result["error_code"] == "issue_not_accessible"
        assert "PAY-9999" in result["error"]

    def test_artifact_download_failure_returns_error(
        self, patch_pipe
    ) -> None:
        patch_pipe.download.payload = RuntimeError("S3 timeout")

        result = asyncio.run(
            upload_artifact_to_jira(
                issue_key="PAY-1",
                bucket="ai-runs",
                key="artifacts/PAY-1/iter-1/report.md",
                file_name="report.md",
                dept_id="payments",
            )
        )

        assert result["success"] is False
        assert result["error_code"] == "artifact_download_failed"
        # MCP call was never issued.
        assert patch_pipe.mcp.calls == []


# ---------------------------------------------------------------------------
# 6. Input dataclass
# ---------------------------------------------------------------------------


class TestInputDataclass:
    """The :class:`UploadArtifactToJiraInput` envelope is frozen and typed."""

    def test_input_is_frozen(self) -> None:
        inp = UploadArtifactToJiraInput(
            issue_key="PAY-1",
            bucket="ai-runs",
            key="key",
            file_name="x.md",
            dept_id="payments",
        )
        with pytest.raises(AttributeError):
            inp.issue_key = "PAY-2"  # type: ignore[misc]

    def test_input_fields(self) -> None:
        inp = UploadArtifactToJiraInput(
            issue_key="PAY-1",
            bucket="ai-runs",
            key="artifacts/PAY-1/iter-1/x.md",
            file_name="x.md",
            dept_id="payments",
        )
        assert inp.issue_key == "PAY-1"
        assert inp.bucket == "ai-runs"
        assert inp.key == "artifacts/PAY-1/iter-1/x.md"
        assert inp.file_name == "x.md"
        assert inp.dept_id == "payments"
