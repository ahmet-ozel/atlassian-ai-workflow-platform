"""Docker cleanup policy correctness.

For any combination of cleanup policy ("on_success", "always", "never") and
task result (success/failure), the Docker_Activity executes cleanup
(docker rm + docker rmi) if and only if:

- policy is "always", OR
- policy is "on_success" AND task succeeded.

Otherwise cleanup is skipped (policy is "never", or policy is
"on_success" and task failed).

The function under test, :func:`_should_perform_cleanup`, is a pure
helper extracted from the ``docker_cleanup_container`` Temporal activity.
It requires no SSH connection, Temporal runtime, or external services -
making it ideal for property-based testing.

Additionally, :func:`build_docker_run_command` is tested to verify that
containers are always created with ``--rm=false`` (cleanup is managed
externally by the policy, not by Docker's auto-remove).
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without installing the worker package.
# Mirrors the bootstrap pattern used in sibling property tests
# (e.g. agent-runner-worker/tests/property/test_work_item_state_machine.py).
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from activities.docker import (  # noqa: E402
    DockerRunInput,
    _should_perform_cleanup,
    build_docker_run_command,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: All valid cleanup policies as defined in the design document.
_CLEANUP_POLICIES = st.sampled_from(["always", "on_success", "never"])

#: Task success/failure outcome.
_TASK_SUCCEEDED = st.booleans()


# ---------------------------------------------------------------------------
# Docker cleanup policy correctness
# ---------------------------------------------------------------------------


@given(task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cleanup_policy_always_performs_cleanup(task_succeeded: bool) -> None:
    """Policy "always" triggers cleanup regardless of task outcome."""
    result = _should_perform_cleanup("always", task_succeeded)
    assert result is True, (
        f"Expected cleanup for policy='always', task_succeeded={task_succeeded}"
    )


@given(task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cleanup_policy_on_success_with_success_performs_cleanup(
    task_succeeded: bool,
) -> None:
    """Policy "on_success" + success  cleanup (rm + rmi)."""
    assume(task_succeeded is True)

    result = _should_perform_cleanup("on_success", task_succeeded)
    assert result is True, (
        "Expected cleanup for policy='on_success' when task succeeded"
    )


@given(task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cleanup_policy_on_success_with_failure_skips_cleanup(
    task_succeeded: bool,
) -> None:
    """Policy "on_success" + failure  skip cleanup."""
    assume(task_succeeded is False)

    result = _should_perform_cleanup("on_success", task_succeeded)
    assert result is False, (
        "Expected NO cleanup for policy='on_success' when task failed"
    )


@given(task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cleanup_policy_never_skips_cleanup(task_succeeded: bool) -> None:
    """Policy "never" skips cleanup regardless of task outcome."""
    result = _should_perform_cleanup("never", task_succeeded)
    assert result is False, (
        f"Expected NO cleanup for policy='never', task_succeeded={task_succeeded}"
    )


@given(policy=_CLEANUP_POLICIES, task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_cleanup_policy_complete_truth_table(
    policy: str, task_succeeded: bool
) -> None:
    """For any (policy, task_succeeded) pair, cleanup decision matches the truth table.

    The complete truth table:
    - ("always", True)   True
    - ("always", False)  True
    - ("on_success", True)   True
    - ("on_success", False)  False
    - ("never", True)   False
    - ("never", False)  False

    This is the unified property that covers all combinations.

    """
    result = _should_perform_cleanup(policy, task_succeeded)

    expected = (policy == "always") or (policy == "on_success" and task_succeeded)

    assert result == expected, (
        f"Cleanup decision mismatch: policy={policy!r}, "
        f"task_succeeded={task_succeeded}, got={result}, expected={expected}"
    )


# ---------------------------------------------------------------------------
# Supplementary check: build_docker_run_command uses --rm=false
# ---------------------------------------------------------------------------


@given(policy=_CLEANUP_POLICIES, task_succeeded=_TASK_SUCCEEDED)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_docker_run_command_disables_auto_remove(
    policy: str, task_succeeded: bool
) -> None:
    """Docker run command always includes --rm=false.

    Cleanup is managed externally by the cleanup policy, so containers
    must not be auto-removed by Docker. This ensures the cleanup activity
    can inspect and remove containers according to the configured policy.
    """
    # Create a minimal DockerRunInput - the policy/task_succeeded don't
    # affect the run command itself, but we vary them to confirm --rm=false
    # is unconditional.
    run_input = DockerRunInput(
        image="test-image:latest",
        command="echo hello",
        workspace_path="/workspace/test",
        environment=None,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        timeout_seconds=1800,
        max_timeout_seconds=7200,
        workflow_id="test-workflow",
    )

    cmd = build_docker_run_command(run_input)

    assert "--rm=false" in cmd, (
        f"Docker run command must include --rm=false to allow policy-based "
        f"cleanup. Got: {cmd}"
    )
