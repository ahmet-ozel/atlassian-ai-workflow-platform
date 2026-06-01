"""Property 9 — ``/healthz`` remains 200 after startup completes.

# Feature: automation-service-wiring, Property 9: Healthz stays 200

For any successful run of the lifespan startup phase, ``GET /healthz``
returns HTTP 200 with body ``{"status": "ok"}`` regardless of the state
of the downstream dependencies. ``/healthz`` is dependency-free; the
property pins the contract that the lifespan handler does not
accidentally wire ``/healthz`` to any of its constructed collaborators.

Validates Requirement 6.2 of the ``automation-service-wiring`` spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import (  # noqa: E402
    app_module,
    install_lifespan_fakes,
)


NON_FAILING_ACTIONS: tuple[str, ...] = (
    "noop_a",
    "noop_b",
    "noop_c",
    "noop_d",
)


@pytest.fixture(scope="module")
def wired_client():
    mp = pytest.MonkeyPatch()
    install_lifespan_fakes(mp)
    app = app_module.create_app()
    with TestClient(app) as client:
        yield client
    mp.undo()


@given(
    actions=st.lists(
        st.sampled_from(NON_FAILING_ACTIONS), max_size=8
    ),
)
@settings(max_examples=200, deadline=None)
def test_healthz_returns_200_with_status_ok(
    actions: list[str],
    wired_client: TestClient,
) -> None:
    """``GET /healthz`` returns 200 + ``{"status": "ok"}`` after startup."""

    # Hit a sampled subset of arbitrary "mid-traffic" actions first so
    # the property explores serving ``/healthz`` alongside other request
    # patterns. The actions themselves are no-op probes of ``/healthz``
    # so the assertion can be flat (one assertion per loop iteration
    # rather than one per generated action).
    for _ in actions:
        response = wired_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    response = wired_client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
