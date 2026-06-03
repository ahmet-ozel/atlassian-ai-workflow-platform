"""SSHHealthcheckCronWorkflow — Temporal cron workflow for proactive SSH monitoring.

Runs every 5 minutes via Temporal cron schedule and monitors SSH runner
health using the existing ``ssh_healthcheck`` activity.

State machine:
    - 3 consecutive failures → mark "unhealthy", block new SSH tasks
    - 2 consecutive successes (while unhealthy) → restore "healthy", remove block
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of consecutive failures before marking runner as unhealthy.
UNHEALTHY_THRESHOLD: int = 3

#: Number of consecutive successes (while unhealthy) to restore healthy.
RECOVERY_THRESHOLD: int = 2

#: Timeout for the ssh_healthcheck activity.
_HEALTHCHECK_ACTIVITY_TIMEOUT: timedelta = timedelta(seconds=10)

#: Timeout for recording results to the database.
_RECORD_ACTIVITY_TIMEOUT: timedelta = timedelta(seconds=30)

#: Timeout for sending notifications.
_NOTIFY_ACTIVITY_TIMEOUT: timedelta = timedelta(seconds=60)

#: Maximum delay before showing a warning.
_MAX_HEALTHCHECK_DELAY_SECONDS: float = 120.0

#: Retry policy for the healthcheck activity — single attempt since
#: the cron itself handles consecutive failure tracking.
_HEALTHCHECK_RETRY_POLICY: RetryPolicy = RetryPolicy(maximum_attempts=1)

#: Retry policy for notification/recording activities — allow retries.
_SIDE_EFFECT_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class HealthcheckState:
    """Tracks consecutive healthcheck results for state machine transitions.

    Attributes
    ----------
    consecutive_failures : int
        Number of consecutive failed healthchecks.
    consecutive_successes : int
        Number of consecutive successful healthchecks (only meaningful
        when ``is_healthy`` is False).
    is_healthy : bool
        Current health status of the SSH runner.
    last_check_time : str | None
        ISO timestamp of the last healthcheck execution.
    """

    consecutive_failures: int = 0
    consecutive_successes: int = 0
    is_healthy: bool = True
    last_check_time: str | None = None


# ---------------------------------------------------------------------------
# Workflow Definition
# ---------------------------------------------------------------------------


@workflow.defn(name="SSHHealthcheckCronWorkflow")
class SSHHealthcheckCronWorkflow:
    """Temporal cron workflow that monitors SSH runner health.

    Cron schedule: every 5 minutes (``*/5 * * * *``).

    Behavior:
        1. Execute ``ssh_healthcheck`` activity with 10s timeout.
        2. On failure: show alert in Admin Dashboard (runner name,
           failure time, error reason), send notification to configured
           channel within 60s.
        3. After 3 consecutive failures: mark "unhealthy", block new
           SSH tasks.
        4. After 2 consecutive successes (while unhealthy): restore to
           "healthy", remove block.
        5. If healthcheck is delayed by 2+ minutes: show warning in
           Admin Dashboard.
        6. Record all results to ``ssh_healthcheck_log`` table.

    The workflow uses Temporal's memo feature to persist state across
    cron iterations. Each cron execution is a fresh workflow run, so
    state is passed via the cron memo mechanism.
    """

    @workflow.run
    async def run(self, state_dict: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a single healthcheck iteration.

        Parameters
        ----------
        state_dict : dict | None
            Serialized HealthcheckState from the previous cron run.
            None on first execution.

        Returns
        -------
        dict[str, Any]
            Updated state dict to be passed to the next cron iteration.
        """
        # Restore state from previous run
        state = self._deserialize_state(state_dict)

        workflow.logger.info(
            "SSHHealthcheckCronWorkflow: starting healthcheck iteration. "
            "Current state: is_healthy=%s, consecutive_failures=%d, "
            "consecutive_successes=%d",
            state.is_healthy,
            state.consecutive_failures,
            state.consecutive_successes,
        )

        # Record the current time for delay detection.
        current_time = workflow.now()
        current_time_iso = current_time.isoformat()

        # Check for healthcheck delay.
        if state.last_check_time is not None:
            await self._check_for_delay(state, current_time_iso)

        # Execute the SSH healthcheck activity.
        hc_result = await self._execute_healthcheck()

        # Update state based on result
        is_check_healthy = hc_result.get("healthy", False)
        host = hc_result.get("host", "unknown")
        port = hc_result.get("port", 22)
        error = hc_result.get("error")

        # Record result to ssh_healthcheck_log.
        await self._record_healthcheck_result(
            host=host,
            port=port,
            healthy=is_check_healthy,
            error=error,
        )

        if is_check_healthy:
            state = self._handle_success(state, current_time_iso)
        else:
            state = await self._handle_failure(
                state, current_time_iso, host, port, error
            )

        # Check for state transitions
        previous_healthy = state.is_healthy

        if not state.is_healthy and state.consecutive_successes >= RECOVERY_THRESHOLD:
            # Restore to healthy.
            state.is_healthy = True
            state.consecutive_failures = 0
            state.consecutive_successes = 0
            await self._restore_healthy(host, port)

        elif state.is_healthy and state.consecutive_failures >= UNHEALTHY_THRESHOLD:
            # Mark as unhealthy.
            state.is_healthy = False
            state.consecutive_successes = 0
            await self._mark_unhealthy(host, port)

        workflow.logger.info(
            "SSHHealthcheckCronWorkflow: iteration complete. "
            "Updated state: is_healthy=%s, consecutive_failures=%d, "
            "consecutive_successes=%d",
            state.is_healthy,
            state.consecutive_failures,
            state.consecutive_successes,
        )

        return self._serialize_state(state)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _deserialize_state(self, state_dict: dict[str, Any] | None) -> HealthcheckState:
        """Reconstruct HealthcheckState from a serialized dict."""
        if state_dict is None:
            return HealthcheckState()
        return HealthcheckState(
            consecutive_failures=state_dict.get("consecutive_failures", 0),
            consecutive_successes=state_dict.get("consecutive_successes", 0),
            is_healthy=state_dict.get("is_healthy", True),
            last_check_time=state_dict.get("last_check_time"),
        )

    def _serialize_state(self, state: HealthcheckState) -> dict[str, Any]:
        """Serialize HealthcheckState to a dict for cron continuation."""
        return {
            "consecutive_failures": state.consecutive_failures,
            "consecutive_successes": state.consecutive_successes,
            "is_healthy": state.is_healthy,
            "last_check_time": state.last_check_time,
        }

    async def _execute_healthcheck(self) -> dict[str, Any]:
        """Run the ssh_healthcheck activity with 10s timeout."""
        try:
            result: dict[str, Any] = await workflow.execute_activity(
                "ssh_healthcheck",
                start_to_close_timeout=_HEALTHCHECK_ACTIVITY_TIMEOUT,
                retry_policy=_HEALTHCHECK_RETRY_POLICY,
            )
            return result
        except Exception:  # noqa: BLE001
            # Activity itself failed — treat as unhealthy
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: ssh_healthcheck activity "
                "raised an exception — treating as unhealthy"
            )
            return {
                "healthy": False,
                "host": "unknown",
                "port": 22,
                "error": "healthcheck_activity_failed",
            }

    def _handle_success(
        self, state: HealthcheckState, current_time_iso: str
    ) -> HealthcheckState:
        """Update state on successful healthcheck."""
        state.consecutive_failures = 0
        state.last_check_time = current_time_iso

        if not state.is_healthy:
            # Count consecutive successes toward recovery
            state.consecutive_successes += 1
            workflow.logger.info(
                "SSHHealthcheckCronWorkflow: success while unhealthy. "
                "consecutive_successes=%d/%d for recovery",
                state.consecutive_successes,
                RECOVERY_THRESHOLD,
            )
        else:
            state.consecutive_successes = 0

        return state

    async def _handle_failure(
        self,
        state: HealthcheckState,
        current_time_iso: str,
        host: str,
        port: int,
        error: str | None,
    ) -> HealthcheckState:
        """Update state on failed healthcheck and send alerts."""
        state.consecutive_failures += 1
        state.consecutive_successes = 0
        state.last_check_time = current_time_iso

        workflow.logger.warning(
            "SSHHealthcheckCronWorkflow: healthcheck FAILED. "
            "host=%s port=%d error=%s consecutive_failures=%d",
            host,
            port,
            error,
            state.consecutive_failures,
        )

        # Show alert in Admin Dashboard and send notification.
        await self._send_failure_alert(
            host=host,
            port=port,
            error=error or "unknown",
            failure_time=current_time_iso,
        )

        return state

    async def _send_failure_alert(
        self,
        *,
        host: str,
        port: int,
        error: str,
        failure_time: str,
    ) -> None:
        """Send failure alert to Admin Dashboard and notification channel."""
        try:
            await workflow.execute_activity(
                "send_ssh_healthcheck_alert",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "error": error,
                        "failure_time": failure_time,
                        "alert_type": "ssh_healthcheck_failed",
                    }
                ],
                start_to_close_timeout=_NOTIFY_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            # Alert sending is best-effort — log and continue
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to send failure alert "
                "(best-effort, continuing)"
            )

    async def _mark_unhealthy(self, host: str, port: int) -> None:
        """Mark SSH runner as unhealthy and block new SSH tasks."""
        workflow.logger.error(
            "SSHHealthcheckCronWorkflow: marking runner as UNHEALTHY. "
            "host=%s port=%d — blocking new SSH tasks",
            host,
            port,
        )

        try:
            await workflow.execute_activity(
                "update_ssh_runner_status",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "status": "unhealthy",
                        "action": "block_new_tasks",
                    }
                ],
                start_to_close_timeout=_RECORD_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to update runner status "
                "to unhealthy (best-effort, continuing)"
            )

        # Send critical notification about unhealthy state
        try:
            await workflow.execute_activity(
                "send_ssh_healthcheck_alert",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "error": "3 consecutive healthcheck failures",
                        "failure_time": workflow.now().isoformat(),
                        "alert_type": "ssh_runner_unhealthy",
                    }
                ],
                start_to_close_timeout=_NOTIFY_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to send unhealthy "
                "notification (best-effort, continuing)"
            )

    async def _restore_healthy(self, host: str, port: int) -> None:
        """Restore SSH runner to healthy and remove task block."""
        workflow.logger.info(
            "SSHHealthcheckCronWorkflow: restoring runner to HEALTHY. "
            "host=%s port=%d — removing task block",
            host,
            port,
        )

        try:
            await workflow.execute_activity(
                "update_ssh_runner_status",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "status": "healthy",
                        "action": "unblock_tasks",
                    }
                ],
                start_to_close_timeout=_RECORD_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to update runner status "
                "to healthy (best-effort, continuing)"
            )

        # Send recovery notification
        try:
            await workflow.execute_activity(
                "send_ssh_healthcheck_alert",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "error": None,
                        "failure_time": workflow.now().isoformat(),
                        "alert_type": "ssh_runner_recovered",
                    }
                ],
                start_to_close_timeout=_NOTIFY_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to send recovery "
                "notification (best-effort, continuing)"
            )

    async def _record_healthcheck_result(
        self,
        *,
        host: str,
        port: int,
        healthy: bool,
        error: str | None,
    ) -> None:
        """Persist healthcheck result to ssh_healthcheck_log table."""
        try:
            await workflow.execute_activity(
                "record_ssh_healthcheck",
                args=[
                    {
                        "host": host,
                        "port": port,
                        "healthy": healthy,
                        "error": error,
                    }
                ],
                start_to_close_timeout=_RECORD_ACTIVITY_TIMEOUT,
                retry_policy=_SIDE_EFFECT_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: failed to record healthcheck "
                "result (best-effort, continuing)"
            )

    async def _check_for_delay(
        self, state: HealthcheckState, current_time_iso: str
    ) -> None:
        """Check if healthcheck is delayed and emit warning if needed."""
        # Parse last_check_time and compare with current time
        # Since we're in a workflow, we use workflow.now() for determinism
        # The delay detection is approximate — based on expected 5-min interval
        # If last_check_time exists and the gap exceeds 7 minutes (5 min interval + 2 min tolerance),
        # we consider it delayed.
        try:
            from datetime import datetime, timezone

            if state.last_check_time:
                last_check = datetime.fromisoformat(state.last_check_time)
                current = workflow.now()
                elapsed = (current - last_check).total_seconds()

                # Expected interval is 5 minutes (300s). If elapsed > 420s (7 min),
                # the healthcheck was delayed by more than 2 minutes.
                if elapsed > 420.0:
                    workflow.logger.warning(
                        "SSHHealthcheckCronWorkflow: healthcheck delayed by "
                        "%.1f seconds (expected ~300s interval)",
                        elapsed,
                    )
                    await workflow.execute_activity(
                        "send_ssh_healthcheck_alert",
                        args=[
                            {
                                "host": "unknown",
                                "port": 0,
                                "error": f"Healthcheck delayed by {elapsed - 300:.0f}s",
                                "failure_time": current_time_iso,
                                "alert_type": "healthcheck_delayed",
                            }
                        ],
                        start_to_close_timeout=_NOTIFY_ACTIVITY_TIMEOUT,
                        retry_policy=_SIDE_EFFECT_RETRY_POLICY,
                    )
        except Exception:  # noqa: BLE001
            # Delay detection is best-effort
            workflow.logger.warning(
                "SSHHealthcheckCronWorkflow: delay detection failed "
                "(best-effort, continuing)"
            )
