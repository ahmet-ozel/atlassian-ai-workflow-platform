"""Task 1.2 — Migration smoke test for ``010_llm_providers.sql``.

Parses the migration file as text and asserts the spec-mandated
invariants:

* ``automation.llm_providers`` has NO column named ``api_key``,
  ``apikey``, ``secret``, ``token``, ``credential``, or ``org_id``
  (Requirement 3.2 — credentials live in Vault only).
* ``status`` default is ``'active'`` and the CHECK constraint
  enumerates the four supported provider types.
* ``dept_llm_provider_overrides.provider_id`` foreign key uses
  ``ON DELETE RESTRICT``.

Parsing is intentionally regex-based: standing up a real Postgres for
a single migration smoke test would balloon CI runtime, and the
invariants we care about are textual (column presence / CHECK
clauses / FK actions), not behavioural.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


#: Walk up to the platform/ root: tests/migrations -> tests -> service -> services -> platform.
_MIGRATION_PATH: Path = (
    Path(__file__).resolve().parents[4]
    / "infra"
    / "postgres"
    / "migrations"
    / "010_llm_providers.sql"
)


@pytest.fixture(scope="module")
def migration_sql() -> str:
    """Return the migration file as a single string."""

    assert _MIGRATION_PATH.exists(), (
        f"010_llm_providers.sql missing at {_MIGRATION_PATH}"
    )
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def _extract_table_block(sql: str, table_name: str) -> str:
    """Return the body of ``CREATE TABLE ... ({...})`` for *table_name*."""

    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table_name)}\s*"
        r"\(([\s\S]*?)\n\);",
        re.IGNORECASE,
    )
    match = pattern.search(sql)
    assert match is not None, f"could not locate CREATE TABLE {table_name}"
    return match.group(1)


class TestNoCredentialColumns:
    """Requirement 3.2 — no credential material in Postgres."""

    @pytest.mark.parametrize(
        "forbidden",
        ["api_key", "apikey", "secret", "token", "credential", "org_id"],
    )
    def test_llm_providers_has_no_credential_column(
        self, migration_sql: str, forbidden: str
    ) -> None:
        """Column ``forbidden`` must not appear in ``automation.llm_providers``."""

        block = _extract_table_block(migration_sql, "automation.llm_providers")
        # Match the column name as a left-aligned identifier ("    name TEXT").
        # The pattern allows leading whitespace and asserts the column name
        # is followed by either a space or a parenthesised type modifier.
        column_pattern = re.compile(
            rf"(?im)^\s+{re.escape(forbidden)}\s+\w",
        )
        assert not column_pattern.search(block), (
            f"forbidden column {forbidden!r} present in automation.llm_providers"
        )


class TestStatusAndProviderTypeConstraints:
    """Requirement 2.6 / 9.1 — status default + CHECK enums."""

    def test_status_default_is_active(self, migration_sql: str) -> None:
        block = _extract_table_block(
            migration_sql, "automation.llm_providers"
        )
        assert re.search(
            r"(?i)status\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'active'",
            block,
        ), "status column missing DEFAULT 'active'"

    def test_provider_type_check_enumerates_supported(
        self, migration_sql: str
    ) -> None:
        block = _extract_table_block(
            migration_sql, "automation.llm_providers"
        )
        # The CHECK clause may use IN (...) with the four allowed values.
        check_pattern = re.compile(
            r"(?is)provider_type\s+IN\s*\(\s*"
            r"'vllm'\s*,\s*'openai'\s*,\s*'anthropic'\s*,\s*'gemini'"
            r"\s*\)"
        )
        assert check_pattern.search(block), (
            "provider_type CHECK must enumerate "
            "{vllm, openai, anthropic, gemini}"
        )

    def test_status_check_enumerates_active_inactive(
        self, migration_sql: str
    ) -> None:
        block = _extract_table_block(
            migration_sql, "automation.llm_providers"
        )
        check_pattern = re.compile(
            r"(?is)status\s+IN\s*\(\s*'active'\s*,\s*'inactive'\s*\)"
        )
        assert check_pattern.search(block), (
            "status CHECK must enumerate {active, inactive}"
        )

    def test_context_length_positive_check(
        self, migration_sql: str
    ) -> None:
        block = _extract_table_block(
            migration_sql, "automation.llm_providers"
        )
        assert re.search(
            r"(?i)context_length\s*>\s*0", block
        ), "context_length must have CHECK (context_length > 0)"


class TestDeptOverrideForeignKey:
    """Requirement 1.7 — provider FK uses ON DELETE RESTRICT."""

    def test_provider_id_fk_on_delete_restrict(
        self, migration_sql: str
    ) -> None:
        block = _extract_table_block(
            migration_sql, "automation.dept_llm_provider_overrides"
        )
        # The FK declaration spans multiple lines:
        #   provider_id UUID NOT NULL
        #       REFERENCES automation.llm_providers(id) ON DELETE RESTRICT,
        pattern = re.compile(
            r"(?is)provider_id[\s\S]+?REFERENCES\s+"
            r"automation\.llm_providers\([^)]*\)\s+ON\s+DELETE\s+RESTRICT",
        )
        assert pattern.search(block), (
            "provider_id FK must use ON DELETE RESTRICT"
        )


class TestIndexes:
    """Spec-mandated indexes are present."""

    def test_status_index(self, migration_sql: str) -> None:
        assert re.search(
            r"(?i)idx_llm_providers_status", migration_sql
        ), "idx_llm_providers_status index missing"

    def test_created_at_desc_index(self, migration_sql: str) -> None:
        assert re.search(
            r"(?is)idx_llm_providers_created_at[\s\S]+?created_at\s+DESC",
            migration_sql,
        ), "idx_llm_providers_created_at DESC index missing"

    def test_dept_override_provider_index(self, migration_sql: str) -> None:
        assert re.search(
            r"(?i)idx_dept_llm_provider_overrides_provider", migration_sql
        ), "idx_dept_llm_provider_overrides_provider missing"
