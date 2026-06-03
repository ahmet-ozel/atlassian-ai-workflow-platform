"""Integration smoke — Chat SSE end-to-end (`ops work` the implementation).

Requires a running stack (assistant-service + dependencies). Gated
by ``--run-docker`` so the default fast-lane skips it. Drives a
single ``POST /api/chat/stream`` request through the public surface
and asserts the SSE stream terminates with ``done`` (no
``error`` / ``rate_limit_exhausted`` / ``token_cap_exceeded``).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _docker_available(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--run-docker"))


@pytest.mark.skipif(
    not os.environ.get("ASSISTANT_BASE_URL"),
    reason="ASSISTANT_BASE_URL not set; integration test requires a running stack",
)
def test_chat_stream_terminates_with_done(request: pytest.FixtureRequest) -> None:
    if not _docker_available(request):
        pytest.skip("requires --run-docker")

    import httpx

    base = os.environ["ASSISTANT_BASE_URL"].rstrip("/")
    payload = {
        "user_message": "Bana platformun yeteneklerini özetle.",
        "history": [],
        "dept_id": os.environ.get("INTEGRATION_DEPT_ID", "payment"),
        "session_id": "integration-smoke-1",
    }

    saw_done = False
    saw_error = False

    with httpx.stream(
        "POST",
        f"{base}/api/chat/stream",
        json=payload,
        timeout=60.0,
    ) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if '"type":"done"' in body or '"type": "done"' in body:
                saw_done = True
                break
            if '"type":"error"' in body or '"type": "error"' in body:
                saw_error = True
                break

    assert saw_done and not saw_error, (
        f"Chat SSE stream did not terminate cleanly "
        f"(saw_done={saw_done}, saw_error={saw_error})."
    )
