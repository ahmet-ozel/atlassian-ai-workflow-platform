"""Unit tests for the LLM provider repository pair.

Covers:
* Round-trip insert / get / list_all / update / delete on
  :class:`LLMProviderRepository`.
* ``overrides_referencing`` returns the exact ``dept_id`` set.
* ``update_test_result`` touches only the two test-result columns.
* :class:`DeptOverrideRepository` upsert / get / delete shape.

Backed by the in-memory pool fake from
``tests/property/_llm_providers_fakes.py`` so the unit tests stay
infrastructure-free and share the same fake shape as the property
suite.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest


_PROPERTY_DIR = (
    Path(__file__).resolve().parents[1] / "property"
)
if str(_PROPERTY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROPERTY_DIR))


from _llm_providers_fakes import FakePool  # noqa: E402
from src.llm_providers.repository import LLMProviderRepository  # noqa: E402
from src.llm_providers.dept_override_repository import (  # noqa: E402
    DeptOverrideRepository,
)
from src.llm_providers.schemas import ProviderUpdate  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# LLMProviderRepository
# ---------------------------------------------------------------------------


def test_insert_then_get_returns_same_row() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()
    provider_id = uuid4()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.insert(
                conn,
                provider_id=provider_id,
                provider_type="openai",
                name="probe",
                model="gpt-4o-mini",
                context_length=128000,
                base_url=None,
            )
            assert row.id == provider_id
            got = await repo.get(conn, provider_id)
            assert got is not None
            assert got.name == "probe"
            assert got.provider_type == "openai"

    _run(_go())


def test_list_all_returns_every_insert() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()

    async def _go():
        async with pool.acquire() as conn:
            first = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="vllm",
                name="first",
                model="m",
                context_length=8192,
                base_url="http://x",
            )
            second = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="anthropic",
                name="second",
                model="claude",
                context_length=200000,
                base_url=None,
            )
            rows = await repo.list_all(conn)
            ids = {r.id for r in rows}
            assert ids == {first.id, second.id}

    _run(_go())


def test_update_merges_patch_and_bumps_updated_at() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="openai",
                name="orig",
                model="m",
                context_length=128000,
                base_url=None,
            )
            updated = await repo.update(
                conn,
                row.id,
                ProviderUpdate(name="renamed", status="inactive"),
            )
            assert updated is not None
            assert updated.name == "renamed"
            assert updated.status == "inactive"
            # Non-patched fields preserve their original value.
            assert updated.model == "m"
            # updated_at moved forward
            assert updated.updated_at >= row.updated_at

    _run(_go())


def test_delete_returns_true_then_false_on_missing() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="anthropic",
                name="claude",
                model="m",
                context_length=200000,
                base_url=None,
            )
            removed = await repo.delete(conn, row.id)
            assert removed is True
            again = await repo.delete(conn, row.id)
            assert again is False

    _run(_go())


def test_update_test_result_touches_only_two_columns() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="openai",
                name="probe",
                model="m",
                context_length=128000,
                base_url=None,
            )
            original_updated_at = pool.providers[row.id]["updated_at"]
            ts = datetime.now(timezone.utc)
            await repo.update_test_result(
                conn,
                row.id,
                last_tested_at=ts,
                last_test_error=None,
            )
            stored = pool.providers[row.id]
            assert stored["last_tested_at"] == ts
            assert stored["last_test_error"] is None
            # ``updated_at`` is NOT bumped by the test-result write.
            assert stored["updated_at"] == original_updated_at

    _run(_go())


def test_overrides_referencing_returns_exact_dept_set() -> None:
    pool = FakePool()
    repo = LLMProviderRepository()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.insert(
                conn,
                provider_id=uuid4(),
                provider_type="openai",
                name="probe",
                model="m",
                context_length=128000,
                base_url=None,
            )
            pool.overrides["dept-a"] = row.id
            pool.overrides["dept-b"] = row.id
            pool.overrides["dept-c"] = uuid4()  # other provider

            depts = await repo.overrides_referencing(conn, row.id)
            assert sorted(depts) == ["dept-a", "dept-b"]

    _run(_go())


# ---------------------------------------------------------------------------
# DeptOverrideRepository
# ---------------------------------------------------------------------------


def test_dept_override_upsert_then_get_returns_row() -> None:
    pool = FakePool()
    repo = DeptOverrideRepository()
    provider_id = uuid4()

    async def _go():
        async with pool.acquire() as conn:
            row = await repo.upsert(conn, "payment-ops", provider_id)
            assert row.dept_id == "payment-ops"
            assert row.provider_id == provider_id
            got = await repo.get(conn, "payment-ops")
            assert got is not None
            assert got.provider_id == provider_id

    _run(_go())


def test_dept_override_delete_removes_row() -> None:
    pool = FakePool()
    repo = DeptOverrideRepository()
    provider_id = uuid4()

    async def _go():
        async with pool.acquire() as conn:
            await repo.upsert(conn, "payment-ops", provider_id)
            await repo.delete(conn, "payment-ops")
            assert await repo.get(conn, "payment-ops") is None

    _run(_go())
