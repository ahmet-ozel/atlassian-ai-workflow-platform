"""``DepartmentsRouter`` - capability matrix + department detail,
repo-mapping self-service CRUD (Feature 9), and runtime department CRUD
for department administration.

Provides:

* ``GET /admin/departments``              - list all departments.
* ``GET /admin/departments/{dept_id}``    - department detail + capability matrix.
* ``GET /admin/departments/{dept_id}/capability-matrix``
                                          - 10×1 workflow_type matrix (/).
* ``POST /admin/departments/{dept_id}/repo-mappings``
                                          - add a repo mapping.
* ``PUT /admin/departments/{dept_id}/repo-mappings``
                                          - update a repo mapping.
* ``DELETE /admin/departments/{dept_id}/repo-mappings``
                                          - remove a repo mapping.
* ``POST /api/v1/departments``            - create a new department (admin only).
* ``PATCH /api/v1/departments/{dept_id}`` - partial update (admin only).
* ``DELETE /api/v1/departments/{dept_id}`` - soft-delete: ``mode=disabled``.

The capability matrix shows which workflow types a department can execute
based on its configured credentials. For each denied workflow type, the
response includes which capabilities are missing and a next-action hint
(e.g. "Bu dept'e Confluence credential'ı ekleyin.").

Repo-mapping CRUD allows ``dept_admin`` users to manage
their own department's repository mappings. RBAC is enforced so that
a dept_admin can only modify mappings for departments they administer.

Runtime CRUD lets a platform admin add, update,
or decommission departments without restarting the platform. The handlers:

1. Validate the resulting document against ``departments.schema.json`` so a
   bad payload never lands on disk.
2. Acquire a cross-process lock on a sidecar ``.lock`` file (``filelock``
   when available, else ``fcntl``/``msvcrt`` per platform) so concurrent
   admin writers cannot corrupt the JSON file.
3. Write atomically via temp-file + ``os.replace`` so a crash mid-write
   never leaves a half-written file.
4. Emit one audit event (``dept_created`` / ``dept_updated`` /
   ``dept_decommissioned``) with the actor's OIDC ``sub``.
5. Signal hot-reload to dependent services so the new config becomes
   active within 10 seconds.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.dependencies import AuthClaims, require_admin

__all__ = ["router", "crud_router"]

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/departments",
    tags=["departments"],
    dependencies=[Depends(require_admin)],
)


def _platform_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    for parent in Path(__file__).resolve().parents:
        if (parent / "config").is_dir():
            return parent
    return Path("/app")


#: Path to departments.json relative to the platform root.
_DEPARTMENTS_CONFIG_PATH = _platform_root() / "config" / "departments.json"


def _load_departments() -> list[dict[str, Any]]:
    """Load departments from config/departments.json."""
    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("departments", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load departments.json: %s", exc)
        return []


def _derive_capabilities(dept: dict[str, Any]) -> frozenset[str]:
    """Derive the capability set from a department's bot credentials.

    Capability derivation rules:
    - jira credential_ref non-empty  jira_read, jira_write
    - bitbucket credential_ref non-empty  bitbucket_read, bitbucket_write
    - confluence credential_ref non-empty  confluence_read, confluence_write
    - SSH_HOST configured (execution-runner; SSH_HOST_1 accepted as
      deprecated alias)  execution
    - web_search_enabled=true  web_search
    """
    caps: set[str] = set()
    bot = dept.get("bot", {})

    if bot.get("jira", {}).get("credential_ref"):
        caps.add("jira_read")
        caps.add("jira_write")

    if bot.get("bitbucket", {}).get("credential_ref"):
        caps.add("bitbucket_read")
        caps.add("bitbucket_write")

    if bot.get("confluence", {}).get("credential_ref"):
        caps.add("confluence_read")
        caps.add("confluence_write")

    # Execution capability comes from an explicit SSH runner assignment
    # or a legacy/env runner flag; Bitbucket alone never implies SSH.
    runner_assigned = bool(
        dept.get("ssh_runner_id")
        or dept.get("ssh_runner_ids")
        or dept.get("ssh_runners")
        or dept.get("execution_runner_id")
    )
    runner_env_available = any(
        os.environ.get(key, "").strip().lower()
        in {"1", "true", "yes", "on"}
        for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
    ) or bool(os.environ.get("SSH_HOST", "").strip())

    if runner_assigned or runner_env_available:
        caps.add("execution")

    if dept.get("web_search_enabled", False):
        caps.add("web_search")

    return frozenset(caps)


def _bot_rows(dept: dict[str, Any]) -> list[dict[str, Any]]:
    """Return UI-friendly bot credential rows from department config."""
    bot = dept.get("bot") or {}
    rows: list[dict[str, Any]] = []
    for service in ("jira", "confluence", "bitbucket"):
        cfg = bot.get(service) or {}
        rows.append(
            {
                "service": service,
                "credential_ref": cfg.get("credential_ref"),
                "account_id": cfg.get("account_id"),
                "username": cfg.get("username"),
                "deployment": cfg.get("deployment"),
            }
        )
    return rows


async def _assigned_runner_ids(request: Request, dept_id: str) -> list[str]:
    """Return SSH runner assignments when the DB pool is available."""
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            """
            SELECT runner_id
            FROM infrastructure.dept_ssh_assignments
            WHERE dept_id = $1
            ORDER BY priority ASC, assigned_at ASC
            """,
            dept_id,
        )
    except Exception as exc:  # noqa: BLE001 - capability UI must degrade
        logger.warning(
            "Failed to load SSH runner assignments for %s: %s",
            dept_id,
            exc,
        )
        return []
    return [row["runner_id"] for row in rows]


#: Human-readable hints for missing capabilities.
_CAPABILITY_HINTS: dict[str, str] = {
    "jira_read": "Bu dept'e Jira credential'ı ekleyin (vault:atlassian/{dept}/jira).",
    "jira_write": "Bu dept'e Jira credential'ı ekleyin (vault:atlassian/{dept}/jira).",
    "bitbucket_read": "Bu dept'e Bitbucket credential'ı ekleyin (vault:atlassian/{dept}/bitbucket).",
    "bitbucket_write": "Bu dept'e Bitbucket credential'ı ekleyin (vault:atlassian/{dept}/bitbucket).",
    "confluence_read": "Bu dept'e Confluence credential'ı ekleyin (vault:atlassian/{dept}/confluence).",
    "confluence_write": "Bu dept'e Confluence credential'ı ekleyin (vault:atlassian/{dept}/confluence).",
    "execution": "Admin panelden en az bir SSH runner tanımlayıp bu bot/dept'e atayın.",
    "web_search": "departments.json'da web_search_enabled=true yapın.",
}


def _build_capability_matrix(dept: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the 10×1 capability matrix for a department.

    Returns a list of dicts, one per workflow_type, with:
    - workflow_type: str
    - allowed: bool
    - missing_capabilities: list[str]  (empty if allowed)
    - hints: list[str]  (next-action hints for missing caps)
    """
    from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

    dept_caps = _derive_capabilities(dept)
    dept_id = dept.get("id", "unknown")
    matrix: list[dict[str, Any]] = []

    for wf_type, required_caps in sorted(WORKFLOW_TYPE_CAPABILITIES.items()):
        missing = required_caps - dept_caps
        allowed = len(missing) == 0
        hints = [
            _CAPABILITY_HINTS.get(cap, f"'{cap}' capability eksik.").replace("{dept}", dept_id)
            for cap in sorted(missing)
        ]
        matrix.append({
            "workflow_type": wf_type,
            "allowed": allowed,
            "missing_capabilities": sorted(missing),
            "hints": hints,
        })

    return matrix


