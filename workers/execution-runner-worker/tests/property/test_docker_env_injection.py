"""Property tests for Docker environment variable injection completeness.

**Property 2: Docker environment variable injection completeness**

**Validates: Requirements 1.9**

Per ``.kiro/specs/platform-completion/design.md`` §"Property 2", for any
set of environment variables defined in department configuration, ALL
key-value pairs SHALL appear as ``--env`` parameters in the docker run
command, with no additions or omissions.

The function under test, :func:`build_docker_run_command`, is a pure
helper that constructs the docker run command string from a
:class:`DockerRunInput` dataclass. It requires no SSH connection,
Temporal runtime, or external services — making it ideal for
property-based testing.

We use Hypothesis to generate random environment variable dictionaries
and verify that every key-value pair is present as an ``--env`` argument
in the generated command string.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without installing the worker package.
# Mirrors the bootstrap pattern used in sibling property tests
# (e.g. test_docker_cleanup_policy.py).
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from activities.docker import (  # noqa: E402
    DockerRunInput,
    build_docker_run_command,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Environment variable keys: non-empty strings composed of letters and
#: digits (matching typical env var naming conventions).
_env_key_strategy = st.text(
    min_size=1,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)

#: Environment variable values: non-empty strings (can contain any
#: printable characters).
_env_value_strategy = st.text(min_size=1)

#: Dictionary of environment variables with alphanumeric keys and
#: arbitrary non-empty string values.
_env_dict_strategy = st.dictionaries(
    keys=_env_key_strategy,
    values=_env_value_strategy,
    min_size=1,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Property 2: Docker environment variable injection completeness
# ---------------------------------------------------------------------------


@given(env_vars=_env_dict_strategy)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_all_env_vars_present_as_env_flags(env_vars: dict[str, str]) -> None:
    """Every key-value pair in environment SHALL appear as --env in the command.

    For any non-empty dictionary of environment variables, the generated
    docker run command must contain an ``--env`` flag for each key-value
    pair. No environment variable may be omitted.

    **Validates: Requirements 1.9**
    """
    run_input = DockerRunInput(
        image="test-image:latest",
        command="echo hello",
        workspace_path="/workspace/test",
        environment=env_vars,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        timeout_seconds=1800,
        max_timeout_seconds=7200,
        workflow_id="test-workflow",
    )

    cmd = build_docker_run_command(run_input)

    # Count --env occurrences — must equal the number of env vars
    env_flag_count = cmd.count("--env ")
    assert env_flag_count == len(env_vars), (
        f"Expected {len(env_vars)} --env flags in the command, "
        f"but found {env_flag_count}. "
        f"Environment variables: {env_vars!r}\n"
        f"Command: {cmd}"
    )

    # Verify each key=value pair is present in the command
    for key, value in env_vars.items():
        expected_pair = f"{key}={value}"
        assert expected_pair in cmd, (
            f"Environment variable {key}={value!r} not found in "
            f"docker run command.\nCommand: {cmd}"
        )


@given(env_vars=_env_dict_strategy)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
def test_no_extra_env_flags_beyond_input(env_vars: dict[str, str]) -> None:
    """The command SHALL NOT contain more --env flags than provided env vars.

    This ensures no spurious environment variables are injected beyond
    what was specified in the input.

    **Validates: Requirements 1.9**
    """
    run_input = DockerRunInput(
        image="test-image:latest",
        command="echo hello",
        workspace_path="/workspace/test",
        environment=env_vars,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        timeout_seconds=1800,
        max_timeout_seconds=7200,
        workflow_id="test-workflow",
    )

    cmd = build_docker_run_command(run_input)

    env_flag_count = cmd.count("--env ")
    assert env_flag_count == len(env_vars), (
        f"Expected exactly {len(env_vars)} --env flags (no additions, "
        f"no omissions), but found {env_flag_count}.\n"
        f"Command: {cmd}"
    )


@given(
    env_vars=st.dictionaries(
        keys=_env_key_strategy,
        values=_env_value_strategy,
        min_size=0,
        max_size=0,
    )
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_empty_env_dict_produces_no_env_flags(env_vars: dict[str, str]) -> None:
    """An empty environment dict SHALL produce zero --env flags.

    **Validates: Requirements 1.9**
    """
    run_input = DockerRunInput(
        image="test-image:latest",
        command="echo hello",
        workspace_path="/workspace/test",
        environment=env_vars,
        cpu_limit=2.0,
        memory_limit_mb=2048,
        timeout_seconds=1800,
        max_timeout_seconds=7200,
        workflow_id="test-workflow",
    )

    cmd = build_docker_run_command(run_input)

    assert "--env" not in cmd, (
        f"Empty environment dict should produce no --env flags.\n"
        f"Command: {cmd}"
    )


@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(env_vars=_env_dict_strategy)
def test_none_environment_produces_no_env_flags(env_vars: dict[str, str]) -> None:
    """A None environment SHALL produce zero --env flags.

    **Validates: Requirements 1.9**
    """
    # env_vars is drawn but unused — we always pass None to confirm
    # the None path is safe regardless of what Hypothesis generates.
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

    assert "--env" not in cmd, (
        f"None environment should produce no --env flags.\n"
        f"Command: {cmd}"
    )
