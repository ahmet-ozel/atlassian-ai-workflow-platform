"""Integration test: ``config/departments.json`` validates against its schema.

This test validates that the schema is loadable by ``automation-service``
and ``admin-dashboard-api`` and successfully validates the bundled example
``departments.json``. It also enforces a small set of structural
invariants:

1. Both JSON files are syntactically well-formed.
2. The schema declares ``$schema`` =
 ``https://json-schema.org/draft/2020-12/schema`` .
3. ``config/departments.json`` validates against
 ``config/departments.schema.json`` using
 ``jsonschema.Draft202012Validator`` .
4. The three example departments (``payment``, ``hr``, ``legal``) are
 present .
5. Every ``bot.<service>.account_id`` is the empty string, matching the
 "auto-fetched on first probe; leave empty initially" invariant.

The test is *integration*-flavoured because it loads real artifacts
from disk and exercises the public ``jsonschema`` validator end-to-end
rather than mocking either side.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema import Draft202012Validator

# The three departments are significant only for the ``set``-based
# presence check below; the
# ``departments.json`` file itself MAY list them in any order.
EXPECTED_DEPARTMENT_IDS: frozenset[str] = frozenset({"payment", "hr", "legal"})

# Atlassian bot service slots that, when present on a department, must
# carry an empty ``account_id``. The schema marks all three as optional
# (any non-empty subset is accepted) so we only assert on the slots that
# actually exist.
BOT_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")

# JSON Schema 2020-12 dialect URI required by the schema.
DRAFT_2020_12_URI: str = "https://json-schema.org/draft/2020-12/schema"


@pytest.fixture(scope="module")
def departments_data(repo_root: Path) -> dict:
    """Loads ``config/departments.json`` once per module."""

    path = repo_root / "config" / "departments.json"
    assert path.is_file(), f"missing fixture file: {path}"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def departments_schema(repo_root: Path) -> dict:
    """Loads ``config/departments.schema.json`` once per module."""

    path = repo_root / "config" / "departments.schema.json"
    assert path.is_file(), f"missing fixture file: {path}"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_departments_json_is_well_formed(repo_root: Path) -> None:
    """``config/departments.json`` parses as JSON without errors.

 Loaded directly here (rather than via the fixture) so a malformed
 file surfaces as a focused failure on this test.
 """

    path = repo_root / "config" / "departments.json"
    raw = path.read_text(encoding="utf-8")
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"departments.json is not valid JSON: {exc}")


def test_departments_schema_is_well_formed(repo_root: Path) -> None:
    """``config/departments.schema.json`` parses as JSON without errors."""

    path = repo_root / "config" / "departments.schema.json"
    raw = path.read_text(encoding="utf-8")
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - regression guard
        pytest.fail(f"departments.schema.json is not valid JSON: {exc}")


def test_schema_declares_draft_2020_12(departments_schema: dict) -> None:
    """The schema's ``$schema`` URI must be JSON Schema 2020-12.

 Validates the dialect determines which keywords
 (e.g. ``minProperties``, ``anyOf``) are honoured by the validator.
 """

    assert departments_schema.get("$schema") == DRAFT_2020_12_URI, (
        f"expected $schema={DRAFT_2020_12_URI!r}, "
        f"got {departments_schema.get('$schema')!r}"
    )


def test_schema_itself_is_a_valid_draft_2020_12_schema(
    departments_schema: dict,
) -> None:
    """Sanity-check: the schema document conforms to its declared dialect.

 ``check_schema`` raises ``SchemaError`` for any violation; we treat
 that as a hard failure so a typo in the schema does not silently
 let invalid ``departments.json`` payloads pass downstream.
 """

    Draft202012Validator.check_schema(departments_schema)


def test_departments_json_validates_against_schema(
    departments_data: dict, departments_schema: dict
) -> None:
    """``departments.json`` MUST satisfy ``departments.schema.json``.

 Uses ``Draft202012Validator.iter_errors`` to surface *every* failure
 in one shot rather than aborting on the first one, which keeps
 diagnostic output useful when the example file drifts.

 Validates """

    validator = Draft202012Validator(departments_schema)
    errors = sorted(
        validator.iter_errors(departments_data),
        key=lambda e: list(e.absolute_path),
    )

    assert not errors, (
        "departments.json failed schema validation:\n"
        + "\n".join(
            f"  - at {list(err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
    )


def test_expected_departments_are_present(departments_data: dict) -> None:
    """The three example departments must all be defined.

 Validates (``payment``, ``hr``, ``legal``).
 """

    departments = departments_data.get("departments", [])
    assert isinstance(departments, list), "'departments' must be a JSON array"

    actual_ids = {dept.get("id") for dept in departments}
    missing = EXPECTED_DEPARTMENT_IDS - actual_ids
    extra = actual_ids - EXPECTED_DEPARTMENT_IDS

    assert not missing, f"missing required departments: {sorted(missing)}"
    assert not extra, (
        "unexpected extra departments present: "
        f"{sorted(extra)} (the project ships only payment/hr/legal)"
    )


def test_all_bot_account_ids_are_empty_strings(departments_data: dict) -> None:
    """Every populated ``bot.<service>.account_id`` is the empty string.

 Validates : ``account_id`` is auto-fetched on the
 first probe and MUST be left empty in the bundled example file.
 The schema permits ``null`` as well, so we additionally require the
 *string* form here to match the example fixture committed in
 the implementation.
 """

    departments = departments_data["departments"]
    offenders: list[str] = []

    for dept in departments:
        dept_id = dept.get("id", "<no-id>")
        bot = dept.get("bot", {})
        for service in BOT_SERVICES:
            entry = bot.get(service)
            if entry is None:
                # Service is optional per the schema's ``anyOf``; skip.
                continue
            account_id = entry.get("account_id")
            if account_id != "":
                offenders.append(
                    f"{dept_id}.bot.{service}.account_id = {account_id!r} "
                    "(expected empty string)"
                )

    assert not offenders, (
        "violation — bot account_id values must be empty "
        "strings:\n " + "\n ".join(offenders)
    )


def test_jsonschema_library_supports_draft_2020_12() -> None:
    """Defensive check that the installed ``jsonschema`` package exposes
 ``Draft202012Validator``.

 Older releases (<4.18) shipped only Draft 2019-09; failing here
 early surfaces an environment misconfiguration before downstream
 validation tests produce confusing errors.
 """

    assert hasattr(jsonschema, "Draft202012Validator"), (
        "jsonschema>=4.18 required for Draft 2020-12 support; "
        "see tests/requirements.txt"
    )
