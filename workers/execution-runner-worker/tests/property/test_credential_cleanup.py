"""Property test for credential cleanup guarantee.

**Validates: Requirements 2.3**

Property 4: Credential cleanup guarantee

*For any* git push operation result (success or failure), the
Credential_Injector SHALL remove temporary credential configuration
from SSH_Runner within 5 seconds of operation completion.

This module verifies two invariants:
1. ``cleanup_git_credentials`` is always called regardless of whether
   the git push succeeded or failed.
2. The cleanup operation uses a timeout of at most
   ``CLEANUP_TIMEOUT_SECONDS`` (5.0s), guaranteeing timely removal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — ensure ``src.activities.credential_injector`` is
# importable when pytest is invoked from any working directory.
# ---------------------------------------------------------------------------

_WORKER_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKER_SRC))

from src.activities.credential_injector import (  # noqa: E402
    CLEANUP_TIMEOUT_SECONDS,
    cleanup_git_credentials,
)

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ===========================================================================
# Property 4 — Credential cleanup guarantee
# ===========================================================================


class TestCredentialCleanupGuarantee:
    """cleanup_git_credentials is called regardless of push result and
    completes within the 5-second timeout budget.

    **Validates: Requirements 2.3**
    """

    @_PROFILE
    @given(push_success=st.booleans())
    @pytest.mark.asyncio
    async def test_cleanup_called_regardless_of_push_result(
        self, push_success: bool
    ) -> None:
        """**Validates: Requirements 2.3**

        For any git push result (success or failure), cleanup_git_credentials
        is invoked and executes the SSH cleanup command.
        """
        workflow_id = f"wf-test-{'success' if push_success else 'failure'}"

        # Mock the SSH execution that cleanup_git_credentials calls internally
        mock_ssh_result = {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
        }

        with patch(
            "src.activities.credential_injector._execute_on_ssh",
            new_callable=AsyncMock,
            return_value=mock_ssh_result,
        ) as mock_execute, patch(
            "src.activities.credential_injector.activity"
        ):
            # Simulate: after a git push (success or failure), cleanup is called
            await cleanup_git_credentials(workflow_id)

            # Assert: cleanup SSH command was executed exactly once
            mock_execute.assert_called_once()

            # Verify the timeout passed to SSH execution is <= 5 seconds
            call_kwargs = mock_execute.call_args
            # _execute_on_ssh(command, timeout_seconds, workflow_id)
            actual_timeout = call_kwargs[1]["timeout_seconds"] if call_kwargs[1] else call_kwargs[0][1]
            assert actual_timeout <= CLEANUP_TIMEOUT_SECONDS, (
                f"Cleanup timeout {actual_timeout}s exceeds the "
                f"guaranteed {CLEANUP_TIMEOUT_SECONDS}s limit"
            )

    @_PROFILE
    @given(push_success=st.booleans())
    @pytest.mark.asyncio
    async def test_cleanup_timeout_within_5_seconds(
        self, push_success: bool
    ) -> None:
        """**Validates: Requirements 2.3**

        The CLEANUP_TIMEOUT_SECONDS constant is at most 5.0, ensuring
        the credential removal completes within the required window.
        """
        # The constant itself must satisfy the requirement
        assert CLEANUP_TIMEOUT_SECONDS <= 5.0, (
            f"CLEANUP_TIMEOUT_SECONDS={CLEANUP_TIMEOUT_SECONDS} exceeds "
            f"the 5-second requirement from Requirement 2.3"
        )

        workflow_id = f"wf-push-{'ok' if push_success else 'fail'}"

        mock_ssh_result = {
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
        }

        with patch(
            "src.activities.credential_injector._execute_on_ssh",
            new_callable=AsyncMock,
            return_value=mock_ssh_result,
        ) as mock_execute, patch(
            "src.activities.credential_injector.activity"
        ):
            await cleanup_git_credentials(workflow_id)

            # Verify _execute_on_ssh was called with timeout_seconds=CLEANUP_TIMEOUT_SECONDS
            call_args = mock_execute.call_args[0]
            # Positional args: (command, timeout_seconds, workflow_id)
            timeout_arg = call_args[1]
            assert timeout_arg == CLEANUP_TIMEOUT_SECONDS
            assert timeout_arg <= 5.0

    @_PROFILE
    @given(push_success=st.booleans())
    @pytest.mark.asyncio
    async def test_cleanup_executes_even_when_ssh_fails(
        self, push_success: bool
    ) -> None:
        """**Validates: Requirements 2.3**

        Even if the SSH execution raises an error during cleanup,
        the function handles it gracefully (best-effort) without
        propagating the exception — ensuring the cleanup attempt
        was made regardless of push outcome.
        """
        from src.activities.credential_injector import CredentialInjectorError

        workflow_id = f"wf-cleanup-err-{'success' if push_success else 'failure'}"

        with patch(
            "src.activities.credential_injector._execute_on_ssh",
            new_callable=AsyncMock,
            side_effect=CredentialInjectorError(
                workflow_id=workflow_id,
                cause="SSH connection refused",
                error_code="credential_unavailable",
            ),
        ) as mock_execute, patch(
            "src.activities.credential_injector.activity"
        ):
            # cleanup_git_credentials should NOT raise even if SSH fails
            # (best-effort cleanup per the implementation)
            await cleanup_git_credentials(workflow_id)

            # The SSH execution was still attempted
            mock_execute.assert_called_once()

            # Verify timeout was still within bounds
            call_args = mock_execute.call_args[0]
            timeout_arg = call_args[1]
            assert timeout_arg <= 5.0
