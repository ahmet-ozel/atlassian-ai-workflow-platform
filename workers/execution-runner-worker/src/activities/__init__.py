"""Activity modules for the execution-runner-worker.

Exports the Temporal activity functions that the ExecutionRunWorkflow
invokes for Vault credential fetching, SSH execution, MinIO artifact
storage, Docker container management, SSH healthcheck, disk quota
enforcement, cleanup policy enforcement, and runner resolution.
"""

from .cleanup import (
    CleanupPolicyInput,
    CleanupPolicyResult,
    apply_cleanup_policy,
)
from .disk_quota import (
    DiskQuotaError,
    DiskQuotaInput,
    DiskQuotaResult,
    check_disk_quota,
)
from .docker import (
    DockerBuildInput,
    DockerBuildResult,
    DockerCleanupInput,
    DockerRunInput,
    DockerRunResult,
    build_docker_run_command,
    docker_build_image,
    docker_cleanup_container,
    docker_collect_logs,
    docker_daemon_healthcheck,
    docker_run_container,
    docker_stop_container,
)
from .minio import (
    ArtifactRef,
    DEFAULT_BUCKET,
    MinIOError,
    minio_download_artifact,
    minio_upload_artifact,
)
from .runner_resolver import (
    RunnerResolution,
    RunnerResolutionError,
    resolve_runner,
)
from .ssh import (
    HEARTBEAT_INTERVAL_S,
    RunResult,
    SSHActivityError,
    ssh_cleanup,
    ssh_connect_and_run,
    ssh_run_test,
)
from .ssh_healthcheck import (
    SSHHealthcheckResult,
    ssh_healthcheck,
)
from .vault import (
    CredentialResolutionError,
    SSHCred,
    vault_fetch_ssh_credentials,
)
from .workspace_cleanup import (
    WorkspaceCleanupError,
    WorkspaceDiskSnapshot,
    WorkspaceIterEntry,
    WorkspacePruneResult,
    emit_workspace_disk_warning,
    list_workspace_iter_dirs_oldest_first,
    probe_workspace_disk_usage,
    prune_workspace_iter,
)

__all__ = [
    # Vault
    "CredentialResolutionError",
    "SSHCred",
    "vault_fetch_ssh_credentials",
    # SSH
    "RunResult",
    "SSHActivityError",
    "ssh_connect_and_run",
    "ssh_cleanup",
    "ssh_run_test",
    "HEARTBEAT_INTERVAL_S",
    # SSH Healthcheck
    "SSHHealthcheckResult",
    "ssh_healthcheck",
    # Runner Resolver (multi-SSH pool - G5)
    "RunnerResolution",
    "RunnerResolutionError",
    "resolve_runner",
    # MinIO
    "ArtifactRef",
    "DEFAULT_BUCKET",
    "MinIOError",
    "minio_upload_artifact",
    "minio_download_artifact",
    # Docker
    "DockerBuildInput",
    "DockerBuildResult",
    "DockerRunInput",
    "DockerRunResult",
    "DockerCleanupInput",
    "docker_build_image",
    "docker_run_container",
    "docker_collect_logs",
    "docker_stop_container",
    "docker_cleanup_container",
    "docker_daemon_healthcheck",
    "build_docker_run_command",
    # Cleanup Policy
    "CleanupPolicyInput",
    "CleanupPolicyResult",
    "apply_cleanup_policy",
    # Disk Quota
    "DiskQuotaInput",
    "DiskQuotaResult",
    "DiskQuotaError",
    "check_disk_quota",
    # Workspace Cleanup Scheduler (single-runner canonical contract - G2)
    "WorkspaceCleanupError",
    "WorkspaceDiskSnapshot",
    "WorkspaceIterEntry",
    "WorkspacePruneResult",
    "probe_workspace_disk_usage",
    "emit_workspace_disk_warning",
    "list_workspace_iter_dirs_oldest_first",
    "prune_workspace_iter",
]