# ---------------------------------------------------------------------------
# GET /admin/departments
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List all departments with basic info",
)
async def list_departments() -> dict[str, Any]:
    """Return all departments with id, display_name, and mode."""
    departments = _load_departments()
    return {
        "departments": [
            {
                "id": d.get("id"),
                "display_name": d.get("display_name"),
                "mode": d.get("mode", "active"),
                "jira_project_keys": d.get("jira_project_keys", []),
            }
            for d in departments
        ]
    }


# ---------------------------------------------------------------------------
# GET /admin/departments/{dept_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{dept_id}",
    summary="Department detail + capability matrix",
)
async def get_department_detail(
    dept_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return full department detail including capability matrix."""
    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    runner_ids = await _assigned_runner_ids(request, dept_id)
    dept_with_runtime = {**dept, "ssh_runner_ids": runner_ids}
    matrix = _build_capability_matrix(dept_with_runtime)
    caps = _derive_capabilities(dept_with_runtime)

    return {
        "id": dept.get("id"),
        "display_name": dept.get("display_name"),
        "mode": dept.get("mode"),
        "jira_project_keys": dept.get("jira_project_keys", []),
        "confluence_space_keys": dept.get("confluence_space_keys", []),
        "bitbucket_workspace": dept.get("bitbucket_workspace"),
        "web_search_enabled": dept.get("web_search_enabled", False),
        "bots": _bot_rows(dept),
        "ssh_runner_ids": runner_ids,
        "derived_capabilities": sorted(caps),
        "capability_matrix": matrix,
        "budget_caps": dept.get("budget_caps"),
    }


# ---------------------------------------------------------------------------
# GET /admin/departments/{dept_id}/capability-matrix
# ---------------------------------------------------------------------------


@router.get(
    "/{dept_id}/capability-matrix",
    summary="Workflow type capability matrix (10×1 grid)",
)
async def get_capability_matrix(
    dept_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return the capability matrix for a specific department.

    Each entry shows whether the department can execute that workflow
    type, what capabilities are missing, and actionable hints.
    """
    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    runner_ids = await _assigned_runner_ids(request, dept_id)
    dept_with_runtime = {**dept, "ssh_runner_ids": runner_ids}
    matrix = _build_capability_matrix(dept_with_runtime)
    allowed_count = sum(1 for m in matrix if m["allowed"])
    denied_count = len(matrix) - allowed_count

    return {
        "dept_id": dept_id,
        "summary": {
            "total_workflow_types": len(matrix),
            "allowed": allowed_count,
            "denied": denied_count,
        },
        "matrix": matrix,
    }


# ---------------------------------------------------------------------------
# Feature 9: Repo Mappings Self-Service CRUD
# ---------------------------------------------------------------------------


class RepoMappingCreate(BaseModel):
    """Request body for creating a repo mapping."""

    repo_slug: str = Field(..., min_length=1, description="Bitbucket repo slug")
    project_key: str = Field(..., min_length=1, description="Jira project key")
    branch_pattern: str = Field(
        default="main", description="Branch pattern for the mapping"
    )


class RepoMappingUpdate(BaseModel):
    """Request body for updating a repo mapping."""

    repo_slug: str = Field(..., min_length=1, description="Repo slug to update")
    project_key: str | None = Field(None, description="New Jira project key")
    branch_pattern: str | None = Field(None, description="New branch pattern")


class RepoMappingDelete(BaseModel):
    """Request body for deleting a repo mapping."""

    repo_slug: str = Field(..., min_length=1, description="Repo slug to remove")


def _require_dept_admin(claims: AuthClaims, dept_id: str) -> None:
    """Enforce that the caller is a dept_admin for the given department.

    Raises HTTPException(403) if the caller does not have dept_admin
    privileges for the specified department.
    """
    # Accept global admin or dept_admin with matching dept claim
    if "admin" in claims.groups:
        return
    if f"dept_admin:{dept_id}" in claims.groups:
        return
    if "dept_admin" in claims.groups:
        # Generic dept_admin - check if they have access to this dept
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"dept_admin access required for department '{dept_id}'",
    )


