"""— Update preserves credentials when api_key is omitted.
NOT touch Vault — the persisted credential survives verbatim. When
``api_key`` is present, the new value lands in Vault and the
post-update payload's ``api_key`` equals the patch value while every
other persisted credential field is preserved."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _llm_providers_fakes import build_service  # noqa: E402
from src.llm_providers.schemas import ProviderUpdate  # noqa: E402
from src.llm_providers.service import _ProviderCreateInput  # noqa: E402


def _make_create(api_key: str, org_id: str | None = None) -> _ProviderCreateInput:
    return _ProviderCreateInput(
        provider_type="openai",
        name="probe",
        model="gpt-4o-mini",
        context_length=128000,
        base_url=None,
        api_key=api_key,
        org_id=org_id,
    )


_KEY_STRATEGY = st.text(
    alphabet=st.sampled_from(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ),
    min_size=20,
    max_size=40,
).map(lambda s: f"sk-test-{s}")


@given(initial_key=_KEY_STRATEGY)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_update_without_api_key_does_not_touch_vault(
    initial_key: str,
) -> None:
    """``ProviderUpdate`` with no ``api_key`` → Vault writes unchanged."""

    async def _go() -> None:
        service, _, vault, _, _ = build_service()
        dto = await service.create(
            _make_create(initial_key), actor_id="admin"
        )
        writes_before = list(vault.writes)
        storage_before = dict(vault.storage[dto.id])

        # Patch with non-credential field only.
        await service.update(
            dto.id, ProviderUpdate(name="renamed"), actor_id="admin"
        )

        assert vault.writes == writes_before
        assert vault.storage[dto.id] == storage_before

    asyncio.run(_go())


@given(initial_key=_KEY_STRATEGY, rotated_key=_KEY_STRATEGY)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_update_with_api_key_writes_new_value_to_vault(
    initial_key: str, rotated_key: str
) -> None:
    """``ProviderUpdate(api_key=...)`` overwrites the Vault payload."""

    async def _go() -> None:
        service, _, vault, _, _ = build_service()
        dto = await service.create(
            _make_create(initial_key), actor_id="admin"
        )
        await service.update(
            dto.id,
            ProviderUpdate(api_key=rotated_key),
            actor_id="admin",
        )
        assert vault.storage[dto.id]["api_key"] == rotated_key

    asyncio.run(_go())


@given(
    initial_key=_KEY_STRATEGY,
    initial_org_id=st.sampled_from(["org-x", "org-payment-ops"]),
    rotated_key=_KEY_STRATEGY,
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_update_api_key_only_preserves_org_id(
    initial_key: str, initial_org_id: str, rotated_key: str
) -> None:
    """Rotating ``api_key`` keeps the previously-stored ``org_id`` intact."""

    async def _go() -> None:
        service, _, vault, _, _ = build_service()
        dto = await service.create(
            _make_create(initial_key, org_id=initial_org_id),
            actor_id="admin",
        )
        await service.update(
            dto.id,
            ProviderUpdate(api_key=rotated_key),
            actor_id="admin",
        )
        merged = vault.storage[dto.id]
        assert merged["api_key"] == rotated_key
        assert merged.get("org_id") == initial_org_id

    asyncio.run(_go())
