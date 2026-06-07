"""Integration smoke - personal PAT fetch (`ops work` the implementation).

U14 opt-in: when a user has consented to the persistent personal-PAT
flow, calling ``/api/credentials/fetch`` returns a Vault path that
the assistant-service can dereference at request time.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("ASSISTANT_BASE_URL"),
    reason="ASSISTANT_BASE_URL not set",
)
def test_persistent_pat_returns_vault_ref(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    session_id = os.environ.get("INTEGRATION_PERSISTED_SESSION_ID", "")
    if not session_id:
        pytest.skip(
            "Set INTEGRATION_PERSISTED_SESSION_ID to a session that "
            "previously opted into PIN-encrypted persistence."
        )

    import httpx

    base = os.environ["ASSISTANT_BASE_URL"].rstrip("/")
    r = httpx.get(
        f"{base}/api/credentials/fetch",
        params={"session_id": session_id, "service": "jira"},
        timeout=15.0,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("vault_path", "").startswith("vault:atlassian/")
    # Plain credential MUST NOT be returned over the wire.
    assert "personal_token" not in body
    assert "api_token" not in body
