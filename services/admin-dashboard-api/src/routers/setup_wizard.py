"""Admin Dashboard Setup Wizard API endpoints.

Provides step-by-step guided setup for platform services.

Requirements: 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
"""
from __future__ import annotations
import asyncio
import logging
from enum import Enum
from typing import Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

class StepStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"

class SetupStep(BaseModel):
    name: str
    status: StepStatus = StepStatus.PENDING
    config_data: dict[str, Any] | None = None
    error: str | None = None

STEP_ORDER = ["vault", "postgresql", "temporal", "mcp_server", "workers", "services", "add_first_department"]

# In-memory state (production uses setup_wizard_state table)
_wizard_state: dict[str, SetupStep] = {
    name: SetupStep(name=name) for name in STEP_ORDER
}

class ConnectionTestRequest(BaseModel):
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    token: str | None = None

class ConnectionTestResult(BaseModel):
    success: bool
    error: str | None = None
    suggestion: str | None = None

@router.get("/status")
async def get_setup_status():
    steps = [_wizard_state[name].model_dump() for name in STEP_ORDER]
    first_incomplete = next((s["name"] for s in steps if s["status"] != "completed"), None)
    return {"steps": steps, "current_step": first_incomplete, "all_complete": first_incomplete is None}

@router.post("/add_first_department/check")
async def check_add_first_department(request: Request):
    """Check if at least one active department exists.

    Queries ``automation.departments`` for rows with ``mode='active'``.
    If at least one exists, marks the ``add_first_department`` step as
    completed and returns the updated status. Otherwise returns pending.

    Requirements: 5.3
    """
    pool = _get_pg_pool(request)
    if pool is None:
        # No DB pool available - cannot verify, return current state
        step = _wizard_state["add_first_department"]
        return {"step": "add_first_department", "status": step.status.value}

    row = await pool.fetchrow(
        "SELECT 1 FROM automation.departments WHERE mode = 'active' LIMIT 1"
    )
    if row is not None:
        _wizard_state["add_first_department"] = SetupStep(
            name="add_first_department", status=StepStatus.COMPLETED
        )
        return {"step": "add_first_department", "status": "completed"}

    return {"step": "add_first_department", "status": "pending"}


@router.post("/{step_name}/test")
async def test_connection(step_name: str, request: ConnectionTestRequest) -> ConnectionTestResult:
    if step_name not in _wizard_state:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_name}")

    try:
        # Simulate connection test with 10s timeout
        await asyncio.wait_for(_simulate_connection_test(step_name, request), timeout=10.0)
        return ConnectionTestResult(success=True)
    except asyncio.TimeoutError:
        return ConnectionTestResult(success=False, error="Connection timeout (10s)", suggestion="Check host and port")
    except Exception as e:
        return ConnectionTestResult(success=False, error=str(e), suggestion="Verify credentials")

@router.post("/{step_name}/complete")
async def complete_step(step_name: str):
    if step_name not in _wizard_state:
        raise HTTPException(status_code=404, detail=f"Unknown step: {step_name}")
    _wizard_state[step_name] = SetupStep(name=step_name, status=StepStatus.COMPLETED)
    return {"step": step_name, "status": "completed"}


def _get_pg_pool(request: Request) -> Any | None:
    """Return the asyncpg pool from ``app.state.pg_pool``, or None."""
    return getattr(request.app.state, "pg_pool", None)

async def _simulate_connection_test(step_name: str, request: ConnectionTestRequest) -> None:
    await asyncio.sleep(0.1)  # Placeholder for actual connection test