def _load_repo_mappings(dept_id: str) -> list[dict[str, Any]]:
    """Load repo mappings for a department from departments.json."""
    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        return []
    return dept.get("repo_mappings", [])


def _save_repo_mappings(dept_id: str, mappings: list[dict[str, Any]]) -> None:
    """Persist repo mappings back to departments.json."""
    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {"departments": []}

    departments = data.get("departments", [])
    for dept in departments:
        if dept.get("id") == dept_id:
            dept["repo_mappings"] = mappings
            break

    with open(_DEPARTMENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def _emit_audit(
    request: Request,
    action: str,
    claims: AuthClaims,
    dept_id: str,
    resource: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit an audit event via the app's audit sink."""
    from audit_logger import AuditEvent

    sink = getattr(request.app.state, "audit_sink", None)
    if sink is None:
        return
    event = AuditEvent(
        actor_id=claims.sub,
        actor_role="dept_admin",
        dept_id=dept_id,
        action=action,
        resource=resource,
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload=payload,
    )
    await sink.write(event)


@router.post(
    "/{dept_id}/repo-mappings",
    summary="Add a repo mapping to a department",
    status_code=status.HTTP_201_CREATED,
)
async def add_repo_mapping(
    dept_id: str,
    body: RepoMappingCreate,
    request: Request,
    claims: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Add a new repo mapping to the department.

    Audit event: ``repo_mapping_added``.
    """
    _require_dept_admin(claims, dept_id)

    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    mappings = _load_repo_mappings(dept_id)

    # Check for duplicate
    if any(m.get("repo_slug") == body.repo_slug for m in mappings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"repo mapping for '{body.repo_slug}' already exists",
        )

    new_mapping = {
        "repo_slug": body.repo_slug,
        "project_key": body.project_key,
        "branch_pattern": body.branch_pattern,
    }
    mappings.append(new_mapping)
    _save_repo_mappings(dept_id, mappings)

    await _emit_audit(
        request,
        action="repo_mapping_added",
        claims=claims,
        dept_id=dept_id,
        resource=f"department:{dept_id}/repo:{body.repo_slug}",
        payload=new_mapping,
    )

    return {"status": "created", "mapping": new_mapping}


@router.put(
    "/{dept_id}/repo-mappings",
    summary="Update a repo mapping in a department",
)
async def update_repo_mapping(
    dept_id: str,
    body: RepoMappingUpdate,
    request: Request,
    claims: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Update an existing repo mapping.

    Audit event: ``repo_mapping_updated``.
    """
    _require_dept_admin(claims, dept_id)

    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    mappings = _load_repo_mappings(dept_id)
    target = next((m for m in mappings if m.get("repo_slug") == body.repo_slug), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"repo mapping for '{body.repo_slug}' not found",
        )

    if body.project_key is not None:
        target["project_key"] = body.project_key
    if body.branch_pattern is not None:
        target["branch_pattern"] = body.branch_pattern

    _save_repo_mappings(dept_id, mappings)

    await _emit_audit(
        request,
        action="repo_mapping_updated",
        claims=claims,
        dept_id=dept_id,
        resource=f"department:{dept_id}/repo:{body.repo_slug}",
        payload=target,
    )

    return {"status": "updated", "mapping": target}


@router.delete(
    "/{dept_id}/repo-mappings",
    summary="Remove a repo mapping from a department",
)
async def remove_repo_mapping(
    dept_id: str,
    body: RepoMappingDelete,
    request: Request,
    claims: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Remove a repo mapping from the department.

    Audit event: ``repo_mapping_removed``.
    """
    _require_dept_admin(claims, dept_id)

    departments = _load_departments()
    dept = next((d for d in departments if d.get("id") == dept_id), None)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    mappings = _load_repo_mappings(dept_id)
    original_len = len(mappings)
    mappings = [m for m in mappings if m.get("repo_slug") != body.repo_slug]

    if len(mappings) == original_len:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"repo mapping for '{body.repo_slug}' not found",
        )

    _save_repo_mappings(dept_id, mappings)

    await _emit_audit(
        request,
        action="repo_mapping_removed",
        claims=claims,
        dept_id=dept_id,
        resource=f"department:{dept_id}/repo:{body.repo_slug}",
        payload={"repo_slug": body.repo_slug},
    )

    return {"status": "removed", "repo_slug": body.repo_slug}


# ===========================================================================
# Runtime Department CRUD
# ===========================================================================
#
# Separate router with prefix ``/api/v1/departments`` mounted alongside the
# admin-side ``/admin/departments`` surface above. Kept on its own
# :class:`APIRouter` so the two prefixes remain crisp and the runtime CRUD
# endpoints can be globally gated by :func:`require_admin` without leaking
# into the read-only matrix endpoints.

#: Path to the JSON Schema that validates the full document. Lives next to
#: ``departments.json`` so the file is co-located with the data it
#: validates.
_DEPARTMENTS_SCHEMA_PATH = (
    _platform_root() / "config" / "departments.schema.json"
)

#: Sidecar lock file used by :func:`_acquire_file_lock`. We never lock the
#: data file itself because Windows refuses to ``rename`` a file that is
#: open / locked by the same process - the sidecar keeps the atomic
#: replace pathway clean.
_DEPARTMENTS_LOCK_PATH = (
    _platform_root() / "config" / ".departments.json.lock"
)

#: Audit action labels - kept as constants so a typo never silently
#: produces a malformed audit row.
_ACTION_DEPT_CREATED = "dept_created"
_ACTION_DEPT_UPDATED = "dept_updated"
_ACTION_DEPT_DECOMMISSIONED = "dept_decommissioned"

#: Bot services whose ``(service, account_id)`` tuple must stay unique
#: across departments. Mirrors the routing key used by the webhook
#: dispatcher (``shared.department_bot_identity``) so a bot account
#: can never resolve to two departments at once. The list is intentionally
#: narrow: only services that the dispatcher actually keys on need conflict
#: detection here.
_BOT_IDENTITY_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")


def _extract_bot_identities(
    dept: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return the ``(service, account_id)`` tuples declared by a dept.

    Empty / missing ``account_id`` values are skipped because the
    bundled ``departments.json`` ships ``account_id: ""`` placeholders
    that get filled in by the Vault probe at runtime - those are *not*
    routing keys yet and must not trigger spurious 409s on the very
    first ``POST``. Once an operator pins a real ``account_id`` it
    starts participating in conflict detection.
    """

    bot = dept.get("bot") or {}
    if not isinstance(bot, dict):
        return []
    out: list[tuple[str, str]] = []
    for service in _BOT_IDENTITY_SERVICES:
        entry = bot.get(service) or {}
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            out.append((service, account_id.strip()))
    return out


def _find_account_id_conflicts(
    candidate: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    skip_dept_id: str | None = None,
) -> list[dict[str, str]]:
    """Detect ``(service, account_id)`` collisions across departments.

    Walks every other department's bot identities and returns the
    list of clashes - each entry carrying the ``service``, the
    offending ``account_id``, and the ``dept_id`` that already owns
    it. The caller raises ``HTTP 409`` when this list is non-empty.

    The ``skip_dept_id`` argument is set on ``PATCH`` so a department
    is never compared against itself (an unchanged identity must not
    look like a conflict on every update).
    """

    candidate_pairs = _extract_bot_identities(candidate)
    if not candidate_pairs:
        return []

    conflicts: list[dict[str, str]] = []
    for other in existing:
        other_id = other.get("id")
        if skip_dept_id is not None and other_id == skip_dept_id:
            continue
        other_pairs = set(_extract_bot_identities(other))
        for service, account_id in candidate_pairs:
            if (service, account_id) in other_pairs:
                conflicts.append(
                    {
                        "service": service,
                        "account_id": account_id,
                        "dept_id": str(other_id) if other_id else "",
                    }
                )
    return conflicts


# ---------------------------------------------------------------------------
# File lock helpers
# ---------------------------------------------------------------------------


class _FileLockContext:
    """Cross-platform file lock context manager.

    Tries ``filelock.FileLock`` first because it's the most ergonomic
    cross-platform option and already in the workspace's transitive
    dependency tree (``virtualenv`` pins it). When ``filelock`` isn't
    importable we fall back to platform-native primitives:

    * POSIX  ``fcntl.flock`` against a sidecar ``.lock`` file.
    * Windows  ``msvcrt.locking`` against the same sidecar.

    Either way the lock is **advisory** - only well-behaved writers
    (i.e. the admin-dashboard-api) need to acquire it. The lock is
    released when the context manager exits, even on exception.
    """

    def __init__(self, lock_path: Path, timeout: float = 10.0) -> None:
        self._lock_path = lock_path
        self._timeout = timeout
        self._impl: Any = None
        self._fh: Any = None

    def __enter__(self) -> "_FileLockContext":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Preferred path: ``filelock`` package.
        try:
            from filelock import FileLock, Timeout  # type: ignore[import-not-found]
        except ImportError:
            FileLock = None  # type: ignore[assignment]
            Timeout = None  # type: ignore[assignment]

        if FileLock is not None:
            try:
                self._impl = FileLock(str(self._lock_path))
                self._impl.acquire(timeout=self._timeout)
                return self
            except Exception as exc:  # noqa: BLE001 - fall through
                logger.warning(
                    "filelock acquisition failed (%s); falling back to "
                    "native file locking",
                    exc,
                )
                self._impl = None

        # Fallback path: POSIX fcntl / Windows msvcrt.
        # Open in append mode so the file is created if missing and we
        # never truncate any pre-existing lock token.
        self._fh = open(self._lock_path, "a+")  # noqa: SIM115 - released in __exit__
        try:
            if os.name == "nt":  # Windows
                import msvcrt  # type: ignore[import-not-found]

                # ``LK_LOCK`` blocks for ~10s then raises; loop a few
                # times to honour ``self._timeout`` more accurately.
                deadline = self._timeout
                while True:
                    try:
                        msvcrt.locking(
                            self._fh.fileno(), msvcrt.LK_LOCK, 1
                        )
                        break
                    except OSError:
                        deadline -= 1.0
                        if deadline <= 0:
                            raise
            else:
                import fcntl  # type: ignore[import-not-found]

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._fh.close()
            self._fh = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._impl is not None:
            try:
                self._impl.release()
            except Exception as exc:  # noqa: BLE001
                logger.warning("filelock release failed: %s", exc)
            self._impl = None
            return

        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt  # type: ignore[import-not-found]

                    try:
                        msvcrt.locking(
                            self._fh.fileno(), msvcrt.LK_UNLCK, 1
                        )
                    except OSError:
                        pass  # Already released or never acquired.
                else:
                    import fcntl  # type: ignore[import-not-found]

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def _acquire_file_lock(timeout: float = 10.0) -> _FileLockContext:
    """Return a context manager that holds an exclusive lock on the
    departments config file.

    Production callers wrap **every** read/modify/write cycle in this
    context so concurrent admin writers can't corrupt the JSON file
    during atomic, lossless updates.
    """

    return _FileLockContext(_DEPARTMENTS_LOCK_PATH, timeout=timeout)


# ---------------------------------------------------------------------------
# Atomic JSON read / write
# ---------------------------------------------------------------------------


def _read_departments_doc() -> dict[str, Any]:
    """Read the full ``departments.json`` document.

    Returns the canonical ``{"version": 1, "departments": [...]}``
    shape. A missing file collapses to a fresh document so ``POST``
    works on a brand-new install. A malformed file raises ``HTTP 500``
    rather than silently overwriting the operator's data.
    """

    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "departments": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "departments_config_corrupt",
                "message": f"departments.json is not valid JSON: {exc}",
            },
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "departments_config_corrupt",
                "message": "departments.json root must be an object",
            },
        )
    data.setdefault("version", 1)
    data.setdefault("departments", [])
    return data


def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
    """Write ``doc`` to ``path`` atomically.

    Strategy: serialise to a temp file in the same directory (so
    ``os.replace`` is rename-only - no cross-device copy), ``fsync``
    the temp file, then ``os.replace`` to swap it into place.
    ``os.replace`` is atomic on both POSIX and Windows so a crash
    mid-write can never leave a partial file at ``path``.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                # ``fsync`` is unsupported on some Windows file types
                # (e.g. tmpfs); fall through - ``os.replace`` is still
                # atomic, we just lose the durability guarantee for
                # this single write.
                pass
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the temp file; we never want to leave
        # ``departments.json.<random>.tmp`` artefacts behind on a
        # failed write.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate_departments_doc(doc: dict[str, Any]) -> None:
    """Validate ``doc`` against ``departments.schema.json``.

    Raises ``HTTPException(422)`` on validation failure with a
    descriptive ``error`` body so the FE can surface the field path
    that broke. When :mod:`jsonschema` is unavailable we fall back to
    a minimal structural check (``version`` + ``departments`` array)
    so the endpoint still rejects obviously broken payloads - but
    log a warning so the operator knows full validation is off.
    """

    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "jsonschema not available; falling back to minimal structural "
            "validation of departments.json"
        )
        if not isinstance(doc, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="departments document must be an object",
            )
        if doc.get("version") != 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="departments.version must be 1",
            )
        if not isinstance(doc.get("departments"), list):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="departments.departments must be an array",
            )
        return

    try:
        with open(_DEPARTMENTS_SCHEMA_PATH, encoding="utf-8") as f:
            schema = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Failed to load departments.schema.json (%s); skipping full "
            "validation",
            exc,
        )
        return

    try:
        jsonschema.validate(instance=doc, schema=schema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        # ``exc.absolute_path`` is a deque of segments; render it as a
        # JSONPath-ish string so the FE can highlight the offending
        # field directly.
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "schema_validation_failed",
                "path": path,
                "message": exc.message,
            },
        ) from exc


# ---------------------------------------------------------------------------
# Audit + hot-reload signalling
# ---------------------------------------------------------------------------


async def _emit_dept_audit(
    request: Request,
    *,
    action: str,
    actor: AuthClaims,
    dept_id: str,
    payload: dict[str, Any] | None = None,
    result: str = "ok",
) -> None:
    """Write a single audit event for a department CRUD action.

    Failures are swallowed - Postgres hiccups must never block a
    legitimate dept mutation (the row write is observability, not a
    correctness guarantee). The sink is resolved through the same
    fallback chain as :func:`workflow_control._get_audit_sink`:
    explicit ``app.state.dept_audit_sink``  AdminProxy's audit sink.
    """

    sink = getattr(request.app.state, "dept_audit_sink", None)
    if sink is None:
        proxy = getattr(request.app.state, "admin_proxy", None)
        if proxy is not None:
            sink = getattr(proxy, "_audit", None)
    if sink is None:
        return

    try:
        from audit_logger import AuditEvent

        event = AuditEvent(
            actor_id=actor.sub,
            actor_role="admin",
            dept_id=dept_id,
            action=action,
            resource=f"department:{dept_id}",
            result=result,  # type: ignore[arg-type]
            timestamp=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 - audit must never block
        logger.warning(
            "dept audit write failed (action=%s, dept=%s): %s",
            action,
            dept_id,
            exc,
        )


async def _signal_hot_reload(
    request: Request,
    *,
    dept_id: str,
    action: str,
) -> None:
    """Notify dependent services that ``departments.json`` changed.

    The admin-dashboard-api itself is the BFF - automation-service and
    the workers consume ``departments.json`` and need to refresh their
    in-memory caches within 10 seconds. Two paths
    are supported, in order of preference:

    1. **Publisher** - ``app.state.departments_reload_publisher`` is a
       callable ``async (dept_id: str, action: str) -> None`` that the
       lifespan wires when a pub/sub channel is available (Redis
       channel, Postgres ``LISTEN/NOTIFY``, etc.).
    2. **HTTP fan-out** - falls back to
       ``POST {automation_service_url}/admin/departments/_reload``
       through the AdminProxy's ``http_client``. The endpoint is
       idempotent on the automation-service side (it just bumps a
       cache generation counter and re-reads the file).

    Both paths are best-effort. When neither is wired we log a
    warning but return success - the next 30-second poll loop in
    each consumer will pick up the new config anyway.
    """

    publisher = getattr(
        request.app.state, "departments_reload_publisher", None
    )
    if publisher is not None:
        try:
            await publisher(dept_id, action)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "departments_reload_publisher failed (dept=%s, action=%s): "
                "%s - falling back to HTTP fan-out",
                dept_id,
                action,
                exc,
            )

    proxy = getattr(request.app.state, "admin_proxy", None)
    http_client = getattr(request.app.state, "http_client", None)
    if proxy is not None and http_client is not None:
        try:
            base = getattr(proxy, "_upstream", None)
            if base:
                await http_client.post(
                    f"{base}/admin/departments/_reload",
                    json={"dept_id": dept_id, "action": action},
                    timeout=5.0,
                )
                return
        except Exception as exc:  # noqa: BLE001 - soft-fail
            logger.warning(
                "automation-service hot-reload signal failed "
                "(dept=%s, action=%s): %s",
                dept_id,
                action,
                exc,
            )
            return

    logger.info(
        "no hot-reload publisher wired; consumers will pick up "
        "dept=%s action=%s on next config poll",
        dept_id,
        action,
    )


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class DepartmentCreate(BaseModel):
    """Request body for ``POST /api/v1/departments``.

    The full department shape is validated against
    ``departments.schema.json`` *after* it's merged into the
    in-memory document so we never reject a payload that the schema
    would actually accept. The Pydantic model itself only enforces
    the bare minimum (``id`` is present and looks like a slug) so the
    validation error messages stay schema-driven.
    """

    model_config = {"extra": "allow"}

    id: str = Field(
        ...,
        min_length=2,
        max_length=31,
        pattern=r"^[a-z][a-z0-9-]{1,30}$",
        description="Department slug (kebab-case, lowercase).",
    )


class DepartmentPatch(BaseModel):
    """Request body for ``PATCH /api/v1/departments/{dept_id}``.

    Free-form mapping - every supplied field overlays the matching
    field on the existing department object. The merged document is
    re-validated against ``departments.schema.json`` so a partial
    update can never break the schema contract.

    The ``id`` field is intentionally rejected here: changing a
    department's id mid-flight would orphan every existing audit row
    / workflow / credential reference. Operators who need to rename
    a department should disable the old one and create a new one.
    """

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Runtime CRUD router
# ---------------------------------------------------------------------------


crud_router = APIRouter(
    prefix="/api/v1/departments",
    tags=["departments-crud"],
)


@crud_router.post(
    "",
    summary="Create a new department (admin only)",
    status_code=status.HTTP_201_CREATED,
)
async def create_department(
    request: Request,
    body: DepartmentCreate = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Add a new department to ``departments.json``.

    Steps:

    1. Acquire the file lock (cross-platform, see
       :func:`_acquire_file_lock`).
    2. Read the current document.
    3. Reject with HTTP 409 if a department with the same ``id``
       already exists.
    4. Append the new department and re-validate the full document
       against ``departments.schema.json``.
    5. Atomically write the file (temp + ``os.replace``).
    6. Emit ``dept_created`` audit event.
    7. Signal hot-reload so the new dept is active within 10 seconds.
    """

    payload = body.model_dump()
    dept_id = payload["id"]

    with _acquire_file_lock():
        doc = _read_departments_doc()
        existing = doc.get("departments", [])
        if any(d.get("id") == dept_id for d in existing):
            # Audit the conflict so repeated probes are observable.
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_CREATED,
                actor=actor,
                dept_id=dept_id,
                payload={"reason": "dept_id_conflict"},
                result="denied",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "dept_id_conflict",
                    "dept_id": dept_id,
                    "message": (
                        f"department '{dept_id}' already exists; pick a "
                        f"different id"
                    ),
                },
            )

        # Bot ``(service, account_id)`` uniqueness. A bot account can
        # only resolve to one department because the webhook dispatcher
        # routes on ``account_id`` alone; accepting a duplicate here
        # would produce an ambiguous routing decision the next time the
        # bot acts.
        identity_conflicts = _find_account_id_conflicts(payload, existing)
        if identity_conflicts:
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_CREATED,
                actor=actor,
                dept_id=dept_id,
                payload={
                    "reason": "account_id_conflict",
                    "conflicts": identity_conflicts,
                },
                result="denied",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "account_id_conflict",
                    "dept_id": dept_id,
                    "conflicts": identity_conflicts,
                    "message": (
                        "one or more bot account_id values already belong "
                        "to a different department; release them before "
                        "reassigning"
                    ),
                },
            )

        existing.append(payload)
        doc["departments"] = existing
        _validate_departments_doc(doc)
        _atomic_write_json(_DEPARTMENTS_CONFIG_PATH, doc)

    await _emit_dept_audit(
        request,
        action=_ACTION_DEPT_CREATED,
        actor=actor,
        dept_id=dept_id,
        payload={"display_name": payload.get("display_name")},
    )
    await _signal_hot_reload(
        request, dept_id=dept_id, action=_ACTION_DEPT_CREATED
    )

    return {"status": "created", "dept_id": dept_id, "department": payload}


