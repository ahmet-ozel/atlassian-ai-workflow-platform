"""``FeatureFlagsRouter`` (`platform-mimari-ops` task 11.6 +
`platform-gap-fill` task 16.1).

**Validates: Requirements R4.6 (mimari-ops Q8) and R16.1–R16.5
(platform-gap-fill — feature flag toggle UI with dept overrides).**

Two surfaces are exposed:

1. ``/admin/feature-flags`` (legacy, mimari-ops) — list + per-flag
   ``PUT`` for the global value. Kept intact so the existing UI and
   audit pipeline continue to work.
2. ``/api/v1/feature-flags`` (gap-fill task 16.1) — listing returns
   global value alongside the per-department override map merged
   from ``departments.json`` ``feature_flag_overrides``. Mutation
   uses ``PATCH /api/v1/feature-flags/{key}`` with body
   ``{value: bool, dept_id?: str}``: if ``dept_id`` is omitted the
   global row is updated, otherwise the dept's override map is
   patched. ``DELETE /api/v1/feature-flags/{key}/overrides/{dept_id}``
   removes a per-dept override (falling back to the global value).

Storage
-------

* Global state: ``shared.feature_flags`` (Postgres, declared in
  ``infra/postgres/init/20_ops.sql``). The schema migration is
  already shipped — this task does **not** add a new migration.
* Per-department overrides: ``platform/config/departments.json`` →
  each department's ``feature_flag_overrides`` ``{flag_name: bool}``
  map (declared in ``departments.schema.json``). Same JSON file the
  runtime CRUD endpoints in :mod:`.routers.departments` mutate.

Audit
-----

Every successful mutation writes one ``feature_flag_toggled`` event
to the canonical audit sink. The payload carries the canonical R16.3
shape::

    {
        "key": <flag name>,
        "scope": "global" | "dept",
        "dept_id": <dept_id> | None,
        "old_value": <bool | None>,
        "new_value": <bool | None>,
    }

A ``new_value=null`` entry on a ``scope="dept"`` event signals an
override removal (the row resolves to the global default again).

Hot-reload
----------

* Global flags — services already poll ``shared.feature_flags`` (see
  :class:`AsyncpgFeatureFlagReader` consumed by
  ``LifecycleService``). The router additionally invokes
  ``app.state.feature_flag_reload_publisher`` when wired so an
  optional pub/sub channel can push the change immediately.
* Dept overrides — reuses the same
  ``app.state.departments_reload_publisher`` /
  ``automation-service /admin/departments/_reload`` fan-out the
  department CRUD endpoints already drive (Requirement 17.4 — 10s
  budget). When neither path is wired we fall back to a structured
  log line and let the standard 30-second config poll pick the
  change up.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = ["router", "v1_router"]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Audit action label written for every mutating action on either
#: surface (Requirement R16.3 / R4.6).
_AUDIT_ACTION: str = "feature_flag_toggled"

#: Path to the canonical departments document — same file the runtime
#: CRUD router mutates. Resolving relative to this module keeps the
#: routers honest when the project is checked out at a non-default
#: location.
def _resolve_config_path(filename: str) -> Path:
    """Return ``config/<filename>`` across repo and container layouts."""

    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        config_dir = parent / "config"
        if config_dir.is_dir():
            return config_dir / filename
    return resolved.parents[min(2, len(resolved.parents) - 1)] / "config" / filename


_DEPARTMENTS_CONFIG_PATH = _resolve_config_path("departments.json")

#: Sidecar lock file shared with :mod:`.routers.departments` so the
#: two routers cannot interleave half-written documents.
_DEPARTMENTS_LOCK_PATH = _resolve_config_path(".departments.json.lock")


router = APIRouter(prefix="/admin/feature-flags", tags=["feature-flags"])
v1_router = APIRouter(prefix="/api/v1/feature-flags", tags=["feature-flags"])


# ---------------------------------------------------------------------------
# Helpers — pool / audit sink resolution
# ---------------------------------------------------------------------------


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "reason": "pg_pool_unavailable"},
        )
    return pool


def _get_audit(request: Request) -> Any:
    sink = getattr(request.app.state, "feature_flag_audit_sink", None)
    if sink is not None:
        return sink
    proxy = getattr(request.app.state, "admin_proxy", None)
    return getattr(proxy, "_audit", None) if proxy is not None else None


async def _emit_audit(
    request: Request,
    *,
    actor: AuthClaims,
    key: str,
    scope: str,
    dept_id: str | None,
    old_value: bool | None,
    new_value: bool | None,
) -> None:
    """Write a single ``feature_flag_toggled`` audit event.

    Failures are swallowed — observability must never block a
    legitimate flag mutation.
    """

    sink = _get_audit(request)
    if sink is None:
        return
    try:
        event = AuditEvent(
            actor_id=actor.sub,
            actor_role="admin",
            dept_id=dept_id,
            action=_AUDIT_ACTION,
            resource=f"feature_flag:{key}",
            result="ok",
            timestamp=datetime.now(tz=timezone.utc),
            payload={
                "key": key,
                "scope": scope,
                "dept_id": dept_id,
                "old_value": old_value,
                "new_value": new_value,
                # Backwards-compat keys consumed by existing dashboards
                # and the mimari-ops audit log integrity tests.
                "name": key,
                "from": old_value,
                "to": new_value,
            },
        )
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 — audit must never block
        logger.warning(
            "feature_flag_toggled audit write failed (key=%s scope=%s): %s",
            key,
            scope,
            exc,
        )


# ---------------------------------------------------------------------------
# Hot-reload propagation
# ---------------------------------------------------------------------------


async def _signal_global_reload(request: Request, *, key: str) -> None:
    """Poke any wired feature-flag pub/sub channel.

    Production wiring is optional; when missing we log a structured
    line so operators can see the change is pending the next 30s
    polling cycle. The publisher contract is intentionally narrow:
    a callable taking ``(key: str)``.
    """

    publisher = getattr(
        request.app.state, "feature_flag_reload_publisher", None
    )
    if publisher is None:
        logger.info(
            "feature_flag_reload_publisher not wired — flag %r will "
            "propagate via the next polling cycle (≤10s SLA, R16.5)",
            key,
        )
        return
    try:
        await publisher(key)
    except Exception as exc:  # noqa: BLE001 — soft-fail
        logger.warning(
            "feature_flag_reload_publisher failed for %r: %s", key, exc
        )


async def _signal_dept_reload(
    request: Request, *, dept_id: str, key: str
) -> None:
    """Reuse the departments hot-reload publisher for dept overrides.

    The departments CRUD router already wires a 10s-bounded reload
    mechanism (Requirement 17.4) — overriding a flag is just another
    departments.json edit, so we ride the same channel rather than
    bolt on a parallel one.
    """

    publisher = getattr(
        request.app.state, "departments_reload_publisher", None
    )
    if publisher is not None:
        try:
            await publisher(dept_id, "feature_flag_overrides_changed")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "departments_reload_publisher failed for dept=%s "
                "(flag=%s): %s — falling back to HTTP fan-out",
                dept_id,
                key,
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
                    json={
                        "dept_id": dept_id,
                        "action": "feature_flag_overrides_changed",
                        "flag": key,
                    },
                    timeout=5.0,
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "automation-service hot-reload signal failed (dept=%s "
                "flag=%s): %s",
                dept_id,
                key,
                exc,
            )
    logger.info(
        "no hot-reload channel wired; dept=%s flag=%s will propagate "
        "on next config poll",
        dept_id,
        key,
    )


# ---------------------------------------------------------------------------
# departments.json read/write helpers (mirror routers.departments)
# ---------------------------------------------------------------------------


class _FileLockContext:
    """Cross-platform advisory lock on ``departments.json``.

    Mirrors the helper in :mod:`.routers.departments` so the two
    routers cannot interleave a half-written document. Implemented
    inline (rather than imported) to keep the dependency graph one-way
    — this router consumes the same JSON file but does not need to
    pull every CRUD helper into its surface.
    """

    def __init__(self, lock_path: Path, timeout: float = 10.0) -> None:
        self._lock_path = lock_path
        self._timeout = timeout
        self._impl: Any = None
        self._fh: Any = None

    def __enter__(self) -> "_FileLockContext":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            from filelock import FileLock  # type: ignore[import-not-found]
        except ImportError:
            FileLock = None  # type: ignore[assignment]

        if FileLock is not None:
            try:
                self._impl = FileLock(str(self._lock_path))
                self._impl.acquire(timeout=self._timeout)
                return self
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "filelock acquisition failed (%s); falling back to "
                    "native file locking",
                    exc,
                )
                self._impl = None

        self._fh = open(self._lock_path, "a+")  # noqa: SIM115
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore[import-not-found]

                deadline = self._timeout
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
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
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl  # type: ignore[import-not-found]

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def _read_departments_doc() -> dict[str, Any]:
    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "departments": []}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "departments_config_corrupt",
                "message": f"departments.json is not valid JSON: {exc}",
            },
        ) from exc
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=500,
            detail={
                "error": "departments_config_corrupt",
                "message": "departments.json root must be an object",
            },
        )
    data.setdefault("version", 1)
    data.setdefault("departments", [])
    return data


def _atomic_write_json(path: Path, doc: dict[str, Any]) -> None:
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
                pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Legacy /admin/feature-flags surface (mimari-ops task 11.6)
# ---------------------------------------------------------------------------


@router.get("", dependencies=[Depends(require_admin)])
async def list_flags(request: Request) -> dict:
    """Legacy listing — global flag rows only."""

    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, enabled, description, impact_note,
                   default_value, updated_by, updated_at
              FROM shared.feature_flags
             ORDER BY name
            """,
        )
    return {
        "flags": [
            {
                "name": r["name"],
                "enabled": bool(r["enabled"]),
                "description": r["description"] or "",
                "impact_note": r["impact_note"] or "",
                "default_value": bool(r["default_value"]),
                "updated_by": r["updated_by"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


@router.put("/{name}", dependencies=[Depends(require_admin)])
async def toggle_flag(
    name: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: dict = Body(...),
) -> dict:
    """Legacy toggle — flips the global ``enabled`` value only."""

    if "enabled" not in body or not isinstance(body["enabled"], bool):
        raise HTTPException(
            status_code=400,
            detail="body must contain `enabled: bool`",
        )
    new_value = bool(body["enabled"])
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT enabled FROM shared.feature_flags WHERE name = $1",
                name,
            )
            if old_row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"feature flag {name!r} not found",
                )
            await conn.execute(
                """
                UPDATE shared.feature_flags
                   SET enabled = $1,
                       updated_by = $2,
                       updated_at = now()
                 WHERE name = $3
                """,
                new_value,
                actor.sub,
                name,
            )

    await _emit_audit(
        request,
        actor=actor,
        key=name,
        scope="global",
        dept_id=None,
        old_value=bool(old_row["enabled"]),
        new_value=new_value,
    )
    await _signal_global_reload(request, key=name)
    return {"name": name, "enabled": new_value}


# ---------------------------------------------------------------------------
# Gap-fill /api/v1/feature-flags surface (task 16.1)
# ---------------------------------------------------------------------------


class FlagPatch(BaseModel):
    """Body for ``PATCH /api/v1/feature-flags/{key}``.

    * ``value`` — required; new boolean state (global or per-dept).
    * ``dept_id`` — optional; when present the override map for that
      dept is patched, otherwise the global row is updated.
    """

    value: bool = Field(..., description="New boolean value.")
    dept_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=64,
        description=(
            "When supplied, set the override on this department. "
            "Otherwise the global row is updated."
        ),
    )


def _collect_dept_overrides(
    departments: list[dict[str, Any]],
) -> dict[str, dict[str, bool]]:
    """Pivot ``feature_flag_overrides`` from per-dept to per-flag.

    Returns ``{flag_name: {dept_id: bool}}`` — the shape the FE
    consumes when rendering the dept-override column.
    """

    by_flag: dict[str, dict[str, bool]] = {}
    for dept in departments:
        dept_id = dept.get("id")
        if not isinstance(dept_id, str):
            continue
        overrides = dept.get("feature_flag_overrides") or {}
        if not isinstance(overrides, dict):
            continue
        for flag_name, value in overrides.items():
            if not isinstance(flag_name, str) or not isinstance(value, bool):
                continue
            by_flag.setdefault(flag_name, {})[dept_id] = value
    return by_flag


@v1_router.get("", dependencies=[Depends(require_admin)])
async def list_flags_with_overrides(request: Request) -> dict:
    """List flags with their global value **and** per-dept overrides.

    **Validates: Requirement R16.1**

    Response shape::

        {
            "flags": [
                {
                    "key": <flag name>,
                    "global_value": <bool>,
                    "default_value": <bool>,
                    "description": <str>,
                    "impact_note": <str>,
                    "updated_by": <str | null>,
                    "updated_at": <iso-8601 | null>,
                    "dept_overrides": { <dept_id>: <bool>, ... }
                },
                ...
            ]
        }
    """

    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, enabled, description, impact_note,
                   default_value, updated_by, updated_at
              FROM shared.feature_flags
             ORDER BY name
            """,
        )

    doc = _read_departments_doc()
    overrides_by_flag = _collect_dept_overrides(doc.get("departments", []))

    return {
        "flags": [
            {
                "key": r["name"],
                "global_value": bool(r["enabled"]),
                "default_value": bool(r["default_value"]),
                "description": r["description"] or "",
                "impact_note": r["impact_note"] or "",
                "updated_by": r["updated_by"],
                "updated_at": (
                    r["updated_at"].isoformat() if r["updated_at"] else None
                ),
                "dept_overrides": overrides_by_flag.get(r["name"], {}),
            }
            for r in rows
        ]
    }


async def _apply_global_patch(
    request: Request,
    *,
    actor: AuthClaims,
    key: str,
    new_value: bool,
) -> dict[str, Any]:
    """Update ``shared.feature_flags.enabled`` and audit the change."""

    pool = _get_pool(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            old_row = await conn.fetchrow(
                "SELECT enabled FROM shared.feature_flags WHERE name = $1",
                key,
            )
            if old_row is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "feature_flag_not_found",
                        "key": key,
                    },
                )
            await conn.execute(
                """
                UPDATE shared.feature_flags
                   SET enabled = $1,
                       updated_by = $2,
                       updated_at = now()
                 WHERE name = $3
                """,
                new_value,
                actor.sub,
                key,
            )
    old_value = bool(old_row["enabled"])
    await _emit_audit(
        request,
        actor=actor,
        key=key,
        scope="global",
        dept_id=None,
        old_value=old_value,
        new_value=new_value,
    )
    await _signal_global_reload(request, key=key)
    return {
        "key": key,
        "scope": "global",
        "dept_id": None,
        "old_value": old_value,
        "new_value": new_value,
    }


def _flag_exists(pool_rows: list[dict[str, Any]] | list[Any], key: str) -> bool:
    for row in pool_rows:
        # asyncpg.Record + dict are both indexable by string.
        try:
            if row["name"] == key:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _check_flag_exists(request: Request, key: str) -> None:
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM shared.feature_flags WHERE name = $1",
            key,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "feature_flag_not_found", "key": key},
        )


async def _apply_dept_patch(
    request: Request,
    *,
    actor: AuthClaims,
    key: str,
    dept_id: str,
    new_value: bool,
) -> dict[str, Any]:
    """Set or update ``departments[i].feature_flag_overrides[key]``."""

    await _check_flag_exists(request, key)

    with _FileLockContext(_DEPARTMENTS_LOCK_PATH):
        doc = _read_departments_doc()
        departments = doc.get("departments", [])
        target_idx: int | None = None
        for idx, d in enumerate(departments):
            if d.get("id") == dept_id:
                target_idx = idx
                break
        if target_idx is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "department_not_found",
                    "dept_id": dept_id,
                },
            )
        target = departments[target_idx]
        overrides = target.get("feature_flag_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        old_value: bool | None
        if key in overrides and isinstance(overrides[key], bool):
            old_value = bool(overrides[key])
        else:
            old_value = None
        overrides[key] = bool(new_value)
        target["feature_flag_overrides"] = overrides
        departments[target_idx] = target
        doc["departments"] = departments
        _atomic_write_json(_DEPARTMENTS_CONFIG_PATH, doc)

    await _emit_audit(
        request,
        actor=actor,
        key=key,
        scope="dept",
        dept_id=dept_id,
        old_value=old_value,
        new_value=new_value,
    )
    await _signal_dept_reload(request, dept_id=dept_id, key=key)
    return {
        "key": key,
        "scope": "dept",
        "dept_id": dept_id,
        "old_value": old_value,
        "new_value": new_value,
    }


@v1_router.patch("/{key}", dependencies=[Depends(require_admin)])
async def patch_flag(
    key: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: FlagPatch = Body(...),
) -> dict[str, Any]:
    """Update a flag's global value or set a per-dept override.

    **Validates: Requirements R16.1, R16.3, R16.4, R16.5**

    Body::

        { "value": <bool>, "dept_id"?: <str> }

    When ``dept_id`` is omitted the global row is updated. When
    supplied the override map for that dept is patched and the
    departments hot-reload channel is poked so the change reaches
    automation-service within the 10 s SLA mandated by R16.5.
    """

    if body.dept_id is None:
        return await _apply_global_patch(
            request, actor=actor, key=key, new_value=body.value
        )
    return await _apply_dept_patch(
        request,
        actor=actor,
        key=key,
        dept_id=body.dept_id,
        new_value=body.value,
    )


@v1_router.delete(
    "/{key}/overrides/{dept_id}",
    dependencies=[Depends(require_admin)],
)
async def delete_dept_override(
    key: str,
    dept_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Remove a department's override for ``key``.

    **Validates: Requirements R16.1, R16.3, R16.4, R16.5**

    The dept reverts to the global value on the next reload. The
    audit row records ``new_value=null`` so consumers can distinguish
    a "set to false" from a "remove override" event.
    """

    await _check_flag_exists(request, key)

    with _FileLockContext(_DEPARTMENTS_LOCK_PATH):
        doc = _read_departments_doc()
        departments = doc.get("departments", [])
        target_idx: int | None = None
        for idx, d in enumerate(departments):
            if d.get("id") == dept_id:
                target_idx = idx
                break
        if target_idx is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "department_not_found",
                    "dept_id": dept_id,
                },
            )
        target = departments[target_idx]
        overrides = target.get("feature_flag_overrides")
        if not isinstance(overrides, dict) or key not in overrides:
            return {
                "key": key,
                "scope": "dept",
                "dept_id": dept_id,
                "status": "not_present",
            }
        old_value: bool | None = None
        if isinstance(overrides.get(key), bool):
            old_value = bool(overrides[key])
        del overrides[key]
        target["feature_flag_overrides"] = overrides
        departments[target_idx] = target
        doc["departments"] = departments
        _atomic_write_json(_DEPARTMENTS_CONFIG_PATH, doc)

    await _emit_audit(
        request,
        actor=actor,
        key=key,
        scope="dept",
        dept_id=dept_id,
        old_value=old_value,
        new_value=None,
    )
    await _signal_dept_reload(request, dept_id=dept_id, key=key)
    return {
        "key": key,
        "scope": "dept",
        "dept_id": dept_id,
        "status": "removed",
        "old_value": old_value,
    }
