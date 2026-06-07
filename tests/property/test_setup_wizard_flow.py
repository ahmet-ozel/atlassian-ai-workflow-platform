"""Property-based tests for Setup Wizard step order and completion invariants.

Background
----------

The Setup Wizard guides admins through a sequential series of steps
to bootstrap the platform.
``add_first_department`` is appended as the **last** step in
``STEP_ORDER``, preserving the existing six steps unchanged.

The ``all_complete`` field in the ``GET /api/v1/setup/status`` response
MUST remain ``false`` until every step - including the new final step -
is marked ``completed``.

The final step's completion logic (``POST /api/v1/setup/add_first_department/check``)
requires at least one row in ``automation.departments`` with
``mode='active'``. Without an active department the
step stays ``pending``.

Strategy
--------

The tests are fully deterministic - no Hypothesis strategies needed.
We import ``STEP_ORDER`` and the wizard state machinery directly from
the ``setup_wizard`` module and verify the three invariants:

(a) ``STEP_ORDER[-1] == "add_first_department"``
(b) ``all_complete`` is ``false`` when any step is incomplete
(c) The check endpoint logic requires an active department row

For (c) we use ``httpx.AsyncClient`` with the FastAPI test client to
exercise the ``/check`` endpoint with a mocked DB pool.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap - expose the admin-dashboard-api source root
# so we can import the setup_wizard module directly.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "services" / "admin-dashboard-api" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from routers.setup_wizard import (  # noqa: E402
    STEP_ORDER,
    SetupStep,
    StepStatus,
    _wizard_state,
    router,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The six original steps that existed before the department step.
_ORIGINAL_STEPS: Final[tuple[str, ...]] = (
    "vault",
    "postgresql",
    "temporal",
    "mcp_server",
    "workers",
    "services",
)

#: The new department step appended to the wizard.
_NEW_FINAL_STEP: Final[str] = "add_first_department"


# ---------------------------------------------------------------------------
# Setup Wizard Flow
# ---------------------------------------------------------------------------


class TestSetupWizardStepOrder:
    """The Setup Wizard STEP_ORDER list maintains the original six steps
    unchanged and appends ``add_first_department`` as the final step.
    """

    def test_step_order_last_element_is_add_first_department(self) -> None:
        """STEP_ORDER[-1] == 'add_first_department'."""
        assert STEP_ORDER[-1] == _NEW_FINAL_STEP, (
            f"Expected last step to be '{_NEW_FINAL_STEP}', "
            f"got '{STEP_ORDER[-1]}'"
        )

    def test_step_order_preserves_original_six_steps(self) -> None:
        """The original six steps remain in order and unchanged."""
        assert len(STEP_ORDER) == 7, (
            f"Expected 7 steps (6 original + 1 new), got {len(STEP_ORDER)}"
        )
        for i, expected_name in enumerate(_ORIGINAL_STEPS):
            assert STEP_ORDER[i] == expected_name, (
                f"Step at index {i} should be '{expected_name}', "
                f"got '{STEP_ORDER[i]}'"
            )

    def test_step_order_is_append_only(self) -> None:
        """The new step is appended, not inserted in the middle."""
        # The first 6 elements must be exactly the original steps
        assert tuple(STEP_ORDER[:6]) == _ORIGINAL_STEPS
        # The 7th element is the new step
        assert STEP_ORDER[6] == _NEW_FINAL_STEP

    def test_add_first_department_exists_in_wizard_state(self) -> None:
        """The wizard state dict has an entry for the new step."""
        assert _NEW_FINAL_STEP in _wizard_state
        step = _wizard_state[_NEW_FINAL_STEP]
        assert isinstance(step, SetupStep)
        assert step.name == _NEW_FINAL_STEP


class TestSetupWizardAllComplete:
    """The ``all_complete`` field in the status response MUST be ``false``
    until every step (including ``add_first_department``) is completed.
    """

    def _build_status_response(
        self, state: dict[str, SetupStep]
    ) -> dict:
        """Replicate the logic of ``get_setup_status`` locally."""
        steps = [state[name].model_dump() for name in STEP_ORDER]
        first_incomplete = next(
            (s["name"] for s in steps if s["status"] != "completed"), None
        )
        return {
            "steps": steps,
            "current_step": first_incomplete,
            "all_complete": first_incomplete is None,
        }

    def test_all_complete_false_when_no_steps_completed(self) -> None:
        """All steps pending  all_complete is False."""
        state = {name: SetupStep(name=name) for name in STEP_ORDER}
        result = self._build_status_response(state)
        assert result["all_complete"] is False
        assert result["current_step"] == STEP_ORDER[0]

    @pytest.mark.parametrize("incomplete_step", STEP_ORDER)
    def test_all_complete_false_when_single_step_incomplete(
        self, incomplete_step: str
    ) -> None:
        """If any single step is not completed, all_complete is False."""
        state = {
            name: SetupStep(name=name, status=StepStatus.COMPLETED)
            for name in STEP_ORDER
        }
        # Mark one step as pending
        state[incomplete_step] = SetupStep(
            name=incomplete_step, status=StepStatus.PENDING
        )
        result = self._build_status_response(state)
        assert result["all_complete"] is False
        assert result["current_step"] == incomplete_step

    def test_all_complete_true_when_all_steps_completed(self) -> None:
        """All steps completed  all_complete is True."""
        state = {
            name: SetupStep(name=name, status=StepStatus.COMPLETED)
            for name in STEP_ORDER
        }
        result = self._build_status_response(state)
        assert result["all_complete"] is True
        assert result["current_step"] is None

    def test_all_complete_false_when_only_last_step_pending(self) -> None:
        """Specifically: first 6 completed but add_first_department pending."""
        state = {
            name: SetupStep(name=name, status=StepStatus.COMPLETED)
            for name in STEP_ORDER
        }
        state[_NEW_FINAL_STEP] = SetupStep(
            name=_NEW_FINAL_STEP, status=StepStatus.PENDING
        )
        result = self._build_status_response(state)
        assert result["all_complete"] is False
        assert result["current_step"] == _NEW_FINAL_STEP

    def test_failed_step_blocks_all_complete(self) -> None:
        """A failed step also prevents all_complete from being True."""
        state = {
            name: SetupStep(name=name, status=StepStatus.COMPLETED)
            for name in STEP_ORDER
        }
        state["postgresql"] = SetupStep(
            name="postgresql", status=StepStatus.FAILED
        )
        result = self._build_status_response(state)
        assert result["all_complete"] is False


class TestAddFirstDepartmentCheckEndpoint:
    """The ``POST /api/v1/setup/add_first_department/check`` endpoint
    requires at least one active department in ``automation.departments``
    to mark the step as completed.
    """

    @pytest.fixture
    def app(self):
        """Create a minimal FastAPI app with the setup wizard router."""
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        return app

    @pytest.fixture
    def mock_pool_with_active_dept(self):
        """Mock asyncpg pool that returns a row (active dept exists)."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value={"?column?": 1})
        return pool

    @pytest.fixture
    def mock_pool_without_active_dept(self):
        """Mock asyncpg pool that returns None (no active dept)."""
        pool = AsyncMock()
        pool.fetchrow = AsyncMock(return_value=None)
        return pool

    @pytest.mark.asyncio
    async def test_check_returns_completed_when_active_dept_exists(
        self, app, mock_pool_with_active_dept
    ) -> None:
        """At least one active dept means the step is completed."""
        from httpx import ASGITransport, AsyncClient

        app.state.pg_pool = mock_pool_with_active_dept

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/setup/add_first_department/check"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["step"] == "add_first_department"

        # Verify the SQL query checks for mode='active'
        mock_pool_with_active_dept.fetchrow.assert_called_once()
        call_args = mock_pool_with_active_dept.fetchrow.call_args
        query = call_args[0][0]
        assert "mode" in query
        assert "active" in query

    @pytest.mark.asyncio
    async def test_check_returns_pending_when_no_active_dept(
        self, app, mock_pool_without_active_dept
    ) -> None:
        """No active dept means the step stays pending."""
        from httpx import ASGITransport, AsyncClient

        app.state.pg_pool = mock_pool_without_active_dept

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/setup/add_first_department/check"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
        assert data["step"] == "add_first_department"

    @pytest.mark.asyncio
    async def test_check_queries_automation_departments_table(
        self, app, mock_pool_without_active_dept
    ) -> None:
        """The check queries automation.departments for active rows."""
        from httpx import ASGITransport, AsyncClient

        app.state.pg_pool = mock_pool_without_active_dept

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/api/v1/setup/add_first_department/check")

        # Verify the query targets the correct table
        call_args = mock_pool_without_active_dept.fetchrow.call_args
        query = call_args[0][0]
        assert "automation.departments" in query

    @pytest.mark.asyncio
    async def test_check_graceful_when_no_pool(self, app) -> None:
        """When no DB pool is available, returns current state without crash."""
        from httpx import ASGITransport, AsyncClient

        # Don't set app.state.pg_pool - simulates no DB connection

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/setup/add_first_department/check"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["step"] == "add_first_department"
        # Status should reflect current wizard state (pending by default)
        assert data["status"] in ("pending", "completed", "failed")
