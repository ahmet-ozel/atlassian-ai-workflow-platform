"""Unit tests for the SSE test runner endpoint.

These tests cover the streaming endpoint contract:

* POST /admin/services/{service_name}/test?stream=true triggers SSE streaming.
* stdout/stderr stream line-by-line as SSE events; final event carries exit_code.
* Client disconnect terminates the subprocess.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Bootstrap sys.path so ``import src.*`` resolves under direct
# ``pytest tests/unit`` invocations from the service root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Tests for stream_subprocess_sse generator
# ---------------------------------------------------------------------------


class TestStreamSubprocessSSE:
    """Test the core SSE streaming generator."""

    @pytest.mark.asyncio
    async def test_streams_lines_as_sse_data_events(self) -> None:
        """Each stdout line becomes a data: <line>\\n\\n SSE frame."""
        from src.routers.test_runner import stream_subprocess_sse

        # Use a simple echo command
        if sys.platform == "win32":
            command = 'python -c "print(\'line1\'); print(\'line2\'); print(\'line3\')"'
        else:
            command = "printf 'line1\\nline2\\nline3\\n'"

        frames: list[bytes] = []
        async for frame in stream_subprocess_sse(command, ".", None):
            frames.append(frame)

        # Should have 3 data frames + 1 done event
        assert len(frames) == 4

        # Verify data frames
        assert frames[0] == b"data: line1\n\n"
        assert frames[1] == b"data: line2\n\n"
        assert frames[2] == b"data: line3\n\n"

        # Verify done event with exit code
        done_frame = frames[3].decode("utf-8")
        assert "event: done" in done_frame
        assert '"exit_code": 0' in done_frame

    @pytest.mark.asyncio
    async def test_final_event_contains_exit_code(self) -> None:
        """The final SSE event is 'event: done' with exit_code."""
        from src.routers.test_runner import stream_subprocess_sse

        if sys.platform == "win32":
            command = 'python -c "print(\'hello\')"'
        else:
            command = "echo hello"

        frames: list[bytes] = []
        async for frame in stream_subprocess_sse(command, ".", None):
            frames.append(frame)

        # Last frame should be the done event
        last_frame = frames[-1].decode("utf-8")
        assert last_frame.startswith("event: done\n")
        assert '"exit_code": 0' in last_frame

    @pytest.mark.asyncio
    async def test_nonzero_exit_code_reported(self) -> None:
        """Non-zero exit code is correctly reported in done event."""
        from src.routers.test_runner import stream_subprocess_sse

        if sys.platform == "win32":
            command = "python -c \"import sys; sys.exit(42)\""
        else:
            command = "sh -c 'exit 42'"

        frames: list[bytes] = []
        async for frame in stream_subprocess_sse(command, ".", None):
            frames.append(frame)

        # Last frame should report exit code 42
        last_frame = frames[-1].decode("utf-8")
        assert "event: done" in last_frame
        assert '"exit_code": 42' in last_frame

    @pytest.mark.asyncio
    async def test_client_disconnect_terminates_process(self) -> None:
        """When client disconnects, subprocess is terminated."""
        from src.routers.test_runner import stream_subprocess_sse

        # Mock request that reports disconnected after first frame
        mock_request = MagicMock()
        disconnect_count = 0

        async def is_disconnected():
            nonlocal disconnect_count
            disconnect_count += 1
            # Disconnect after the first check (allow one line through)
            return disconnect_count > 2

        mock_request.is_disconnected = is_disconnected

        # Use a long-running command
        if sys.platform == "win32":
            command = "python -c \"import time; [print(i) or time.sleep(0.1) for i in range(100)]\""
        else:
            command = "sh -c 'for i in $(seq 1 100); do echo $i; sleep 0.1; done'"

        frames: list[bytes] = []
        async for frame in stream_subprocess_sse(command, ".", mock_request):
            frames.append(frame)

        # Should have terminated early (not all 100 lines)
        # The generator should have returned without the done event
        assert len(frames) < 100

    @pytest.mark.asyncio
    async def test_empty_output_still_sends_done(self) -> None:
        """A command with no output still sends the done event."""
        from src.routers.test_runner import stream_subprocess_sse

        if sys.platform == "win32":
            command = "python -c \"pass\""
        else:
            command = "true"

        frames: list[bytes] = []
        async for frame in stream_subprocess_sse(command, ".", None):
            frames.append(frame)

        # Should have at least the done event
        assert len(frames) >= 1
        last_frame = frames[-1].decode("utf-8")
        assert "event: done" in last_frame
        assert '"exit_code": 0' in last_frame


# ---------------------------------------------------------------------------
# Tests for run_subprocess_json helper
# ---------------------------------------------------------------------------


class TestRunSubprocessJson:
    """Test the non-streaming subprocess execution."""

    @pytest.mark.asyncio
    async def test_captures_full_output(self) -> None:
        """Non-streaming mode captures all output."""
        from src.routers.test_runner import run_subprocess_json

        if sys.platform == "win32":
            command = 'python -c "print(\'hello world\')"'
        else:
            command = "echo 'hello world'"

        output, exit_code = await run_subprocess_json(command, ".")

        assert "hello world" in output
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_captures_nonzero_exit_code(self) -> None:
        """Non-streaming mode captures non-zero exit codes."""
        from src.routers.test_runner import run_subprocess_json

        if sys.platform == "win32":
            command = "python -c \"import sys; print('fail'); sys.exit(1)\""
        else:
            command = "sh -c 'echo fail; exit 1'"

        output, exit_code = await run_subprocess_json(command, ".")

        assert "fail" in output
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Tests for resolve_test_command
# ---------------------------------------------------------------------------


class TestResolveTestCommand:
    """Test command resolution logic."""

    def test_known_service_returns_command(self) -> None:
        """Known services return their default test command."""
        from src.routers.test_runner import resolve_test_command

        cmd = resolve_test_command("admin-dashboard-api", None)
        assert cmd is not None
        assert "pytest" in cmd

    def test_unknown_service_returns_none(self) -> None:
        """Unknown services return None."""
        from src.routers.test_runner import resolve_test_command

        cmd = resolve_test_command("nonexistent-service", None)
        assert cmd is None

    def test_manifest_takes_precedence(self) -> None:
        """When LifecycleService is available, manifest command wins."""
        from src.routers.test_runner import resolve_test_command

        mock_request = MagicMock()
        mock_entry = MagicMock()
        mock_entry.test_command = "custom-test-cmd"
        mock_lifecycle = MagicMock()
        mock_lifecycle.get_manifest_entry.return_value = mock_entry
        mock_request.app.state.lifecycle = mock_lifecycle

        cmd = resolve_test_command("admin-dashboard-api", mock_request)
        assert cmd == "custom-test-cmd"


# ---------------------------------------------------------------------------
# Tests for the endpoint via TestClient
# ---------------------------------------------------------------------------


class TestRunTestsEndpoint:
    """Test the POST /admin/services/{service_name}/test endpoint.

    Uses a minimal FastAPI app with only the test_runner router mounted
    to avoid route conflicts with the services_lifecycle router.
    """

    @pytest.fixture
    def client(self):
        """Create a test client with only the test_runner router."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.routers.test_runner import router
        from src.auth.dependencies import require_admin, AuthClaims

        test_app = FastAPI()
        test_app.include_router(router)

        # Override auth dependency to bypass authentication
        async def mock_require_admin():
            return AuthClaims(sub="test-user", groups=["admin"])

        test_app.dependency_overrides[require_admin] = mock_require_admin
        # Set app.state.lifecycle to None so resolve_test_command
        # falls back to the default lookup table
        test_app.state.lifecycle = None

        with TestClient(test_app, raise_server_exceptions=False) as c:
            yield c

    def test_unknown_service_returns_404(self, client) -> None:
        """Unknown service → 404."""
        response = client.post(
            "/admin/services/nonexistent-service-xyz/test",
        )
        assert response.status_code == 404

    def test_stream_false_returns_json(self, client) -> None:
        """stream=False returns JSON with output and exit_code."""
        with patch(
            "src.routers.test_runner.resolve_test_command",
            return_value='python -c "print(\'test output\')"',
        ), patch(
            "src.routers.test_runner.resolve_working_directory",
            return_value=".",
        ):
            response = client.post(
                "/admin/services/admin-dashboard-api/test?stream=false",
            )

        assert response.status_code == 200
        body = response.json()
        assert "output" in body
        assert "exit_code" in body
        assert body["service_name"] == "admin-dashboard-api"
        assert body["exit_code"] == 0
        assert "test output" in body["output"]

    def test_stream_true_returns_event_stream(self, client) -> None:
        """stream=True returns text/event-stream content type."""
        with patch(
            "src.routers.test_runner.resolve_test_command",
            return_value='python -c "print(\'streamed line\')"',
        ), patch(
            "src.routers.test_runner.resolve_working_directory",
            return_value=".",
        ):
            response = client.post(
                "/admin/services/admin-dashboard-api/test?stream=true",
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Parse SSE content
        content = response.text
        assert "data: streamed line" in content
        assert "event: done" in content
        assert '"exit_code": 0' in content
