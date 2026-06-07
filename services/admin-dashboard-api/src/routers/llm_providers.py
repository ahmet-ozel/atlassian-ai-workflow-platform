"""``/admin/llm-providers`` + ``/admin/departments/{id}/llm-provider`` routers.

Implements the design's "Components › routers/llm_providers.py" section:
two thin sibling routers under the ``llm-providers`` tag, gated by
:func:`auth.dependencies.require_admin`.

The handlers carry no business logic - every operation delegates to
:class:`llm_providers.service.ProviderService`, which is built per
request from the lifespan-managed collaborators on ``app.state``:

* ``app.state.pg_pool`` - :class:`asyncpg.Pool`
* ``app.state.vault_client`` - :class:`VaultClient`
* ``app.state.http_client`` - :class:`httpx.AsyncClient`
* ``app.state.audit_logger`` - audit sink (``AsyncpgAuditSink`` in
  production, ``LoggingAuditSink`` in tests)

Errors raised by the service layer translate one-to-one into the
spec-mandated HTTP status codes; the four service exceptions
(:class:`VaultWriteFailed`, :class:`VaultDeleteFailed`,
:class:`ProviderInUse`, :class:`ProviderNotFound`,
:class:`ProviderInactive`) are caught here and rendered as the
documented response bodies.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..auth.dependencies import AuthClaims, require_admin
from ..llm_providers.connection_tester import ConnectionTester
from ..llm_providers.dept_override_repository import DeptOverrideRepository
from ..llm_providers.model_capabilities import model_capabilities
from ..llm_providers.repository import LLMProviderRepository
from ..llm_providers.schemas import (
    DeptOverrideDTO,
    LLMProviderConfigDTO,
    ProviderCreate,
    ProviderUpdate,
    SavedTestRequest,
    UnsavedTestRequest,
)
from ..llm_providers.service import (
    ProviderInUse,
    ProviderInactive,
    ProviderNotFound,
    ProviderService,
    VaultDeleteFailed,
    VaultWriteFailed,
    _ProviderCreateInput,
)


__all__ = ["router", "department_router"]


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------


router = APIRouter(
    prefix="/admin/llm-providers",
    tags=["llm-providers"],
    dependencies=[Depends(require_admin)],
)


department_router = APIRouter(
    prefix="/admin/departments",
    tags=["llm-providers"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


def _service(request: Request) -> ProviderService:
    """Build a fresh :class:`ProviderService` from ``app.state`` collaborators.

    Surfaces a deployment misconfiguration (router mounted but
    collaborators not wired) as a clear ``500`` rather than a downstream
    :class:`AttributeError`. Tests bypass this by setting
    ``app.state.llm_provider_service`` directly to a hand-rolled
    service instance.
    """

    cached = getattr(request.app.state, "llm_provider_service", None)
    if cached is not None:
        return cached

    state = request.app.state
    pool = getattr(state, "pg_pool", None)
    vault = getattr(state, "vault_client", None)
    http_client = getattr(state, "http_client", None)
    audit = getattr(state, "audit_logger", None) or getattr(
        state, "llm_provider_audit_sink", None
    )

    missing = [
        name
        for name, value in (
            ("pg_pool", pool),
            ("vault_client", vault),
            ("http_client", http_client),
            ("audit_logger", audit),
        )
        if value is None
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "llm_providers_unavailable",
                "missing": missing,
            },
        )

    tester = getattr(state, "llm_connection_tester", None)
    if tester is None:
        tester = ConnectionTester(http_client)

    return ProviderService(
        pool=pool,
        vault_client=vault,
        repo=LLMProviderRepository(),
        override_repo=DeptOverrideRepository(),
        connection_tester=tester,
        audit_sink=audit,
    )


def _service_dep(request: Request) -> ProviderService:
    """FastAPI ``Depends`` shim returning the per-request service."""

    return _service(request)


# ---------------------------------------------------------------------------
# Providers CRUD
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreate,
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> LLMProviderConfigDTO:
    """``POST /admin/llm-providers`` - create a new provider."""

    try:
        return await service.create(
            _payload_to_input(payload), actor_id=claims.sub
        )
    except VaultWriteFailed as exc:
        return _vault_failure_response(
            "vault_write_failed", exc.provider_id, exc.cause
        )


@router.get("")
async def list_providers(
    service: ProviderService = Depends(_service_dep),
) -> list[LLMProviderConfigDTO]:
    """``GET /admin/llm-providers`` - list every provider."""

    return await service.list_providers()


@router.get("/model-capabilities")
async def get_model_capabilities(model: str) -> dict[str, Any]:
    """``GET /admin/llm-providers/model-capabilities?model=…``.

    Returns ``{"model": str, "reasoning_effort": bool, "verbosity": bool}``
    so the provider form can show the tuning inputs only for models that
    accept them. Pure lookup - no DB / Vault access, no side effects.
    """

    caps = model_capabilities(model)
    return {"model": model, **caps}


@router.get("/{provider_id}")
async def get_provider(
    provider_id: UUID,
    service: ProviderService = Depends(_service_dep),
) -> LLMProviderConfigDTO:
    """``GET /admin/llm-providers/{id}`` - single provider."""

    dto = await service.get_provider(provider_id)
    if dto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "provider_not_found",
                "provider_id": str(provider_id),
            },
        )
    return dto


@router.put("/{provider_id}")
async def update_provider(
    provider_id: UUID,
    patch: ProviderUpdate,
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> LLMProviderConfigDTO:
    """``PUT /admin/llm-providers/{id}`` - partial update."""

    try:
        dto = await service.update(provider_id, patch, actor_id=claims.sub)
    except VaultWriteFailed as exc:
        return _vault_failure_response(
            "vault_write_failed", exc.provider_id, exc.cause
        )
    if dto is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "provider_not_found",
                "provider_id": str(provider_id),
            },
        )
    return dto


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: UUID,
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> JSONResponse:
    """``DELETE /admin/llm-providers/{id}`` - delete with referential check."""

    try:
        deleted = await service.delete(provider_id, actor_id=claims.sub)
    except ProviderInUse as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "provider_in_use",
                "provider_id": str(exc.provider_id),
                "dept_ids": exc.dept_ids,
            },
        )
    except VaultDeleteFailed as exc:
        return _vault_failure_response(
            "vault_delete_failed", exc.provider_id, exc.cause
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "provider_not_found",
                "provider_id": str(provider_id),
            },
        )
    return JSONResponse(
        status_code=status.HTTP_204_NO_CONTENT, content=None
    )


# ---------------------------------------------------------------------------
# Connection test endpoints
# ---------------------------------------------------------------------------


@router.post("/{provider_id}/test")
async def test_saved_provider(
    provider_id: UUID,
    _body: SavedTestRequest,
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> dict[str, Any]:
    """``POST /admin/llm-providers/{id}/test`` - test a persisted provider.

    The body must be the empty ``{}`` envelope; the
    :class:`SavedTestRequest` model's ``extra="forbid"`` config
    rejects any prompt-shaping field (R8.3, R8.4) before the handler
    runs.
    """

    result = await service.test_saved(provider_id, actor_id=claims.sub)
    return result.model_dump(mode="json")


@router.post("/test")
async def test_unsaved_provider(
    payload: UnsavedTestRequest,
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> dict[str, Any]:
    """``POST /admin/llm-providers/test`` - test an unsaved provider config."""

    result = await service.test_unsaved(payload, actor_id=claims.sub)
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Department override endpoints
# ---------------------------------------------------------------------------


@department_router.get("/{dept_id}/llm-provider")
async def get_dept_llm_provider(
    dept_id: str,
    service: ProviderService = Depends(_service_dep),
) -> DeptOverrideDTO:
    """``GET /admin/departments/{dept_id}/llm-provider`` - read the override."""

    return await service.get_override(dept_id)


@department_router.put("/{dept_id}/llm-provider")
async def set_dept_llm_provider(
    dept_id: str,
    payload: "_DeptOverrideUpdate",
    claims: AuthClaims = Depends(require_admin),
    service: ProviderService = Depends(_service_dep),
) -> DeptOverrideDTO:
    """``PUT /admin/departments/{dept_id}/llm-provider`` - pin / unpin.

    Body shape: ``{"provider_id": "<uuid>" | null}``. Passing ``null``
    deletes the override; passing a UUID upserts. Returns 422 when the
    target provider is missing and 409 when it is inactive.
    """

    try:
        return await service.set_override(
            dept_id, payload.provider_id, actor_id=claims.sub
        )
    except ProviderNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "provider_not_found",
                "provider_id": str(exc.provider_id),
            },
        ) from exc
    except ProviderInactive as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "provider_inactive",
                "provider_id": str(exc.provider_id),
            },
        ) from exc


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


from pydantic import BaseModel, ConfigDict  # noqa: E402 - small local model


class _DeptOverrideUpdate(BaseModel):
    """PUT body for the per-dept override endpoint."""

    model_config = ConfigDict(extra="forbid")

    provider_id: UUID | None = None


def _payload_to_input(payload: ProviderCreate) -> _ProviderCreateInput:
    """Project a :class:`ProviderCreate` variant into the service input."""

    return _ProviderCreateInput(
        provider_type=payload.provider_type,
        name=payload.name,
        model=payload.model,
        context_length=payload.context_length,
        base_url=(
            str(payload.base_url)
            if getattr(payload, "base_url", None) is not None
            else None
        ),
        api_key=getattr(payload, "api_key", None),
        org_id=getattr(payload, "org_id", None),
        reasoning_effort=getattr(payload, "reasoning_effort", None),
        verbosity=getattr(payload, "verbosity", None),
    )


def _vault_failure_response(
    error_code: str, provider_id: UUID, cause: Exception
) -> JSONResponse:
    """Render the documented 502 body for Vault read/write/delete failures."""

    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "error": error_code,
            "provider_id": str(provider_id),
            "detail": cause.__class__.__name__,
        },
    )
