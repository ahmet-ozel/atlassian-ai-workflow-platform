"""Dept bulk import atomicity.



Background
----------

The bulk import service (``dept_bulk_import_service.py``) orchestrates
importing multiple departments from a single JSON file upload. It
reuses the foundation staging pattern:

 For each department in the uploaded file:
 1. Write credentials to staging Vault path.
 2. Run connectivity probe against staged credentials.
 3. On success: promote to final Vault path + DB commit.
 4. On failure: skip department, clean up staging, report in result.

The ``dry_run=True`` mode validates the schema and simulates probes
without writing any state (Vault or DB).

Strategy
--------

We use Hypothesis to generate random department JSON payloads (1-20
departments) with varying probe outcomes. Fake implementations of
VaultClient, AtlassianProbeClient, AsyncConnection, and AuditLogger
are injected to verify:

(a) Schema-invalid payloads raise SchemaValidationError ( HTTP 422).
(b) Departments whose probe fails are skipped; successful ones commit.
(c) Each department is processed atomically - failure of one does not
 affect others.
(d) dry_run=True never writes any state (Vault or DB).
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_AUTOMATION_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "services" / "automation-service"
)
_AUTOMATION_SRC: Final[Path] = _AUTOMATION_ROOT / "src"

for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

_LIB_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "db-shared" / "src",
    _PLATFORM_ROOT / "libs" / "vault_client" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)

# ---------------------------------------------------------------------------
# Import the bulk import service module directly to avoid the circular
# import triggered by ``automation_service.__init__``  ``.app`` chain.
# We use importlib to load the module from its file path.
# ---------------------------------------------------------------------------
import importlib.util as _ilu

def _import_module_from_path(module_name: str, file_path: Path) -> Any:
    """Import a module directly from its file path."""
    spec = _ilu.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

# Pre-load the staging module (dependency of bulk import service)
# so it's available when the service module imports it.
_staging_mod = _import_module_from_path(
    "automation_service.staging",
    _AUTOMATION_SRC / "automation_service" / "staging.py",
)

# Pre-load the probe module - but we need to handle its own imports.
# The probe module imports from typing_extensions and other stdlib;
# it also uses a Protocol class. We load it carefully.
_probe_mod = _import_module_from_path(
    "automation_service.probe",
    _AUTOMATION_SRC / "automation_service" / "probe.py",
)

# Now load the bulk import service module
_bulk_import_mod = _import_module_from_path(
    "services.dept_bulk_import_service",
    _AUTOMATION_SRC / "services" / "dept_bulk_import_service.py",
)

BulkImportResult = _bulk_import_mod.BulkImportResult
BulkImportService = _bulk_import_mod.BulkImportService
DeptImportOutcome = _bulk_import_mod.DeptImportOutcome
SchemaValidationError = _bulk_import_mod.SchemaValidationError

VALID_SERVICES = _staging_mod.VALID_SERVICES


# ---------------------------------------------------------------------------
# Fake implementations
# ---------------------------------------------------------------------------


class FakeVaultClient:
    """In-memory Vault client that tracks all writes and deletes."""

    def __init__(
        self,
        *,
        fail_write_for: set[str] | None = None,
        fail_read_for: set[str] | None = None,
        initial_data: dict[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.backend = "local-dev"
        self._store: dict[str, Mapping[str, str]] = dict(initial_data or {})
        self._fail_write_for = fail_write_for or set()
        self._fail_read_for = fail_read_for or set()
        self.writes: list[tuple[str, Mapping[str, str]]] = []
        self.deletes: list[str] = []

    def read(self, path: Any) -> Mapping[str, str]:
        raw = path.raw if hasattr(path, "raw") else str(path)
        if raw in self._fail_read_for:
            raise KeyError(f"No secret at {raw}")
        if raw in self._store:
            return self._store[raw]
        raise KeyError(f"No secret at {raw}")

    def write(self, path: Any, data: Mapping[str, str]) -> None:
        raw = path.raw if hasattr(path, "raw") else str(path)
        if raw in self._fail_write_for:
            raise RuntimeError(f"Vault write failed for {raw}")
        self._store[raw] = dict(data)
        self.writes.append((raw, data))

    def delete(self, path: Any) -> None:
        raw = path.raw if hasattr(path, "raw") else str(path)
        self._store.pop(raw, None)
        self.deletes.append(raw)


class FakeAsyncConnection:
    """In-memory async DB connection that records executed queries."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> None:
        if self._fail:
            raise RuntimeError("DB execute failed")
        self.executions.append((query, args))


