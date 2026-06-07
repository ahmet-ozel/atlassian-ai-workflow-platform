"""- Provider-in-use deletes are blocked before any side-effect.
* Provider with at least one referencing dept  409 with ``dept_ids``
  matching the override set; no Vault or DB write happens.
* Provider with no overrides + healthy Vault delete  row removed
  AND Vault key removed; exactly one ``llm_provider_deleted`` audit
  emitted.
* Provider with no overrides + Vault delete raises  row stays;
  exactly one ``llm_provider_delete_vault_failed`` audit emitted."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _llm_providers_fakes import (  # noqa: E402
    FakeVault,
    build_service,
)
from src.llm_providers.service import (  # noqa: E402
    ProviderInUse,
    VaultDeleteFailed,
    _ProviderCreateInput,
)


def _payload() -> _ProviderCreateInput:
    return _ProviderCreateInput(
        provider_type="openai",
        name="probe",
        model="gpt-4o-mini",
        context_length=128000,
        base_url=None,
        api_key="sk-test-1234567890ABCDEFGH",
        org_id=None,
    )


@given(
    dept_ids=st.lists(
        st.from_regex(r"[a-z][a-z0-9-]{2,20}", fullmatch=True),
        unique=True,
        min_size=1,
        max_size=5,
    )
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_in_use_delete_returns_409_without_side_effects(
    dept_ids: list[str],
) -> None:
    """Override set non-empty  409 with dept_ids; no Vault / DB writes."""

    async def _go() -> None:
        service, pool, vault, _, _ = build_service()
        dto = await service.create(_payload(), actor_id="admin")
        for dept in dept_ids:
            pool.overrides[dept] = dto.id
        vault_writes_before = list(vault.writes)

        with pytest.raises(ProviderInUse) as exc_info:
            await service.delete(dto.id, actor_id="admin")

        assert sorted(exc_info.value.dept_ids) == sorted(dept_ids)
        # No Vault delete, no row removal.
        assert dto.id in pool.providers
        assert vault.deletes == []
        # No extra Vault write happened either.
        assert vault.writes == vault_writes_before

    asyncio.run(_go())


def test_clean_delete_removes_row_and_emits_audit() -> None:
    """Clean delete removes Postgres row + Vault key + emits one audit."""

    async def _go() -> None:
        service, pool, vault, _, audit = build_service()
        dto = await service.create(_payload(), actor_id="admin")

        deleted = await service.delete(dto.id, actor_id="admin")
        assert deleted is True
        assert dto.id not in pool.providers
        assert vault.deletes == [dto.id]
        delete_events = [
            e
            for e in audit.events
            if e.action == "llm_provider_deleted"
        ]
        assert len(delete_events) == 1

    asyncio.run(_go())


def test_vault_delete_failure_keeps_row_and_emits_failure_audit() -> None:
    """Vault delete raises  row remains AND failure audit emitted exactly once."""

    async def _go() -> None:
        service, pool, vault, _, audit = build_service(
            vault=FakeVault(fail_on={"delete"})
        )
        dto = await service.create(_payload(), actor_id="admin")

        with pytest.raises(VaultDeleteFailed):
            await service.delete(dto.id, actor_id="admin")

        # Row survives the Vault delete failure.
        assert dto.id in pool.providers
        failure_events = [
            e
            for e in audit.events
            if e.action == "llm_provider_delete_vault_failed"
        ]
        assert len(failure_events) == 1

    asyncio.run(_go())
