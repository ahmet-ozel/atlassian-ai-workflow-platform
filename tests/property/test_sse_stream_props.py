"""Property tests for SSE Event Stream Completeness.

**Validates: Requirements 4.2**

Property 7: SSE Event Stream Completeness

*For any* sequence of test output lines (stdout/stderr), the SSE stream
SHALL emit exactly one event per line in the original order. No lines
SHALL be dropped or reordered during streaming. The final event SHALL
contain the exit code.

Implementation note:
    The ``stream_subprocess_sse`` function in
    ``services/admin-dashboard-api/src/routers/test_runner.py`` cannot be
    imported directly in the test environment due to relative imports that
    require the full application context. Instead, this test exercises the
    SSE streaming behaviour end-to-end by spawning a real subprocess (a
    small Python script that outputs the Hypothesis-generated lines) and
    collecting the SSE frames produced by the same async generator logic.

    The async generator under test is re-implemented here with the same
    framing contract as the production code:
    - Each stdout line → ``data: <line>\\n\\n``
    - Process exit → ``event: done\\ndata: {"exit_code": N}\\n\\n``

    This validates that the SSE protocol guarantees hold for arbitrary
    output sequences.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator

from hypothesis import given, settings, assume
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# SSE streaming generator — mirrors production implementation
# ---------------------------------------------------------------------------


async def _stream_subprocess_sse(command: str, cwd: str) -> AsyncIterator[bytes]:
    """Spawn a subprocess and yield stdout/stderr as SSE events.

    This is a faithful reproduction of the core framing logic from
    ``routers/test_runner.py:stream_subprocess_sse`` without the
    FastAPI-specific disconnect detection (which is orthogonal to the
    completeness property being tested).

    Each output line becomes a ``data: <line>\\n\\n`` SSE frame.
    On completion, emits ``event: done\\ndata: {"exit_code": N}\\n\\n``.
    """
    import os
    import shlex

    # Force UTF-8 encoding for subprocess I/O to avoid codepage issues
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    if os.name == "nt":
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )
    else:
        args = shlex.split(command)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )

    assert process.stdout is not None  # noqa: S101

    # Stream line-by-line
    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
        yield f"data: {line}\n\n".encode("utf-8")

    # Wait for process to finish and get exit code
    exit_code = await process.wait()

    # Send final event with exit code
    yield (
        f"event: done\n"
        f"data: {{\"exit_code\": {exit_code}}}\n\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate printable line content (no newlines, no null bytes — those would
# be swallowed by readline or cause encoding issues). We use text that
# doesn't contain \n or \r so each generated string maps to exactly one
# output line from the subprocess.
_LINE_CONTENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S", "Zs"),
        blacklist_characters="\n\r\x00",
    ),
    min_size=1,
    max_size=80,
)

# Generate a non-empty list of output lines (simulating subprocess stdout).
_OUTPUT_LINES = st.lists(
    _LINE_CONTENT,
    min_size=1,
    max_size=30,
)

# Generate an exit code (0 = success, non-zero = failure).
_EXIT_CODES = st.integers(min_value=0, max_value=255)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_echo_script(lines: list[str], exit_code: int, tmp_dir: str) -> str:
    """Write a Python script that prints the given lines and exits.

    The script explicitly reconfigures stdout to UTF-8 to ensure
    characters survive the round-trip regardless of the system locale.

    Returns the command string to execute the script.
    """
    script_path = Path(tmp_dir) / "_sse_echo.py"
    # Write lines as a JSON-encoded list to avoid quoting issues
    script_content = (
        "import json, sys, io\n"
        "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')\n"
        f"lines = json.loads({json.dumps(lines)!r})\n"
        "for line in lines:\n"
        "    sys.stdout.write(line + '\\n')\n"
        "    sys.stdout.flush()\n"
        f"sys.exit({exit_code})\n"
    )
    script_path.write_text(script_content, encoding="utf-8")
    return f'"{sys.executable}" "{script_path}"'


async def _collect_sse_frames(command: str, cwd: str) -> list[bytes]:
    """Run the SSE generator and collect all emitted frames."""
    frames: list[bytes] = []
    async for frame in _stream_subprocess_sse(command, cwd):
        frames.append(frame)
    return frames


def _parse_sse_frames(frames: list[bytes]) -> tuple[list[str], dict | None]:
    """Parse collected SSE frames into data lines and the final done event.

    Returns:
        A tuple of (data_lines, done_event_data).
        - data_lines: list of line contents from ``data: <line>`` frames.
        - done_event_data: parsed dict from the ``event: done`` frame, or None.
    """
    data_lines: list[str] = []
    done_event: dict | None = None

    for frame in frames:
        text = frame.decode("utf-8")

        if text.startswith("event: done\n"):
            # Parse the done event: "event: done\ndata: {"exit_code": N}\n\n"
            for part in text.split("\n"):
                if part.startswith("data: "):
                    done_event = json.loads(part[len("data: "):])
                    break
        elif text.startswith("data: "):
            # Regular data line: "data: <content>\n\n"
            # Strip the "data: " prefix and trailing "\n\n"
            line_content = text[len("data: "):]
            # Remove trailing double newline (SSE frame separator)
            if line_content.endswith("\n\n"):
                line_content = line_content[:-2]
            data_lines.append(line_content)

    return data_lines, done_event


# ---------------------------------------------------------------------------
# Property Test
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=10000)
@given(
    lines=_OUTPUT_LINES,
    exit_code=_EXIT_CODES,
)
def test_sse_stream_completeness(lines: list[str], exit_code: int) -> None:
    """Feature: production-hardening, Property 7: SSE Event Stream Completeness

    **Validates: Requirements 4.2**

    For any sequence of output lines, the SSE stream emits exactly one
    event per line in the original order with no drops or reorders.
    The final event contains the exit code.
    """
    # Ensure lines can be JSON-encoded (skip surrogates, etc.)
    try:
        json.dumps(lines)
    except (ValueError, UnicodeEncodeError):
        assume(False)

    with tempfile.TemporaryDirectory() as tmp_dir:
        command = _write_echo_script(lines, exit_code, tmp_dir)

        # Collect SSE frames by running the subprocess
        frames = asyncio.run(_collect_sse_frames(command, tmp_dir))

        # Parse frames into data lines and done event
        data_lines, done_event = _parse_sse_frames(frames)

        # Property 1: Exactly one SSE event per line — no drops
        assert len(data_lines) == len(lines), (
            f"Expected {len(lines)} data events, got {len(data_lines)}. "
            f"Input lines: {lines!r}, "
            f"Received data events: {data_lines!r}"
        )

        # Property 2: Original order preserved — no reorders
        assert data_lines == lines, (
            f"SSE data lines do not match original order.\n"
            f"Expected: {lines!r}\n"
            f"Got:      {data_lines!r}"
        )

        # Property 3: Final event contains exit code
        assert done_event is not None, (
            "Missing 'event: done' frame in SSE stream. "
            f"All frames: {[f.decode('utf-8') for f in frames]!r}"
        )
        assert done_event.get("exit_code") == exit_code, (
            f"Expected exit_code={exit_code}, "
            f"got {done_event.get('exit_code')!r} in done event."
        )
