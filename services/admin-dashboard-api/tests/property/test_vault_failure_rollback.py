"""— Vault write failure leaves no row; rollback failure surfaces 502.
spec. Two scenarios are parametrised:
* ``rollback_raises=False`` — the Vault write raises; the service
  rolls back cleanly and no row survives.
* ``rollback_raises=True`` — the Vault write raises AND the asyncpg
  ROLLBACK itself raises; the service still surfaces
  :class:`VaultWriteFailed` (→ 502) and logs
  ``llm_provider_rollback_failed`` at ERROR with the provider_id +
  exception class on the log record."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

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
    VaultWriteFailed,
    _ProviderCreateInput,
)


def _payload(api_key: str) -> _ProviderCreateInput:
    return _ProviderCreateInput(
        provider_type="anthropic",
        name="Claude",
        model="claude-3-5-sonnet",
        context_length=200000,
        base_url=None,
        api_key=api_key,
        org_id=None,
    )


@given(
    api_key=st.sampled_from(
        [
            "sk-ant-abc1234567890ABCDEFG",
            "sk-ant-xyz9876543210ZYXWVU",
        ]
    ),
)
@settings(max_examples=100, deadline=None)
def test_vault_write_failure_leaves_no_row(api_key: str) -> None:
    """The Vault write raises → no row exists for the provider_id."""

    async def _go() -> None:
        service, pool, _, _, _ = build_service(
            vault=FakeVault(fail_on={"write"})
        )
        with pytest.raises(VaultWriteFailed):
            await service.create(_payload(api_key), actor_id="admin")
        assert pool.providers == {}

    asyncio.run(_go())


@given(
    api_key=st.sampled_from(
        [
            "sk-ant-abc1234567890ABCDEFG",
            "sk-ant-rotated1234567890ABC",
        ]
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_rollback_failure_still_surfaces_502(
    api_key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rollback raising MUST NOT mask the VaultWriteFailed → 502 contract.

    The service logs ``llm_provider_rollback_failed`` at ERROR with the
    provider_id and the exception class name so an operator can
    correlate the failure post-hoc.
    """

    async def _go() -> bool:
        service, _, _, _, _ = build_service(
            vault=FakeVault(
                fail_on={"write"}, rollback_raises=True
            )
        )
        with pytest.raises(VaultWriteFailed):
            await service.create(_payload(api_key), actor_id="admin")
        return True

    with caplog.at_level(
        logging.ERROR, logger="src.llm_providers.service"
    ):
        asyncio.run(_go())

    rollback_records = [
        r
        for r in caplog.records
        if "llm_provider_rollback_failed" in r.getMessage()
    ]
    assert rollback_records, (
        "expected llm_provider_rollback_failed log line on rollback failure"
    )
    record = rollback_records[-1]
    assert "RuntimeError" in record.getMessage()
