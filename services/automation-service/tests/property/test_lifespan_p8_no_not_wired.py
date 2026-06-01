"""Property 8 — after startup, no router replies with ``Router_Not_Wired_Error``.

# Feature: automation-service-wiring, Property 8: No Router_Not_Wired_Error

For any :class:`FastAPI` app produced by :func:`create_app` and any
successful run of the lifespan startup phase, hitting any path the
existing routers expose with a syntactically valid request never
returns an HTTP response whose body equals
``{"detail": "<name> router is not wired"}`` for any ``<name>`` in
the slot set after lifespan startup completes.

Auth failures, validation failures and downstream backend errors are
allowed and expected; only the wiring-error response shape is
forbidden.

Validates Requirements 5.3, 5.4, 5.5 and 6.1 of the
``automation-service-wiring`` spec.
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


#: Slot names that surface a ``"<name> router is not wired"`` detail in
#: the failure response of the matching router. Used to build the
#: forbidden detail-set the property asserts against.
SLOT_NAMES: tuple[str, ...] = (
    "dept_credentials",
    "admin",
    "webhooks",
    "cancel",
    "repo_sync",
    "po_review",
    "inbound",
    "webhook_v2",
    "webhook_pipeline",
)


#: A representative set of routes mounted by ``create_app``. Hitting any
#: of these after lifespan startup completes must NOT return a wiring-
#: error detail; auth/validation/backend failures are fine.
ROUTER_PATHS: tuple[tuple[str, str], ...] = (
    ("GET", "/admin/departments"),
    ("POST", "/admin/departments"),
    ("GET", "/api/orphan-branches"),
    ("GET", "/api/po-review-inbox"),
    ("POST", "/api/workflows/test/cancel"),
    ("POST", "/webhooks/jira"),
    ("POST", "/webhooks/bitbucket"),
    ("POST", "/webhooks/jira/issue_created"),
    ("POST", "/webhooks/jira/issue_commented"),
    ("POST", "/webhooks/inbound/slack"),
    ("POST", "/webhooks/jira/pipeline"),
)


@pytest.fixture(scope="module")
def wired_app():
    """Build a FastAPI app once and trigger the lifespan startup.

    Hypothesis property-tests below reuse this fixture across every
    sampled path; the fixture is module-scoped because the lifespan
    state survives the entire property run (the property only checks
    *response shape*, never asserts against close counters).

    ``raise_server_exceptions=False`` keeps a backend exception (the
    fake pool returns an ``object()`` from ``acquire()`` so any router
    that issues SQL during the request will surface an
    ``AttributeError`` on ``conn.execute``) from propagating through the
    test runner. The property only inspects the response body's
    ``detail`` field — the 500 itself is acceptable so long as the
    response does not carry a ``"<name> router is not wired"`` shape.
    """

    mp = pytest.MonkeyPatch()
    install_lifespan_fakes(mp)
    app = app_module.create_app()
    # ``TestClient`` enters the lifespan on ``__enter__`` and exits it
    # on ``__exit__`` — use the context-manager form so the property
    # observes a fully-wired app on every iteration.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    mp.undo()


@given(
    route_index=st.integers(min_value=0, max_value=len(ROUTER_PATHS) - 1),
    body=st.binary(max_size=256),
)
@settings(max_examples=200, deadline=None)
def test_no_router_not_wired_response(
    route_index: int,
    body: bytes,
    wired_app: TestClient,
) -> None:
    """Hitting any router path never surfaces a wiring-error detail."""

    method, path = ROUTER_PATHS[route_index]
    response = wired_app.request(method, path, content=body)

    # Parse JSON when available — non-JSON responses (e.g. raw 405) cannot
    # carry a wiring-error detail.
    try:
        body_dict = response.json()
    except ValueError:
        return

    if not isinstance(body_dict, dict):
        return
    detail = body_dict.get("detail")
    if not isinstance(detail, str):
        return

    forbidden = {f"{slot} router is not wired" for slot in SLOT_NAMES}
    # The cancel / po_review / webhooks_v2 routers emit a slightly more
    # verbose detail ("... router is not wired (app.state.<name> missing)").
    # Treat anything starting with one of the forbidden prefixes as a
    # violation so the property is robust to minor wording changes.
    for fragment in forbidden:
        assert not detail.startswith(fragment), (
            f"path {method} {path} returned wiring-error detail "
            f"after lifespan startup: {detail!r}"
        )
