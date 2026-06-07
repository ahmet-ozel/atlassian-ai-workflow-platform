"""- Department override CRUD honours referential / lifecycle rules.
* ``set_override(dept, provider)`` upserts when ``provider`` is active.
* ``get_override(dept)`` returns the documented payload (with shaped
  provider) or the ``provider=None`` shape for missing depts.
* Missing ``provider_id`` → :class:`ProviderNotFound` (→ 422
  ``provider_not_found``).
* Inactive provider → :class:`ProviderInactive` (→ 409
  ``provider_inactive``).
* ``set_override(dept, provider_id=None)`` deletes the row."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _llm_providers_fakes import build_service  # noqa: E402
from src.llm_providers.schemas import ProviderUpdate  # noqa: E402
from src.llm_providers.service import (  # noqa: E402
    ProviderInactive,
    ProviderNotFound,
    _ProviderCreateInput,
)


def _make_create(name: str = "claude") -> _ProviderCreateInput:
    return _ProviderCreateInput(
        provider_type="anthropic",
        name=name,
        model="claude-3-5-sonnet",
        context_length=200000,
        base_url=None,
        api_key="sk-ant-1234567890ABCDEFGHIJ",
        org_id=None,
    )


@given(
    dept_id=st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_set_override_then_get_returns_provider(dept_id: str) -> None:
    """PUT (active) → GET returns the shaped provider DTO."""

    async def _go() -> None:
        service, _, _, _, _ = build_service()
        dto = await service.create(_make_create(), actor_id="admin")
        await service.set_override(dept_id, dto.id, actor_id="admin")
        override = await service.get_override(dept_id)
        assert override.provider is not None
        assert override.provider.id == dto.id

    asyncio.run(_go())


@given(
    dept_id=st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_get_missing_returns_null_shape(dept_id: str) -> None:
    """GET on a dept with no pin → ``provider=None`` shape."""

    async def _go() -> None:
        service, _, _, _, _ = build_service()
        result = await service.get_override(dept_id)
        assert result.dept_id == dept_id
        assert result.provider is None

    asyncio.run(_go())


@given(
    dept_id=st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_set_override_missing_provider_raises_not_found(
    dept_id: str,
) -> None:
    """PUT with unknown provider_id → :class:`ProviderNotFound`."""

    async def _go() -> None:
        service, _, _, _, _ = build_service()
        with pytest.raises(ProviderNotFound):
            await service.set_override(dept_id, uuid4(), actor_id="admin")

    asyncio.run(_go())


@given(
    dept_id=st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_set_override_inactive_provider_raises_inactive(
    dept_id: str,
) -> None:
    """PUT with inactive provider → :class:`ProviderInactive`."""

    async def _go() -> None:
        service, _, _, _, _ = build_service()
        dto = await service.create(_make_create(), actor_id="admin")
        await service.update(
            dto.id, ProviderUpdate(status="inactive"), actor_id="admin"
        )
        with pytest.raises(ProviderInactive):
            await service.set_override(dept_id, dto.id, actor_id="admin")

    asyncio.run(_go())


@given(
    dept_id=st.from_regex(r"[a-z][a-z0-9-]{2,15}", fullmatch=True),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_set_override_none_deletes_the_row(dept_id: str) -> None:
    """PUT with ``provider_id=None`` deletes the override row."""

    async def _go() -> None:
        service, pool, _, _, _ = build_service()
        dto = await service.create(_make_create(), actor_id="admin")
        await service.set_override(dept_id, dto.id, actor_id="admin")
        assert pool.overrides.get(dept_id) == dto.id

        result = await service.set_override(dept_id, None, actor_id="admin")
        assert result.provider is None
        assert dept_id not in pool.overrides

    asyncio.run(_go())
