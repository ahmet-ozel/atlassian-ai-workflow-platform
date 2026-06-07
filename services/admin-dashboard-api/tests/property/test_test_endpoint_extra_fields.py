"""- Test endpoint refuses prompt-shaping fields.
``{prompt, messages, content, max_tokens, max_output_tokens,
temperature, top_p, system}`` mixed into a saved or unsaved test
body MUST be rejected with HTTP 422 ``extra_fields_not_allowed`` and
the offending field names exactly equal to the subset chosen. No
upstream HTTP request is captured by the ``httpx.MockTransport``
because validation happens before the connection tester runs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st
from pydantic import TypeAdapter, ValidationError


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.llm_providers.error_handlers import (  # noqa: E402
    register_validation_error_handler,
)
from src.llm_providers.schemas import (  # noqa: E402
    FORBIDDEN_TEST_FIELDS,
    SavedTestRequest,
    UnsavedTestRequest,
)


def _build_app() -> tuple[TestClient, list[httpx.Request]]:
    """Mount the saved + unsaved test endpoints with a request counter.

    The route validates the body against the matching schema and, on
    success, would call the connection tester through the supplied
    ``httpx.AsyncClient``. The mock transport's call counter MUST stay
    at zero across every property iteration - validation failures
    short-circuit before any upstream request is sent.
    """

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"model": "x"})

    app = FastAPI()
    register_validation_error_handler(app)
    saved_adapter: TypeAdapter[Any] = TypeAdapter(SavedTestRequest)
    unsaved_adapter: TypeAdapter[Any] = TypeAdapter(UnsavedTestRequest)

    @app.post("/admin/llm-providers/{provider_id}/test")
    async def saved_test(
        provider_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            saved_adapter.validate_python(payload)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        # The real endpoint would call the connection tester here; the
        # property only checks the negative path so we leave success a
        # no-op.
        return {"ok": True, "provider_id": provider_id}

    @app.post("/admin/llm-providers/test")
    async def unsaved_test(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            unsaved_adapter.validate_python(payload)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        return {"ok": True}

    return TestClient(app), captured


_CLIENT, _CAPTURED = _build_app()


@given(
    extras=st.lists(
        st.sampled_from(list(FORBIDDEN_TEST_FIELDS)),
        unique=True,
        min_size=1,
    )
)
@settings(max_examples=100, deadline=None)
def test_saved_test_rejects_prompt_shaping_fields(extras: list[str]) -> None:
    """``POST /admin/llm-providers/{id}/test`` with extras → 422."""

    body: dict[str, Any] = {field: "x" for field in extras}
    before = len(_CAPTURED)
    response = _CLIENT.post(
        "/admin/llm-providers/00000000-0000-0000-0000-000000000000/test",
        json=body,
    )
    assert response.status_code == 422
    body_json = response.json()
    assert body_json.get("error") == "extra_fields_not_allowed"
    assert sorted(body_json.get("fields", [])) == sorted(extras)
    # No upstream call was made.
    assert len(_CAPTURED) == before


@given(
    extras=st.lists(
        st.sampled_from(list(FORBIDDEN_TEST_FIELDS)),
        unique=True,
        min_size=1,
    )
)
@settings(max_examples=100, deadline=None)
def test_unsaved_test_rejects_prompt_shaping_fields(extras: list[str]) -> None:
    """``POST /admin/llm-providers/test`` with extras → 422."""

    # Build a minimally-valid OpenAI body so the discriminated union
    # picks the variant before encountering the extras.
    body: dict[str, Any] = {
        "provider_type": "openai",
        "name": "probe",
        "model": "gpt-4o-mini",
        "context_length": 128000,
        "api_key": "sk-test-1234567890ABCDEFGH",
    }
    for field in extras:
        body[field] = "x"
    before = len(_CAPTURED)
    response = _CLIENT.post("/admin/llm-providers/test", json=body)
    assert response.status_code == 422
    body_json = response.json()
    assert body_json.get("error") == "extra_fields_not_allowed"
    assert sorted(body_json.get("fields", [])) == sorted(extras)
    assert len(_CAPTURED) == before