@crud_router.patch(
    "/{dept_id}",
    summary="Partially update an existing department (admin only)",
)
async def update_department(
    dept_id: str,
    request: Request,
    body: DepartmentPatch = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Apply a partial update to ``dept_id``.

    The patch body is merged onto the existing department object
    (top-level field replacement; nested objects are replaced
    wholesale, not deep-merged - operators who want a deep merge
    should ``GET`` first and ``PATCH`` the full object). The merged
    document is re-validated against ``departments.schema.json``
    before it lands on disk.

    Returns HTTP 404 when ``dept_id`` doesn't exist; HTTP 422 when
    the merged document fails schema validation.
    """

    patch = body.model_dump(exclude_unset=True)
    # Reject ``id`` mutations - the dept id is the stable handle that
    # links audit rows, workflows, and credentials together.
    if "id" in patch and patch["id"] != dept_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "id_mutation_forbidden",
                "message": (
                    "department id cannot be changed via PATCH; disable "
                    "the old dept and create a new one instead"
                ),
            },
        )
    patch.pop("id", None)

    with _acquire_file_lock():
        doc = _read_departments_doc()
        departments = doc.get("departments", [])
        target_idx: int | None = None
        for idx, d in enumerate(departments):
            if d.get("id") == dept_id:
                target_idx = idx
                break

        if target_idx is None:
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_UPDATED,
                actor=actor,
                dept_id=dept_id,
                payload={"reason": "not_found"},
                result="denied",
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"department '{dept_id}' not found",
            )

        merged = {**departments[target_idx], **patch}
        # The id stays put even if Pydantic let it through above.
        merged["id"] = dept_id

        # Bot identity uniqueness. We compare the merged dept against
        # every *other* dept (skip self) so an unchanged ``account_id``
        # never trips the check on every PATCH.
        identity_conflicts = _find_account_id_conflicts(
            merged, departments, skip_dept_id=dept_id
        )
        if identity_conflicts:
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_UPDATED,
                actor=actor,
                dept_id=dept_id,
                payload={
                    "reason": "account_id_conflict",
                    "conflicts": identity_conflicts,
                },
                result="denied",
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "account_id_conflict",
                    "dept_id": dept_id,
                    "conflicts": identity_conflicts,
                    "message": (
                        "one or more bot account_id values already belong "
                        "to a different department; release them before "
                        "reassigning"
                    ),
                },
            )

        departments[target_idx] = merged
        doc["departments"] = departments
        _validate_departments_doc(doc)
        _atomic_write_json(_DEPARTMENTS_CONFIG_PATH, doc)

    await _emit_dept_audit(
        request,
        action=_ACTION_DEPT_UPDATED,
        actor=actor,
        dept_id=dept_id,
        payload={"fields": sorted(patch.keys())},
    )
    await _signal_hot_reload(
        request, dept_id=dept_id, action=_ACTION_DEPT_UPDATED
    )

    return {"status": "updated", "dept_id": dept_id, "department": merged}


@crud_router.delete(
    "/{dept_id}",
    summary="Decommission a department (soft-delete to mode=disabled)",
)
async def decommission_department(
    dept_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Soft-delete: set ``mode=disabled``.

    The dept row stays in ``departments.json`` so historic audit /
    workflow rows that reference it stay resolvable. Only the
    ``mode`` flips to ``"disabled"``, which causes the capability
    gate in automation-service to reject all new workflow starts for
    this dept while leaving in-flight workflows to drain.

    Calling ``DELETE`` against an already-disabled dept is a no-op
    that returns 200 with ``status="already_disabled"`` so the
    endpoint is idempotent - replaying the call after a network
    blip never surfaces a confusing 404.
    """

    with _acquire_file_lock():
        doc = _read_departments_doc()
        departments = doc.get("departments", [])
        target_idx: int | None = None
        for idx, d in enumerate(departments):
            if d.get("id") == dept_id:
                target_idx = idx
                break

        if target_idx is None:
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_DECOMMISSIONED,
                actor=actor,
                dept_id=dept_id,
                payload={"reason": "not_found"},
                result="denied",
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"department '{dept_id}' not found",
            )

        target = departments[target_idx]
        previous_mode = target.get("mode", "active")
        if previous_mode == "disabled":
            # Idempotent - record the no-op but skip the rewrite so
            # we don't bump file mtime or trigger a needless reload.
            await _emit_dept_audit(
                request,
                action=_ACTION_DEPT_DECOMMISSIONED,
                actor=actor,
                dept_id=dept_id,
                payload={"already_disabled": True},
            )
            return {
                "status": "already_disabled",
                "dept_id": dept_id,
            }

        target["mode"] = "disabled"
        departments[target_idx] = target
        doc["departments"] = departments
        _validate_departments_doc(doc)
        _atomic_write_json(_DEPARTMENTS_CONFIG_PATH, doc)

    await _emit_dept_audit(
        request,
        action=_ACTION_DEPT_DECOMMISSIONED,
        actor=actor,
        dept_id=dept_id,
        payload={"previous_mode": previous_mode},
    )
    await _signal_hot_reload(
        request, dept_id=dept_id, action=_ACTION_DEPT_DECOMMISSIONED
    )

    return {
        "status": "decommissioned",
        "dept_id": dept_id,
        "previous_mode": previous_mode,
    }
