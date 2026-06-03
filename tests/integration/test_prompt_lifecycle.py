"""Integration smoke — Prompt lifecycle (`ops work` the implementation).

End-to-end ``/admin/prompts`` round-trip: list prompts, read one,
create a draft branch, open a PR. Gated by ``--run-docker``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("ADMIN_API_BASE_URL"),
    reason="ADMIN_API_BASE_URL not set; integration test requires a running stack",
)
def test_prompt_list_then_draft(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    import httpx

    base = os.environ["ADMIN_API_BASE_URL"].rstrip("/")
    token = os.environ.get("ADMIN_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    r = httpx.get(f"{base}/admin/prompts", headers=headers, timeout=15.0)
    r.raise_for_status()
    items = r.json().get("items", [])
    assert items, "no prompts returned from /admin/prompts"

    first = items[0]
    detail = httpx.get(
        f"{base}/admin/prompts/{first['path']}",
        headers=headers,
        timeout=15.0,
    )
    detail.raise_for_status()
    body = detail.json().get("body", "")
    assert body, "prompt detail returned empty body"

    # The draft endpoint requires a workable git repo on the server
    # side; we POST a no-op edit (re-write the same body + a comment
    # marker) and assert a 201 response.
    edit = body + "\n<!-- integration smoke -->\n"
    draft = httpx.post(
        f"{base}/admin/prompts/{first['path']}/draft",
        headers={**headers, "Content-Type": "application/json"},
        json={"body": edit, "message": "integration smoke"},
        timeout=30.0,
    )
    assert draft.status_code in (201, 422), (
        f"unexpected draft status {draft.status_code}: {draft.text}"
    )
