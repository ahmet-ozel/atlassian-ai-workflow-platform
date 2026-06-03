"""End-to-end test — automation-service mounts ``TraceMiddleware``.

The automation-service FastAPI app must:

* Set the ``X-Trace-Id`` response header on every reply, generated
  from a fresh UUIDv7 when the inbound request omits it.
* Preserve the inbound ``X-Trace-Id`` value when the caller supplies
  one, so Atlassian webhook retries keep the same trace_id.

Both behaviours are covered by the ``observability.TraceMiddleware``
unit tests; this test confirms the middleware is *wired* into the
production FastAPI factory (``automation_service.app.create_app``)
rather than re-exercising the middleware contract itself.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves under both pytest invocation styles (focused and root-level).
# Mirrors the bootstrap used by the sibling unit tests.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from automation_service.app import create_app  # noqa: E402


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_response_carries_x_trace_id_when_inbound_header_absent() -> None:
    """A fresh UUIDv7 is generated and echoed on the response."""

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    assert "X-Trace-Id" in resp.headers
    assert _UUID_RE.match(resp.headers["X-Trace-Id"]), resp.headers["X-Trace-Id"]


def test_response_preserves_inbound_x_trace_id() -> None:
    """A valid inbound ``X-Trace-Id`` round-trips through the middleware."""

    inbound = "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/healthz", headers={"X-Trace-Id": inbound})

    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-Id") == inbound