class FakeProbeClient:
    """Fake Atlassian probe client with configurable per-dept outcomes.

 The ``fail_for`` set contains dept_ids whose probes should fail.
 """

    def __init__(
        self,
        *,
        fail_for: set[str] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self._fail_for = fail_for or set()
        self._raise_for = raise_for or set()
        self.calls: list[tuple[str, str]] = []

    async def jira_myself(self, cred: Any) -> dict[str, Any]:
        return {"accountId": "fake-account-id-12345678"}

    async def bitbucket_user(self, cred: Any) -> dict[str, Any]:
        return {"account_id": "fake-bb-account-id"}

    async def confluence_user(self, cred: Any) -> dict[str, Any]:
        return {"accountId": "fake-conf-account-id"}


@dataclass
class FakeProbeResult:
    """Minimal probe result for testing."""

    state: str
    error_message: str | None = None


class FakeProbeRunner:
    """Probe runner that returns configurable results per dept_id."""

    def __init__(
        self,
        *,
        fail_for: set[str] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self._fail_for = fail_for or set()
        self._raise_for = raise_for or set()

    async def run(
        self,
        dept_id: str,
        service: str,
        credential: Any,
        targets: Any = None,
    ) -> FakeProbeResult:
        if dept_id in self._raise_for:
            raise ConnectionError(f"Probe connection failed for {dept_id}")
        if dept_id in self._fail_for:
            return FakeProbeResult(
                state="read_failed",
                error_message=f"Probe failed for {dept_id}/{service}",
            )
        return FakeProbeResult(state="ok")


class FakeAuditLogger:
    """Audit logger that records events in memory."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Patched BulkImportService for testing
# ---------------------------------------------------------------------------


class _TestableBulkImportService(BulkImportService):
    """Subclass that overrides probe execution with fake results.

 This allows us to control probe outcomes per-department without
 needing real Atlassian API access.
 """

    __test__ = False  # Prevent pytest from collecting this class

    def __init__(
        self,
        *,
        vault: FakeVaultClient,
        connection_factory: Any,
        probe_client: Any,
        audit_logger: FakeAuditLogger,
        schema: dict[str, Any],
        fail_probe_for: set[str] | None = None,
        raise_probe_for: set[str] | None = None,
    ) -> None:
        super().__init__(
            vault=vault,  # type: ignore[arg-type]
            connection_factory=connection_factory,
            probe_client=probe_client,
            audit_logger=audit_logger,  # type: ignore[arg-type]
            schema=schema,
        )
        self._fail_probe_for = fail_probe_for or set()
        self._raise_probe_for = raise_probe_for or set()

    async def _run_probe(
        self,
        *,
        dept_id: str,
        service: Any,
        credential: Any,
    ) -> Any:
        """Override probe to use fake results."""
        if dept_id in self._raise_probe_for:
            raise ConnectionError(f"Probe connection failed for {dept_id}")
        if dept_id in self._fail_probe_for:
            return FakeProbeResult(
                state="read_failed",
                error_message=f"Probe failed for {dept_id}/{service}",
            )
        return FakeProbeResult(state="ok")


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department ID strategy - matches ^[a-z][a-z0-9-]{1,30}$
_dept_id_strategy = st.from_regex(r"^[a-z][a-z0-9\-]{1,10}$", fullmatch=True)

#: Jira project key strategy - matches ^[A-Z][A-Z0-9_]{1,9}$
_jira_key_strategy = st.from_regex(r"^[A-Z][A-Z0-9]{1,5}$", fullmatch=True)

#: Display name strategy
_display_name_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "Nd", "Zs")),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")


@st.composite
def _valid_dept_entry(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a single valid department entry matching the schema."""
    dept_id = draw(_dept_id_strategy)
    display_name = draw(_display_name_strategy)
    jira_keys = draw(
        st.lists(_jira_key_strategy, min_size=1, max_size=3, unique=True)
    )

    # Generate bot config - at least one service required
    services_to_include = draw(
        st.lists(
            st.sampled_from(["jira", "bitbucket", "confluence"]),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )

    bot: dict[str, Any] = {}
    for svc in services_to_include:
        bot[svc] = {
            "credential_ref": f"vault:atlassian/{dept_id}/{svc}",
            "account_id": None,
            "username": f"bot-{dept_id}@example.com",
        }

    return {
        "id": dept_id,
        "display_name": display_name,
        "jira_project_keys": jira_keys,
        "bot": bot,
        "budget_caps": {
            "weekly_usd_dept": 100.0,
            "weekly_usd_user": 50.0,
            "monthly_usd_dept": 400.0,
            "monthly_usd_user": 200.0,
        },
    }


@st.composite
def _valid_bulk_import_payload(
    draw: st.DrawFn,
    min_depts: int = 1,
    max_depts: int = 20,
) -> dict[str, Any]:
    """Generate a valid bulk import JSON payload with 1-20 departments."""
    depts = draw(
        st.lists(
            _valid_dept_entry(),
            min_size=min_depts,
            max_size=max_depts,
        )
    )
    # Ensure unique dept_ids
    seen: set[str] = set()
    unique_depts: list[dict[str, Any]] = []
    for dept in depts:
        if dept["id"] not in seen:
            seen.add(dept["id"])
            unique_depts.append(dept)
    assume(len(unique_depts) >= min_depts)
    return {"version": 1, "departments": unique_depts}


@st.composite
def _invalid_schema_payload(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a payload that fails schema validation."""
    variant = draw(st.sampled_from([
        "missing_version",
        "wrong_version_type",
        "missing_departments",
        "invalid_dept_id",
        "missing_bot",
        "empty_jira_keys",
    ]))

    if variant == "missing_version":
        return {"departments": []}
    elif variant == "wrong_version_type":
        return {"version": "1", "departments": []}
    elif variant == "missing_departments":
        return {"version": 1}
    elif variant == "invalid_dept_id":
        return {
            "version": 1,
            "departments": [{
                "id": "INVALID_ID",  # uppercase not allowed
                "display_name": "Test",
                "jira_project_keys": ["TEST"],
                "bot": {"jira": {"credential_ref": "vault:atlassian/test/jira"}},
                "budget_caps": {
                    "weekly_usd_dept": 100,
                    "weekly_usd_user": 50,
                    "monthly_usd_dept": 400,
                    "monthly_usd_user": 200,
                },
            }],
        }
    elif variant == "missing_bot":
        return {
            "version": 1,
            "departments": [{
                "id": "test-dept",
                "display_name": "Test",
                "jira_project_keys": ["TEST"],
                "budget_caps": {
                    "weekly_usd_dept": 100,
                    "weekly_usd_user": 50,
                    "monthly_usd_dept": 400,
                    "monthly_usd_user": 200,
                },
            }],
        }
    else:  # empty_jira_keys
        return {
            "version": 1,
            "departments": [{
                "id": "test-dept",
                "display_name": "Test",
                "jira_project_keys": [],  # minItems: 1 violated
                "bot": {"jira": {"credential_ref": "vault:atlassian/test/jira"}},
                "budget_caps": {
                    "weekly_usd_dept": 100,
                    "weekly_usd_user": 50,
                    "monthly_usd_dept": 400,
                    "monthly_usd_user": 200,
                },
            }],
        }


# ---------------------------------------------------------------------------
# Helper: load the real schema
# ---------------------------------------------------------------------------

def _load_schema() -> dict[str, Any]:
    """Load the departments.schema.json from the platform config dir."""
    schema_path = _PLATFORM_ROOT / "config" / "departments.schema.json"
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helper: create a testable service instance
# ---------------------------------------------------------------------------

def _make_service(
    *,
    fail_probe_for: set[str] | None = None,
    raise_probe_for: set[str] | None = None,
    fail_db: bool = False,
    fail_vault_write_for: set[str] | None = None,
    initial_vault_data: dict[str, Mapping[str, str]] | None = None,
) -> tuple[_TestableBulkImportService, FakeVaultClient, FakeAsyncConnection, FakeAuditLogger]:
    """Create a _TestableBulkImportService with fake dependencies."""
    vault = FakeVaultClient(
        fail_write_for=fail_vault_write_for,
        initial_data=initial_vault_data,
    )
    conn = FakeAsyncConnection(fail=fail_db)
    audit = FakeAuditLogger()

    # Seed vault with credential data for probes
    # The service reads from credential_ref during probe resolution
    initial_cred_data: dict[str, str] = {
        "url": "https://acme.atlassian.net",
        "username": "bot@example.com",
        "personal_token": "fake-token-12345",
    }

    async def connection_factory() -> FakeAsyncConnection:
        return conn

    service = _TestableBulkImportService(
        vault=vault,
        connection_factory=connection_factory,
        probe_client=FakeProbeClient(),
        audit_logger=audit,
        schema=_load_schema(),
        fail_probe_for=fail_probe_for,
        raise_probe_for=raise_probe_for,
    )

    return service, vault, conn, audit


# ---------------------------------------------------------------------------
# Schema-invalid payloads  SchemaValidationError (HTTP 422)
# ---------------------------------------------------------------------------


class TestSchemaInvalidReturns422:
    """When the uploaded JSON fails ``departments.schema.json`` validation,
 the service raises ``SchemaValidationError`` which the router maps
 to HTTP 422.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_invalid_schema_payload())
    def test_invalid_schema_raises_validation_error(
        self, payload: dict[str, Any]
    ) -> None:
        """: Schema-invalid JSON raises SchemaValidationError."""
        service, vault, conn, audit = _make_service()

        with pytest.raises(SchemaValidationError) as exc_info:
            asyncio.run(
                service.bulk_import(
                    json.dumps(payload).encode("utf-8"),
                    dry_run=False,
                )
            )

        # The error should contain validation error messages
        assert len(exc_info.value.errors) > 0

        # No Vault writes should have occurred
        assert len(vault.writes) == 0

        # No DB executions should have occurred
        assert len(conn.executions) == 0

    def test_malformed_json_raises_validation_error(self) -> None:
        """: Non-JSON content raises SchemaValidationError."""
        service, vault, conn, audit = _make_service()

        with pytest.raises(SchemaValidationError) as exc_info:
            asyncio.run(
                service.bulk_import(
                    b"this is not json {{{",
                    dry_run=False,
                )
            )

        assert len(exc_info.value.errors) > 0
        assert "Invalid JSON" in exc_info.value.errors[0]

    def test_empty_bytes_raises_validation_error(self) -> None:
        """: Empty content raises SchemaValidationError."""
        service, vault, conn, audit = _make_service()

        with pytest.raises(SchemaValidationError):
            asyncio.run(
                service.bulk_import(b"", dry_run=False)
            )


# ---------------------------------------------------------------------------
# Probe-failing departments are skipped
# ---------------------------------------------------------------------------


class TestProbeFailSkipsDept:
    """Departments whose connectivity probe fails are skipped (not
 imported). They appear in the ``failed`` list of the result with
 an error description. Successfully probed departments are still
 imported.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=2, max_depts=10))
    def test_probe_fail_skips_dept_imports_others(
        self, payload: dict[str, Any]
    ) -> None:
        """: Departments with failed probes are skipped; others
 are imported successfully."""
        departments = payload["departments"]
        assume(len(departments) >= 2)

        # First half of departments will have probe failures
        midpoint = len(departments) // 2
        fail_ids = {dept["id"] for dept in departments[:midpoint]}

        service, vault, conn, audit = _make_service(fail_probe_for=fail_ids)

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        assert isinstance(result, BulkImportResult)
        assert result.total == len(departments)

        # Failed departments should be in the failed list
        failed_ids = {o.dept_id for o in result.failed}
        for dept_id in fail_ids:
            assert dept_id in failed_ids

        # Successful departments should be in the imported list
        imported_ids = {o.dept_id for o in result.imported}
        success_ids = {dept["id"] for dept in departments[midpoint:]}
        for dept_id in success_ids:
            assert dept_id in imported_ids

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=10))
    def test_all_probes_fail_results_in_all_failed(
        self, payload: dict[str, Any]
    ) -> None:
        """: When all probes fail, all departments are in the
 failed list and none are imported."""
        departments = payload["departments"]
        all_ids = {dept["id"] for dept in departments}

        service, vault, conn, audit = _make_service(fail_probe_for=all_ids)

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        assert len(result.imported) == 0
        assert len(result.failed) == len(departments)

        # No DB commits should have occurred for departments
        dept_inserts = [
            e for e in conn.executions
            if "automation.departments" in e[0]
        ]
        assert len(dept_inserts) == 0

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=5))
    def test_probe_exception_treated_as_failure(
        self, payload: dict[str, Any]
    ) -> None:
        """: When a probe raises an exception (network error),
 the department is treated as failed, not as a crash."""
        departments = payload["departments"]
        raise_ids = {dept["id"] for dept in departments}

        service, vault, conn, audit = _make_service(raise_probe_for=raise_ids)

        # Should NOT raise - exceptions are caught per-dept
        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        assert len(result.imported) == 0
        assert len(result.failed) == len(departments)


# ---------------------------------------------------------------------------
# Atomic per-department behavior
# ---------------------------------------------------------------------------


class TestAtomicPerDeptBehavior:
    """Each department is processed atomically: failure of one department
 does not affect the processing of others. Staging paths are cleaned
 up on failure.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=2, max_depts=10))
    def test_failure_of_one_dept_does_not_affect_others(
        self, payload: dict[str, Any]
    ) -> None:
        """: When one department fails, others are still
 processed independently."""
        departments = payload["departments"]
        assume(len(departments) >= 2)

        # Only the first department fails
        fail_ids = {departments[0]["id"]}

        service, vault, conn, audit = _make_service(fail_probe_for=fail_ids)

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        # The failed dept should be in failed list
        failed_ids = {o.dept_id for o in result.failed}
        assert departments[0]["id"] in failed_ids

        # All other departments should be imported
        expected_success = len(departments) - 1
        assert len(result.imported) == expected_success

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=10))
    def test_all_probes_succeed_all_imported(
        self, payload: dict[str, Any]
    ) -> None:
        """: When all probes succeed, all departments are
 imported successfully."""
        departments = payload["departments"]

        service, vault, conn, audit = _make_service()

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        assert result.total == len(departments)
        assert len(result.imported) == len(departments)
        assert len(result.failed) == 0

        # Each imported dept should have DB commits
        dept_inserts = [
            e for e in conn.executions
            if "automation.departments" in e[0]
        ]
        assert len(dept_inserts) == len(departments)

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=2, max_depts=8))
    def test_staging_cleanup_on_probe_failure(
        self, payload: dict[str, Any]
    ) -> None:
        """: When a department's probe fails, its staging Vault
 paths are cleaned up (deleted)."""
        departments = payload["departments"]
        assume(len(departments) >= 2)

        # First department fails probe
        fail_ids = {departments[0]["id"]}

        service, vault, conn, audit = _make_service(fail_probe_for=fail_ids)

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        # Staging paths for the failed dept should have been deleted
        # (cleanup_staging is called)
        staging_deletes = [
            d for d in vault.deletes if "_staging" in d
        ]
        # At least one staging path should have been cleaned up
        # for the failed department
        assert len(staging_deletes) > 0

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=10))
    def test_result_total_matches_input_count(
        self, payload: dict[str, Any]
    ) -> None:
        """: The result's total field always matches the number
 of departments in the input, regardless of outcomes."""
        departments = payload["departments"]

        # Random subset fails
        import random
        fail_count = random.randint(0, len(departments))
        fail_ids = {dept["id"] for dept in departments[:fail_count]}

        service, vault, conn, audit = _make_service(fail_probe_for=fail_ids)

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=False,
            )
        )

        assert result.total == len(departments)
        assert len(result.imported) + len(result.failed) == len(departments)


