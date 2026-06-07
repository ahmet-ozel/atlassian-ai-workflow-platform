"""Service Test Results API endpoints.

Provides structured test result parsing and **persistent** history
tracking.

Run results are written to ``automation.test_runs`` (migration
``011_test_runs.sql``) so the admin dashboard's "Servis testleri"
panel keeps a durable pass/fail trend across admin-dashboard-api
restarts. When the Postgres pool is unavailable the module degrades
to an in-memory ring buffer so the panel still renders.

"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/services", tags=["test-results"])

MAX_TEST_HISTORY = 20
MAX_ERROR_LENGTH = 500
#: Last N bytes of stdout persisted with each run - bounded so a chatty
#: pytest run does not bloat the row.
OUTPUT_TAIL_BYTES = 4096

# In-memory fallback store (used only when the Postgres pool is absent).
_test_history: dict[str, list[dict[str, Any]]] = defaultdict(list)

# pytest prints a summary line like "=== 5 passed, 2 failed in 1.2s ===".
_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")
_ERROR_RE = re.compile(r"(\d+)\s+error")
_SKIPPED_RE = re.compile(r"(\d+)\s+skipped")


class TestCaseResult(BaseModel):
    name: str
    status: str  # "pass" or "fail"
    duration_ms: int | None = None
    error: str | None = None


class TestRunResult(BaseModel):
    service_name: str
    total_tests: int
    passed: int
    failed: int
    duration_ms: int | None = None
    test_cases: list[TestCaseResult]
    raw_output: str | None = None
    is_structured: bool = True


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def _parse_test_output(output: str) -> tuple[list[TestCaseResult], bool]:
    """Parse a structured JSON test report into TestCaseResult rows."""
    try:
        data = json.loads(output)
        cases: list[TestCaseResult] = []
        if isinstance(data, dict) and "tests" in data:
            for test in data["tests"]:
                error_msg = test.get("error", "")
                if error_msg and len(error_msg) > MAX_ERROR_LENGTH:
                    error_msg = error_msg[:MAX_ERROR_LENGTH]
                cases.append(
                    TestCaseResult(
                        name=test.get("name", "unknown"),
                        status="pass"
                        if test.get("passed", test.get("status") == "pass")
                        else "fail",
                        duration_ms=test.get("duration_ms"),
                        error=error_msg or None,
                    )
                )
        return cases, True
    except (json.JSONDecodeError, KeyError, TypeError):
        return [], False


def summarize_pytest_output(output: str) -> dict[str, int]:
    """Extract passed / failed / skipped counts from pytest stdout.

    Falls back to zeros when no summary line is present. ``error``
    counts (collection errors) are folded into ``failed`` because the
    dashboard's pass/fail trend treats any non-pass as a failure.
    """
    passed = sum(int(m) for m in _PASSED_RE.findall(output))
    failed = sum(int(m) for m in _FAILED_RE.findall(output))
    errors = sum(int(m) for m in _ERROR_RE.findall(output))
    skipped = sum(int(m) for m in _SKIPPED_RE.findall(output))
    failed_total = failed + errors
    return {
        "passed": passed,
        "failed": failed_total,
        "skipped": skipped,
        "total": passed + failed_total + skipped,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def record_test_run(
    request: Request,
    *,
    service_name: str,
    exit_code: int,
    output: str,
    duration_ms: int | None,
    triggered_by: str = "system",
) -> dict[str, Any]:
    """Persist a single service test run to ``automation.test_runs``.

    Called by ``test_runner.py`` after a non-streaming run completes.
    When the Postgres pool is missing the row is appended to the
    in-memory ring buffer instead so history still works in degraded
    deployments (and in unit tests).

    Returns the recorded row as a plain dict (also used by the API
    response shape).
    """
    counts = summarize_pytest_output(output)
    status = "pass" if exit_code == 0 else "fail"
    output_tail = output[-OUTPUT_TAIL_BYTES:] if output else ""

    row: dict[str, Any] = {
        "service_name": service_name,
        "exit_code": exit_code,
        "status": status,
        "total_tests": counts["total"],
        "passed": counts["passed"],
        "failed": counts["failed"],
        "duration_ms": duration_ms,
        "output_tail": output_tail,
        "triggered_by": triggered_by,
    }

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                record = await conn.fetchrow(
                    """
                    INSERT INTO automation.test_runs (
                        service_name, exit_code, status, total_tests,
                        passed, failed, duration_ms, output_tail,
                        triggered_by
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id, created_at
                    """,
                    service_name,
                    exit_code,
                    status,
                    counts["total"],
                    counts["passed"],
                    counts["failed"],
                    duration_ms,
                    output_tail,
                    triggered_by,
                )
            row["id"] = record["id"]
            row["created_at"] = record["created_at"].isoformat()
            return row
        except Exception as exc:  # noqa: BLE001 - degrade to memory store
            _logger.warning(
                "record_test_run: DB insert failed for %s, "
                "falling back to in-memory store: %s",
                service_name,
                exc,
            )

    # In-memory fallback.
    from datetime import datetime, timezone

    row["id"] = f"mem-{len(_test_history[service_name]) + 1}"
    row["created_at"] = datetime.now(timezone.utc).isoformat()
    _test_history[service_name].append(row)
    # Bound the buffer.
    if len(_test_history[service_name]) > MAX_TEST_HISTORY:
        _test_history[service_name] = _test_history[service_name][-MAX_TEST_HISTORY:]
    return row


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{service_name}/tests/run")
async def run_tests(service_name: str) -> TestRunResult:
    """Placeholder structured-run endpoint.

    The real test execution lives in ``test_runner.py``
    (``POST /admin/services/{service_name}/test``) which streams output
    and persists a summary via :func:`record_test_run`. This endpoint
    is kept for the structured-JSON contract used by older callers.
    """
    return TestRunResult(
        service_name=service_name,
        total_tests=0,
        passed=0,
        failed=0,
        test_cases=[],
        is_structured=True,
    )


@router.get("/{service_name}/tests/history")
async def get_test_history(service_name: str, request: Request) -> dict[str, Any]:
    """Return the persisted test-run history for a service.

    Reads ``automation.test_runs`` newest-first. Falls back to the
    in-memory ring buffer when the Postgres pool is unavailable so the
    panel always renders.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, service_name, exit_code, status,
                           total_tests, passed, failed, duration_ms,
                           output_tail, triggered_by, created_at
                    FROM automation.test_runs
                    WHERE service_name = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    service_name,
                    MAX_TEST_HISTORY,
                )
            runs = [
                {
                    "id": r["id"],
                    "service_name": r["service_name"],
                    "exit_code": r["exit_code"],
                    "status": r["status"],
                    "total_tests": r["total_tests"],
                    "passed": r["passed"],
                    "failed": r["failed"],
                    "duration_ms": r["duration_ms"],
                    "output_tail": r["output_tail"],
                    "triggered_by": r["triggered_by"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ]
            return {
                "service_name": service_name,
                "runs": runs,
                "total_runs": len(runs),
                "source": "postgres",
            }
        except Exception as exc:  # noqa: BLE001 - degrade to memory store
            _logger.warning(
                "get_test_history: DB read failed for %s: %s",
                service_name,
                exc,
            )

    history = _test_history.get(service_name, [])
    return {
        "service_name": service_name,
        "runs": list(reversed(history[-MAX_TEST_HISTORY:])),
        "total_runs": len(history),
        "source": "memory",
    }
