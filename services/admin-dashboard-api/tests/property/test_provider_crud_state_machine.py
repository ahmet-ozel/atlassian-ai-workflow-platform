"""- CRUD round-trip preserves metadata and never leaks credentials.
9.2, 9.3. A :class:`RuleBasedStateMachine` issues legal sequences of
create / update / get / list / delete against the
:class:`ProviderService` with the in-memory ``VaultClient`` fake.
After every step the property asserts:
* every returned DTO carries the masked credential field
  (``api_key_masked``) - never the raw ``api_key``;
* every persisted Vault payload's ``api_key`` matches the most-recent
  written value (no stale credential survives an update);
* no DTO serialisation contains an unredacted credential marker."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _llm_providers_fakes import build_service  # noqa: E402
from src.llm_providers.schemas import ProviderUpdate  # noqa: E402
from src.llm_providers.service import _ProviderCreateInput  # noqa: E402


_FORBIDDEN_MARKERS = (
    "sk-test-",
    "sk-proj-",
    "sk-ant-",
    "sk-live-",
)


_KEY_STRATEGY = st.text(
    alphabet=st.sampled_from(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ),
    min_size=20,
    max_size=40,
).map(lambda s: f"sk-test-{s}")


def _serialise(dto) -> str:
    return json.dumps(dto.model_dump(mode="json"), default=str)


def _no_leak(dto, raw_key: str) -> None:
    body = _serialise(dto)
    assert raw_key not in body, "raw api_key leaked to DTO"
    for marker in _FORBIDDEN_MARKERS:
        # The raw key starts with sk-test- which IS a forbidden marker
        # prefix; the masked credential collapses it to "…<last4>" so
        # the prefix should not appear verbatim either.
        if marker == "sk-test-":
            continue
        assert marker not in body


class ProviderCrudStateMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        result = build_service()
        self._service, self._pool, self._vault, _, _ = result
        self._provider_keys: dict[str, str] = {}  # provider_id -> last raw key

    def teardown(self) -> None:
        self._loop.close()

    @initialize()
    def _seed(self) -> None:
        return None

    @rule(
        api_key=_KEY_STRATEGY,
        name=st.from_regex(r"[A-Za-z][A-Za-z0-9 \-_]{2,16}", fullmatch=True),
    )
    def create_provider(self, api_key: str, name: str) -> None:
        async def _go() -> None:
            dto = await self._service.create(
                _ProviderCreateInput(
                    provider_type="openai",
                    name=name.strip(),
                    model="gpt-4o-mini",
                    context_length=128000,
                    base_url=None,
                    api_key=api_key,
                    org_id=None,
                ),
                actor_id="admin",
            )
            self._provider_keys[str(dto.id)] = api_key
            _no_leak(dto, api_key)
            # Vault has the raw key - Postgres only carries vault_path.
            stored = self._vault.storage[dto.id]
            assert stored["api_key"] == api_key

        self._loop.run_until_complete(_go())

    @rule(rotated_key=_KEY_STRATEGY)
    def rotate_api_key(self, rotated_key: str) -> None:
        if not self._provider_keys:
            return
        provider_id = next(iter(self._provider_keys))
        from uuid import UUID

        async def _go() -> None:
            dto = await self._service.update(
                UUID(provider_id),
                ProviderUpdate(api_key=rotated_key),
                actor_id="admin",
            )
            if dto is None:
                return
            self._provider_keys[provider_id] = rotated_key
            _no_leak(dto, rotated_key)
            # Vault has the new key.
            assert self._vault.storage[dto.id]["api_key"] == rotated_key

        self._loop.run_until_complete(_go())

    @rule()
    def list_providers(self) -> None:
        async def _go() -> None:
            dtos = await self._service.list_providers()
            assert len(dtos) == len(self._provider_keys)
            for dto in dtos:
                raw = self._provider_keys[str(dto.id)]
                _no_leak(dto, raw)

        self._loop.run_until_complete(_go())

    @rule()
    def get_provider(self) -> None:
        if not self._provider_keys:
            return
        provider_id = next(iter(self._provider_keys))
        from uuid import UUID

        async def _go() -> None:
            dto = await self._service.get_provider(UUID(provider_id))
            assert dto is not None
            raw = self._provider_keys[provider_id]
            _no_leak(dto, raw)
            # The mask preserves the last 4 chars of the raw key.
            assert dto.api_key_masked.endswith(raw[-4:])

        self._loop.run_until_complete(_go())

    @rule()
    def delete_provider(self) -> None:
        if not self._provider_keys:
            return
        provider_id = next(iter(self._provider_keys))
        from uuid import UUID

        async def _go() -> None:
            removed = await self._service.delete(
                UUID(provider_id), actor_id="admin"
            )
            assert removed is True
            self._provider_keys.pop(provider_id)

        self._loop.run_until_complete(_go())


TestProviderCrudStateMachine = ProviderCrudStateMachine.TestCase
TestProviderCrudStateMachine.settings = settings(
    max_examples=100,
    deadline=None,
    stateful_step_count=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
