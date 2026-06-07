"""Workspace cleanup activities for the runner-wide disk auto-prune cron.

Single-runner canonical contract (G2). The execution-runner is the only
worker with SSH credentials, so the four activities driven by
``WorkspaceCleanupSchedulerWorkflow`` (in ``automation-worker``) live
here:

* :func:`probe_workspace_disk_usage` - runs ``df`` against
  ``RUNNER_BASE_PATH`` and returns a snapshot.
* :func:`emit_workspace_disk_warning` - POSTs a warning to the
  admin-dashboard with 60-minute dedup. Decoupled from the per-dept
  ``check_disk_quota`` warning path: this one carries
  ``dept_id="*"`` to indicate "host-wide" and uses a separate dedup
  key so a host-wide alert never silences a per-dept one.
* :func:`list_workspace_iter_dirs_oldest_first` - lists every
  ``iter-N`` directory under ``RUNNER_BASE_PATH`` sorted by mtime
  ascending (oldest first), with size estimates.
* :func:`prune_workspace_iter` - ``rm -rf``'s a single ``iter-N``
  directory and writes a ``workspace_auto_pruned`` audit event.

Single-source-of-truth env resolution
-------------------------------------

Every activity in this module reads ``SSH_HOST`` (canonical) with
``SSH_HOST_1`` accepted as a deprecated alias, ``RUNNER_BASE_PATH``
(canonical with ``SSH_BASE_PATH`` deprecated alias),
``RUNNER_DISK_WARN_PCT``, and ``RUNNER_DISK_EVICT_PCT``. The activities
**never** read per-department config - this is the host-wide pruner.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from temporalio import activity

__all__ = [
    "WorkspaceDiskSnapshot",
    "WorkspaceIterEntry",
    "WorkspacePruneResult",
    "WorkspaceCleanupError",
    "probe_workspace_disk_usage",
    "emit_workspace_disk_warning",
    "list_workspace_iter_dirs_oldest_first",
    "prune_workspace_iter",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default warn / evict thresholds. Mirror the constants in the workflow
#: module; activities apply them only when the env vars are unset / unparsable.
DEFAULT_WARN_PCT: int = 80
DEFAULT_EVICT_PCT: int = 90

#: Timeout for the ``df`` probe - single short SSH command.
DISK_PROBE_TIMEOUT_S: float = 30.0

#: Timeout for the ``find`` listing - may take a few seconds on a
#: saturated host.
LIST_TIMEOUT_S: float = 60.0

#: Timeout for a single ``rm -rf``. Large workspaces can take minutes.
PRUNE_TIMEOUT_S: float = 300.0

#: Dedup window for host-wide warnings (minutes). Mirrors the per-dept
#: ``disk_quota`` activity dedup window.
WARNING_DEDUP_MINUTES: int = 60


# ---------------------------------------------------------------------------
# Errors and dataclasses
# ---------------------------------------------------------------------------


class WorkspaceCleanupError(RuntimeError):
    """Raised when an SSH command in the cleanup pipeline fails fatally."""


@dataclass(frozen=True)
class WorkspaceDiskSnapshot:
    """Mirrors the workflow-side dataclass; serialised as a plain dict."""

    runner_base_path: str
    total_mb: int
    used_mb: int
    usage_pct: float
    warn_pct: int = DEFAULT_WARN_PCT
    evict_pct: int = DEFAULT_EVICT_PCT
    error: str | None = None


@dataclass(frozen=True)
class WorkspaceIterEntry:
    """Mirrors the workflow-side dataclass; serialised as a plain dict."""

    path: str
    issue_key: str
    iter_n: int
    mtime_epoch: int
    size_mb: int


@dataclass(frozen=True)
class WorkspacePruneResult:
    """Mirrors the workflow-side dataclass; serialised as a plain dict."""

    path: str
    success: bool
    freed_mb: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Env / SSH helpers
# ---------------------------------------------------------------------------


def _resolve_runner_base_path() -> str:
    """Read ``RUNNER_BASE_PATH`` with ``SSH_BASE_PATH`` deprecated alias."""
    base = os.environ.get("RUNNER_BASE_PATH", "").strip()
    if not base:
        base = os.environ.get("SSH_BASE_PATH", "").strip()
    if not base:
        base = "/var/ai-runner"
    return base.rstrip("/")


def _resolve_thresholds() -> tuple[int, int]:
    """Read ``RUNNER_DISK_WARN_PCT`` / ``RUNNER_DISK_EVICT_PCT`` from env."""

    def _parse(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        if value < 1 or value > 99:
            return default
        return value

    warn = _parse("RUNNER_DISK_WARN_PCT", DEFAULT_WARN_PCT)
    evict = _parse("RUNNER_DISK_EVICT_PCT", DEFAULT_EVICT_PCT)
    if evict <= warn:
        # Defensive - if an operator inverts them, restore sane order.
        warn, evict = DEFAULT_WARN_PCT, DEFAULT_EVICT_PCT
    return warn, evict


def _admin_dashboard_api_url() -> str:
    """Read Admin Dashboard API URL from environment."""
    return os.environ.get(
        "ADMIN_DASHBOARD_API_URL", "http://admin-dashboard-api:8000"
    ).rstrip("/")


async def _ssh_exec(
    command: str, timeout_seconds: float, *, label: str
) -> dict[str, Any]:
    """Execute a command on the single SSH runner host.

    Lazy imports the existing ``vault`` + ``ssh`` activity helpers so
    this module does not introduce any new SSH plumbing.
    """
    from src.activities.vault import vault_fetch_ssh_credentials
    from src.activities.ssh import _ssh_execute_command, SSHActivityError

    try:
        cred = await vault_fetch_ssh_credentials(label)
    except Exception as exc:
        raise WorkspaceCleanupError(
            f"SSH credential fetch failed: {exc}"
        ) from exc

    try:
        result = await asyncio.to_thread(
            _ssh_execute_command,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            command,
            "",
            int(timeout_seconds),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except SSHActivityError as exc:
        raise WorkspaceCleanupError(
            f"SSH execution failed ({label}): {exc.cause}"
        ) from exc


# ---------------------------------------------------------------------------
# Activity 1 - probe_workspace_disk_usage
# ---------------------------------------------------------------------------


@activity.defn(name="probe_workspace_disk_usage")
async def probe_workspace_disk_usage() -> dict[str, Any]:
    """Run ``df`` against the runner workspace base path.

    Returns a snapshot dict that mirrors :class:`WorkspaceDiskSnapshot`.
    Non-fatal on probe failure: returns a dict with ``error`` set so the
    workflow can record the failure without crashing the cron.
    """
    base = _resolve_runner_base_path()
    warn_pct, evict_pct = _resolve_thresholds()

    activity.logger.info(
        "probe_workspace_disk_usage: base=%s warn=%d%% evict=%d%%",
        base,
        warn_pct,
        evict_pct,
    )

    # ``df -P -m {base}``  POSIX-portable, MB units.
    # Output shape:
    # Filesystem    1M-blocks    Used    Available    Capacity    Mounted on
    # /dev/sda1     102400       54231   48169        53%         /
    command = f"df -P -m {base!s} 2>/dev/null | tail -n 1"

    try:
        result = await _ssh_exec(
            command, DISK_PROBE_TIMEOUT_S, label="workspace_disk_probe"
        )
    except WorkspaceCleanupError as exc:
        return asdict(
            WorkspaceDiskSnapshot(
                runner_base_path=base,
                total_mb=0,
                used_mb=0,
                usage_pct=0.0,
                warn_pct=warn_pct,
                evict_pct=evict_pct,
                error=str(exc),
            )
        )

    if result["exit_code"] != 0:
        return asdict(
            WorkspaceDiskSnapshot(
                runner_base_path=base,
                total_mb=0,
                used_mb=0,
                usage_pct=0.0,
                warn_pct=warn_pct,
                evict_pct=evict_pct,
                error=(
                    f"df exit_code={result['exit_code']}: "
                    f"{result['stderr'][:200]}"
                ),
            )
        )

    line = (result["stdout"] or "").strip()
    if not line:
        return asdict(
            WorkspaceDiskSnapshot(
                runner_base_path=base,
                total_mb=0,
                used_mb=0,
                usage_pct=0.0,
                warn_pct=warn_pct,
                evict_pct=evict_pct,
                error="df returned empty output",
            )
        )

    parts = line.split()
    # Defensive parse: at least 5 columns (filesystem, blocks, used, avail, capacity).
    if len(parts) < 5:
        return asdict(
            WorkspaceDiskSnapshot(
                runner_base_path=base,
                total_mb=0,
                used_mb=0,
                usage_pct=0.0,
                warn_pct=warn_pct,
                evict_pct=evict_pct,
                error=f"unparsable df output: {line!r}",
            )
        )

    try:
        total_mb = int(parts[1])
        used_mb = int(parts[2])
    except (TypeError, ValueError) as exc:
        return asdict(
            WorkspaceDiskSnapshot(
                runner_base_path=base,
                total_mb=0,
                used_mb=0,
                usage_pct=0.0,
                warn_pct=warn_pct,
                evict_pct=evict_pct,
                error=f"unparsable df numbers ({line!r}): {exc}",
            )
        )

    usage_pct = round((used_mb / total_mb) * 100, 1) if total_mb > 0 else 0.0

    return asdict(
        WorkspaceDiskSnapshot(
            runner_base_path=base,
            total_mb=total_mb,
            used_mb=used_mb,
            usage_pct=usage_pct,
            warn_pct=warn_pct,
            evict_pct=evict_pct,
            error=None,
        )
    )


# ---------------------------------------------------------------------------
# Activity 2 - emit_workspace_disk_warning
# ---------------------------------------------------------------------------


@activity.defn(name="emit_workspace_disk_warning")
async def emit_workspace_disk_warning(payload: dict[str, Any]) -> dict[str, Any]:
    """POST a host-wide disk warning to admin-dashboard with 60-minute dedup.

    Uses ``dept_id="*"`` to scope the warning at the host level. The
    admin-dashboard ``disk_quota_warnings`` table already stores
    ``dept_id`` so re-using the same dedup helper is safe; the
    sentinel ``*`` simply opens a separate dedup bucket from per-dept
    warnings.
    """
    url = f"{_admin_dashboard_api_url()}/api/v1/disk-quota/warnings"
    body = {
        "dept_id": "*",
        "scope": "runner_host",
        "runner_base_path": payload.get("runner_base_path", ""),
        "total_mb": int(payload.get("total_mb", 0) or 0),
        "used_mb": int(payload.get("used_mb", 0) or 0),
        "usage_mb": float(payload.get("used_mb", 0) or 0),
        "quota_mb": float(payload.get("total_mb", 0) or 0),
        "usage_percent": float(payload.get("usage_pct", 0.0) or 0.0),
        "warn_pct": int(payload.get("warn_pct", DEFAULT_WARN_PCT) or DEFAULT_WARN_PCT),
        "evict_pct": int(
            payload.get("evict_pct", DEFAULT_EVICT_PCT) or DEFAULT_EVICT_PCT
        ),
        "cleanup_candidates": [],
        "warned_at": datetime.now(timezone.utc).isoformat(),
        "dedup_minutes": WARNING_DEDUP_MINUTES,
    }

    activity.logger.info(
        "emit_workspace_disk_warning: usage=%.1f%% (warn=%d%% evict=%d%%)",
        body["usage_percent"],
        body["warn_pct"],
        body["evict_pct"],
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=body)
            sent = 200 <= response.status_code < 300
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "emit_workspace_disk_warning: POST failed: %s", exc
        )
        return {"sent": False, "status_code": None, "error": str(exc)}

    return {
        "sent": sent,
        "status_code": response.status_code,
        "error": None if sent else f"HTTP {response.status_code}",
    }


# ---------------------------------------------------------------------------
# Activity 3 - list_workspace_iter_dirs_oldest_first
# ---------------------------------------------------------------------------


# Matches workspace path layout {RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}/
_ITER_DIR_PATTERN = re.compile(
    r"^(?P<path>.+/(?P<issue>[A-Z][A-Z0-9_]*-\d+)/iter-(?P<n>\d+))$"
)


@activity.defn(name="list_workspace_iter_dirs_oldest_first")
async def list_workspace_iter_dirs_oldest_first() -> list[dict[str, Any]]:
    """List every ``iter-N`` directory under ``RUNNER_BASE_PATH``.

    Sorted oldest-first by mtime. Each entry includes a ``du -sm`` size
    estimate so the caller can maintain a running usage estimate during
    eviction without an extra round-trip per directory.

    Returns an empty list if the base path is empty / missing /
    contains no matching directories.
    """
    base = _resolve_runner_base_path()

    # ``find {base} -mindepth 2 -maxdepth 2 -type d -name 'iter-*' -printf '%T@ %s %p\n'``
    # yields: "<mtime_epoch_float> <size_bytes_unused> <abs_path>"
    # We then re-`du -sm` per entry to get size in MB (find's %s is the
    # directory inode size, not contents).
    command = (
        f"find {base!s} -mindepth 2 -maxdepth 2 -type d -name 'iter-*' "
        f"-printf '%T@\\t%p\\n' 2>/dev/null | sort -n"
    )

    try:
        result = await _ssh_exec(
            command, LIST_TIMEOUT_S, label="workspace_list_iter_dirs"
        )
    except WorkspaceCleanupError as exc:
        activity.logger.warning(
            "list_workspace_iter_dirs_oldest_first: SSH failed: %s", exc
        )
        return []

    if result["exit_code"] != 0:
        return []

    out = (result["stdout"] or "").strip()
    if not out:
        return []

    entries: list[WorkspaceIterEntry] = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        mtime_str, path = line.split("\t", 1)
        path = path.strip()
        try:
            mtime_epoch = int(float(mtime_str.strip()))
        except (TypeError, ValueError):
            mtime_epoch = 0

        match = _ITER_DIR_PATTERN.match(path)
        if not match:
            continue
        try:
            iter_n = int(match.group("n"))
        except (TypeError, ValueError):
            iter_n = 0
        issue_key = match.group("issue")

        # Best-effort size lookup. Failures degrade to size_mb=0; the
        # workflow handles zero-size entries by simply not updating the
        # running usage estimate.
        size_mb = 0
        try:
            size_result = await _ssh_exec(
                f"du -sm {path!s} 2>/dev/null | awk '{{print $1}}'",
                DISK_PROBE_TIMEOUT_S,
                label="workspace_iter_size",
            )
            if size_result["exit_code"] == 0:
                stripped = size_result["stdout"].strip()
                if stripped:
                    try:
                        size_mb = int(float(stripped))
                    except (TypeError, ValueError):
                        size_mb = 0
        except WorkspaceCleanupError:
            size_mb = 0

        entries.append(
            WorkspaceIterEntry(
                path=path,
                issue_key=issue_key,
                iter_n=iter_n,
                mtime_epoch=mtime_epoch,
                size_mb=size_mb,
            )
        )

    # The find ``sort -n`` already returns oldest-first; reaffirm here so
    # downstream callers can rely on the contract regardless of any
    # future implementation change.
    entries.sort(key=lambda e: e.mtime_epoch)
    return [asdict(e) for e in entries]


# ---------------------------------------------------------------------------
# Activity 4 - prune_workspace_iter
# ---------------------------------------------------------------------------


_AUDIT_WORKSPACE_AUTO_PRUNED: str = "workspace_auto_pruned"


def _safe_to_prune(path: str, base: str) -> bool:
    """Defensive guard: never ``rm -rf`` a path outside ``RUNNER_BASE_PATH``.

    Also rejects paths with ``..`` segments that could traverse out of
    the workspace tree. The workflow body already filters via the
    listing activity, but this is the last line of defence before
    invoking ``rm -rf`` over SSH.
    """
    if not path or not base:
        return False
    if ".." in path.split("/"):
        return False
    if not path.startswith(base.rstrip("/") + "/"):
        return False
    if "/iter-" not in path:
        return False
    return True


async def _write_audit_event(
    *,
    action: str,
    path: str,
    issue_key: str,
    iter_n: int,
    freed_mb: int,
    success: bool,
    error: str | None,
) -> None:
    """Best-effort audit event write to the admin-dashboard.

    The ``WorkspaceCleanupSchedulerWorkflow`` itself does not write
    audit events directly (it stays purely deterministic); the
    activity does. Failure to write is logged but does not propagate
    back to the workflow - the prune already succeeded at the SSH
    layer.
    """
    url = f"{_admin_dashboard_api_url()}/api/v1/audit/events"
    body = {
        "action": action,
        "actor_role": "system",
        "actor_id": "workspace-cleanup-scheduler",
        "dept_id": "*",
        "resource": path,
        "result": "ok" if success else "error",
        "metadata": {
            "issue_key": issue_key,
            "iter_n": iter_n,
            "freed_mb": freed_mb,
            "error": error,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=body)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "audit write failed (action=%s path=%s): %s", action, path, exc
        )


@activity.defn(name="prune_workspace_iter")
async def prune_workspace_iter(payload: dict[str, Any]) -> dict[str, Any]:
    """``rm -rf`` a single ``iter-N`` directory and write an audit event.

    The activity is naturally idempotent: re-running ``rm -rf`` against
    a path that no longer exists succeeds with exit_code=0 and
    ``freed_mb=0``.

    Refuses to prune paths outside ``RUNNER_BASE_PATH`` or paths that
    do not contain ``/iter-`` - defence-in-depth even though the
    listing activity should never produce such paths.
    """
    path = str(payload.get("path", "")).strip()
    issue_key = str(payload.get("issue_key", "")).strip()
    try:
        iter_n = int(payload.get("iter_n", 0) or 0)
    except (TypeError, ValueError):
        iter_n = 0
    try:
        expected_size_mb = int(payload.get("expected_size_mb", 0) or 0)
    except (TypeError, ValueError):
        expected_size_mb = 0

    base = _resolve_runner_base_path()
    if not _safe_to_prune(path, base):
        err = (
            f"refused unsafe prune path={path!r} "
            f"(outside base={base!r} or missing /iter- segment)"
        )
        activity.logger.error("prune_workspace_iter: %s", err)
        return asdict(
            WorkspacePruneResult(
                path=path, success=False, freed_mb=0, error=err
            )
        )

    activity.logger.info(
        "prune_workspace_iter: path=%s expected_size_mb=%d",
        path,
        expected_size_mb,
    )

    # Single shell command: re-measure size, rm -rf, then verify gone.
    # ``du`` may legitimately fail (ENOENT) if the directory was already
    # removed concurrently; treat that as freed_mb=0 success.
    command = (
        f"if [ -d {path!s} ]; then "
        f"  SIZE=$(du -sm {path!s} 2>/dev/null | awk '{{print $1}}' || echo 0); "
        f"  rm -rf {path!s} && echo \"FREED=${{SIZE}}\" || echo \"FREED=0\"; "
        f"else "
        f"  echo \"FREED=0\"; "
        f"fi"
    )

    try:
        result = await _ssh_exec(
            command, PRUNE_TIMEOUT_S, label=f"workspace_prune_{issue_key}"
        )
    except WorkspaceCleanupError as exc:
        await _write_audit_event(
            action=_AUDIT_WORKSPACE_AUTO_PRUNED,
            path=path,
            issue_key=issue_key,
            iter_n=iter_n,
            freed_mb=0,
            success=False,
            error=str(exc),
        )
        return asdict(
            WorkspacePruneResult(
                path=path, success=False, freed_mb=0, error=str(exc)
            )
        )

    if result["exit_code"] != 0:
        err = f"rm -rf exit_code={result['exit_code']}: {result['stderr'][:200]}"
        await _write_audit_event(
            action=_AUDIT_WORKSPACE_AUTO_PRUNED,
            path=path,
            issue_key=issue_key,
            iter_n=iter_n,
            freed_mb=0,
            success=False,
            error=err,
        )
        return asdict(
            WorkspacePruneResult(path=path, success=False, freed_mb=0, error=err)
        )

    freed_mb = 0
    out = (result["stdout"] or "").strip()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("FREED="):
            try:
                freed_mb = int(line.split("=", 1)[1])
            except (TypeError, ValueError):
                freed_mb = 0

    if freed_mb == 0 and expected_size_mb > 0:
        # The directory was probably gone before our du could measure it;
        # use the workflow's expected size as a best-effort estimate so the
        # running usage estimate moves correctly.
        freed_mb = expected_size_mb

    await _write_audit_event(
        action=_AUDIT_WORKSPACE_AUTO_PRUNED,
        path=path,
        issue_key=issue_key,
        iter_n=iter_n,
        freed_mb=freed_mb,
        success=True,
        error=None,
    )

    return asdict(
        WorkspacePruneResult(
            path=path, success=True, freed_mb=freed_mb, error=None
        )
    )
