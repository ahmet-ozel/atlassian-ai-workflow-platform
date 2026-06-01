"""Pydantic v2 schemas for the LLM provider management surface.

Implements the design's "Components › schemas.py" section: discriminated
union over ``ProviderCreate``, an ``extra="forbid"`` ``ProviderUpdate``,
two test-only request models that reject prompt-shaping fields
(``SavedTestRequest`` / ``UnsavedTestRequest``), and the read-side
DTOs (``LLMProviderConfigDTO``, ``DeptOverrideDTO``,
``ConnectionTestResult``, ``ConnectionTestError``).

The discriminated-union approach gives R2.1 — R2.4 their per-type
required-field semantics for free; FastAPI surfaces validation failures
as :class:`fastapi.exceptions.RequestValidationError` which the custom
exception handler in :mod:`llm_providers.error_handlers` rewrites into
the spec-mandated body shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


__all__ = [
    "ProviderType",
    "Status",
    "VllmCreate",
    "OpenAICreate",
    "AnthropicCreate",
    "GeminiCreate",
    "ProviderCreate",
    "ProviderUpdate",
    "SavedTestRequest",
    "UnsavedTestRequest",
    "ConnectionTestError",
    "ConnectionTestResult",
    "LLMProviderConfigDTO",
    "DeptOverrideDTO",
    "PROVIDER_TYPES",
    "FORBIDDEN_TEST_FIELDS",
]


#: The exact provider types this feature supports (R2.6). Anything
#: outside this set surfaces as 422 ``unsupported_provider_type`` via
#: the discriminated union's literal validator.
ProviderType = Literal["vllm", "openai", "anthropic", "gemini"]

#: Status values mirroring the Postgres CHECK constraint on
#: ``automation.llm_providers.status``.
Status = Literal["active", "inactive"]

#: Canonical list — used by the custom validation error handler to
#: populate the ``supported`` field of ``unsupported_provider_type``.
PROVIDER_TYPES: Final[tuple[str, ...]] = (
    "vllm",
    "openai",
    "anthropic",
    "gemini",
)

#: Fields that must never appear on a test request body (R8.3, R8.4).
#: The connection tester pins ``Test_Prompt = "hi"`` + ``Token_Cap = 5``
#: and refuses any caller-supplied prompt shaping so operators cannot
#: drive a large bill or smuggle a different prompt past the
#: per-provider budget.
FORBIDDEN_TEST_FIELDS: Final[tuple[str, ...]] = (
    "prompt",
    "messages",
    "content",
    "max_tokens",
    "max_output_tokens",
    "temperature",
    "top_p",
    "system",
)


# ---------------------------------------------------------------------------
# Create request models — discriminated union over ``provider_type``
# ---------------------------------------------------------------------------


class _ProviderBase(BaseModel):
    """Fields common to every ``ProviderCreate`` variant."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    provider_type: ProviderType
    model: str = Field(min_length=1, max_length=256)
    context_length: int = Field(gt=0)


class VllmCreate(_ProviderBase):
    """vLLM provider — requires ``base_url`` (R2.1)."""

    provider_type: Literal["vllm"]  # type: ignore[assignment]
    base_url: HttpUrl
    api_key: str | None = None


class OpenAICreate(_ProviderBase):
    """OpenAI-compatible provider — requires ``api_key`` (R2.2)."""

    provider_type: Literal["openai"]  # type: ignore[assignment]
    api_key: str = Field(min_length=1)
    org_id: str | None = None
    base_url: HttpUrl | None = None


class AnthropicCreate(_ProviderBase):
    """Anthropic provider — requires ``api_key`` (R2.3)."""

    provider_type: Literal["anthropic"]  # type: ignore[assignment]
    api_key: str = Field(min_length=1)


class GeminiCreate(_ProviderBase):
    """Google Gemini provider — requires ``api_key`` (R2.4)."""

    provider_type: Literal["gemini"]  # type: ignore[assignment]
    api_key: str = Field(min_length=1)


#: Discriminated union — FastAPI dispatches on ``provider_type``.
ProviderCreate = Annotated[
    VllmCreate | OpenAICreate | AnthropicCreate | GeminiCreate,
    Field(discriminator="provider_type"),
]


# ---------------------------------------------------------------------------
# Update request model — every field optional, extra forbidden (R4.6, R8.4)
# ---------------------------------------------------------------------------


class ProviderUpdate(BaseModel):
    """Partial-update payload — ``extra="forbid"`` so unknown fields 422.

    ``api_key`` omitted means *preserve existing credential* (R4.6);
    setting it to a non-empty string overwrites the Vault payload while
    every non-credential field updates only when present.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    context_length: int | None = Field(default=None, gt=0)
    base_url: HttpUrl | None = None
    api_key: str | None = Field(default=None, min_length=1)
    org_id: str | None = None
    status: Status | None = None


# ---------------------------------------------------------------------------
# Test request models — ``extra="forbid"`` rejects prompt-shaping fields
# ---------------------------------------------------------------------------


class SavedTestRequest(BaseModel):
    """Body for ``POST /admin/llm-providers/{id}/test`` — must be empty.

    The endpoint reads the persisted configuration + Vault credentials
    at request time; the caller may not supply prompt, message body or
    sampling parameters (R8.3, R8.4).
    """

    model_config = ConfigDict(extra="forbid")


class UnsavedTestRequest(_ProviderBase):
    """Body for ``POST /admin/llm-providers/test`` — same shape as create.

    Accepts a fresh provider configuration including ``api_key`` /
    ``org_id`` so an operator can probe a candidate before saving.  The
    inherited ``extra="forbid"`` config on :class:`_ProviderBase`
    rejects prompt-shaping fields the operator cannot override (R8.4).
    """

    base_url: HttpUrl | None = None
    api_key: str | None = None
    org_id: str | None = None


# ---------------------------------------------------------------------------
# Read-side DTOs
# ---------------------------------------------------------------------------


class ConnectionTestError(BaseModel):
    """Error envelope embedded in a failing :class:`ConnectionTestResult`."""

    model_config = ConfigDict(extra="forbid")

    status_code: int | None = None
    message: str


class ConnectionTestResult(BaseModel):
    """Result envelope returned by the connection tester (R5.4 — R5.6).

    On the timeout / non-2xx paths ``model`` is ``None`` and ``error``
    carries the diagnostic; on the 2xx path ``error`` is ``None`` and
    ``model`` is the upstream-echoed (or configured) model identifier.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    latency_ms: int
    model: str | None = None
    error: ConnectionTestError | None = None


class LLMProviderConfigDTO(BaseModel):
    """Read-side projection of an ``automation.llm_providers`` row.

    Every credential is masked through
    :func:`llm_providers.masking.mask` before serialisation — the
    schema deliberately exposes ``api_key_masked`` (and the optional
    ``org_id_masked``) rather than the raw fields so accidental
    routes that return this DTO cannot leak a credential.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    provider_type: ProviderType
    name: str
    model: str
    context_length: int
    base_url: str | None = None
    status: Status
    api_key_masked: str
    org_id_masked: str | None = None
    last_tested_at: datetime | None = None
    last_test_error: str | None = None
    created_at: datetime
    updated_at: datetime


class DeptOverrideDTO(BaseModel):
    """Read-side projection of an ``automation.dept_llm_provider_overrides`` row.

    ``provider`` is ``None`` when the dept has no pin (R10.2 — the
    endpoint returns the documented null shape instead of 404 so the
    UI can render a clean "no override" panel).
    """

    model_config = ConfigDict(extra="forbid")

    dept_id: str
    provider: LLMProviderConfigDTO | None = None