# ---------------------------------------------------------------------------
# dry_run=True never changes state
# ---------------------------------------------------------------------------


class TestDryRunNoStateChange:
    """When ``dry_run=True``, the service validates the schema and
 simulates probes but NEVER writes to Vault or DB. The result
 contains validated departments but no imported ones.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=20))
    def test_dry_run_no_vault_writes(
        self, payload: dict[str, Any]
    ) -> None:
        """: dry_run=True produces zero Vault writes."""
        service, vault, conn, audit = _make_service()

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=True,
            )
        )

        # No Vault writes should have occurred (only audit writes)
        assert len(vault.writes) == 0

        # Result should indicate dry_run
        assert result.dry_run is True

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=20))
    def test_dry_run_no_db_commits(
        self, payload: dict[str, Any]
    ) -> None:
        """: dry_run=True produces zero DB commits."""
        service, vault, conn, audit = _make_service()

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=True,
            )
        )

        # No DB executions should have occurred
        assert len(conn.executions) == 0

        # No departments should be in the imported list
        assert len(result.imported) == 0

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=20))
    def test_dry_run_returns_validated_departments(
        self, payload: dict[str, Any]
    ) -> None:
        """: dry_run=True returns validated departments in the
 validated list (not imported)."""
        departments = payload["departments"]
        service, vault, conn, audit = _make_service()

        result = asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=True,
            )
        )

        assert result.total == len(departments)
        # All valid departments should be in validated list
        assert len(result.validated) + len(result.failed) == len(departments)
        # None should be imported
        assert len(result.imported) == 0

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=10))
    def test_dry_run_no_vault_deletes(
        self, payload: dict[str, Any]
    ) -> None:
        """: dry_run=True produces zero Vault deletes (no staging
 cleanup needed since nothing was staged)."""
        service, vault, conn, audit = _make_service()

        asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=True,
            )
        )

        # No Vault deletes should have occurred
        assert len(vault.deletes) == 0

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_valid_bulk_import_payload(min_depts=1, max_depts=10))
    def test_dry_run_still_writes_audit(
        self, payload: dict[str, Any]
    ) -> None:
        """: dry_run=True still writes audit events (start +
 complete) for observability, but no state-changing operations."""
        service, vault, conn, audit = _make_service()

        asyncio.run(
            service.bulk_import(
                json.dumps(payload).encode("utf-8"),
                dry_run=True,
            )
        )

        # Audit events should still be written (start + complete)
        assert len(audit.events) >= 2
        actions = [e.action for e in audit.events]
        assert "dept_bulk_import_started" in actions
        assert "dept_bulk_import_completed" in actions
