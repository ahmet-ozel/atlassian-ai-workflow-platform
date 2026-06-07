"""- schema validation rejects malformed payloads.
spec: every malformed ``ProviderCreate`` body the operator could
submit is rejected with HTTP 422 carrying one of four documented
shapes - ``validation_failed`` (missing field or single reason),
``unsupported_provider_type``, or ``extra_fields_not_allowed``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


def _build_test_app() -> TestClient:
    """Mount a minimal app that exercises the create-validation surface.
    ``provider_type`` payloads. The route
    accepts a raw ``dict`` body, then hand-validates with a
    :class:`pydantic.TypeAdapter` over the discriminated-union alias
    so any :class:`ValidationError` flows through the project's
    custom 422 handler verbatim. This sidesteps FastAPI's body-
    resolution corner cases with discriminated unions exposed as
    top-level parameter types."""

    from fastapi import APIRouter, status
    from fastapi.exceptions import RequestValidationError
    from pydantic import TypeAdapter, ValidationError

    from src.llm_providers.error_handlers import (
        register_validation_error_handler,
    )
    from src.llm_providers.schemas import ProviderCreate

    app = FastAPI()
    register_validation_error_handler(app)
    router = APIRouter()

    _adapter: TypeAdapter[Any] = TypeAdapter(ProviderCreate)

    @router.post("/test/create", status_code=status.HTTP_200_OK)
    async def _create(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            _adapter.validate_python(payload)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors()) from exc
        return {"ok": True}

    app.include_router(router)
    return TestClient(app)


_CLIENT = _build_test_app()


@given(
    bogus_type=st.text(min_size=1, max_size=20).filter(
        lambda s: s not in ("vllm", "openai", "anthropic", "gemini")
    )
)
@settings(max_examples=100, deadline=None)
def test_unsupported_provider_type_returns_documented_shape(
    bogus_type: str,
) -> None:
    """Unknown ``provider_type``  422 ``unsupported_provider_type``."""

    response = _CLIENT.post(
        "/test/create",
        json={
            "provider_type": bogus_type,
            "name": "x",
            "model": "y",
            "context_length": 100,
        },
    )
    assert response.status_code == 422
    body = response.json()
    # Discriminator failures may fall through to either the
    # unsupported-type shape or the generic ``validation_failed``
    # shape depending on Pydantic's internal error type. Both are
    # acceptable per the spec - the property only forbids the raw
    # Pydantic ``detail: [...]`` shape leaking through.
    assert body.get("error") in {
        "unsupported_provider_type",
        "validation_failed",
    }


@given(
    extra_field=st.sampled_from(
        [
            "prompt",
            "messages",
            "content",
            "max_tokens",
            "max_output_tokens",
            "temperature",
            "top_p",
            "system",
        ]
    )
)
@settings(max_examples=100, deadline=None)
def test_extra_fields_are_rejected(extra_field: str) -> None:
    """Prompt-shaping fields  422 ``extra_fields_not_allowed``."""

    response = _CLIENT.post(
        "/test/create",
        json={
            "provider_type": "openai",
            "name": "x",
            "model": "gpt-4o-mini",
            "context_length": 100,
            "api_key": "sk-test1234567890ABCDEF",
            extra_field: "hello",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "extra_fields_not_allowed"
    assert extra_field in body.get("fields", [])


def test_missing_required_api_key_for_openai() -> None:
    """Missing ``api_key`` on OpenAI body  422 ``validation_failed``."""

    response = _CLIENT.post(
        "/test/create",
        json={
            "provider_type": "openai",
            "name": "x",
            "model": "gpt-4o-mini",
            "context_length": 100,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "validation_failed"


def test_non_positive_context_length() -> None:
    """``context_length <= 0``  422 ``validation_failed``."""

    response = _CLIENT.post(
        "/test/create",
        json={
            "provider_type": "anthropic",
            "name": "x",
            "model": "claude",
            "context_length": 0,
            "api_key": "sk-ant-test1234567890ABCDEFGH",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body.get("error") == "validation_failed"
