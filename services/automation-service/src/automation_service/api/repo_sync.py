"""``POST /admin/departments/{id}/repo-mappings/sync`` endpoint.

* Dry-run mode (no ``?apply=true``) - scan the dept's Bitbucket
  workspace through ``mcp_client``, fold the result against the
  dept's current ``repo_mappings`` array via the pure set-algebra
  helper :func:`temporal_shared.repo_sync.compute_repo_mapping_diff`,
  and return the three-way diff as JSON. **No mutations**.
* Apply mode (``?apply=true``) - same scan + diff, then atomically
  persist the new mapping list via the injected
  :class:`SupportsDepartmentsRepo` (``departments_repo
  .update_repo_mappings``) and emit one
  ``repo_mapping_synced`` audit row carrying the diff in the payload.
* Authorization - every request is gated by
  :func:`auth_shared.requires("admin")`. A non-admin actor (or one
  whose token is missing / malformed) receives HTTP 403 with an
  ``rbac_denied`` audit row.

The endpoint is deliberately **thin**: every collaborator (OIDC
validator, MCP-side Bitbucket scanner, departments registry, audit
logger, clock) is read from :class:`RepoSyncEndpointDeps` parked on
``request.app.state.repo_sync``. Tests inject a stub container so the
router can be exercised end-to-end without a live Bitbucket workspace
or Postgres connection.

Implementation notes
--------------------

* The corresponding pure helper is re-exported by
  :mod:`temporal_shared.repo_sync`.
* ``actor_role`` is required on every audit row; the writer rejects
  empty / unknown values before any DB round-trip, so the helper here
  always forwards the resolved role.
* :func:`requires` is the pure-Python guard composed on top of an
  explicit OIDC validator dependency. Failures raise
  :class:`auth_shared.PermissionDenied`; we translate that into an
  HTTP 403 + ``rbac_denied`` audit row.

Pure pieces of the decision are exposed via
:mod:`temporal_shared.repo_sync` (``RepoMapping``, ``RepoMappingDiff``,
``compute_repo_mapping_diff``) so this module owns nothing beyond the
HTTP shim and the audit / persistence wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from auth_shared import (
    AuthContext,
    InvalidTokenError,
    OIDCValidator,
    PermissionDenied,
    check as auth_check,
)
from temporal_shared import (
    RepoMapping,
    RepoMappingDiff,
    compute_repo_mapping_diff,
)

__all__ = [
    "BitbucketRepoScanner",
    "RepoSyncEndpointDeps",
    "SupportsDepartmentsRepo",
    "router",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit action / resource constants - single source of truth
# ---------------------------------------------------------------------------

#: Audit ``action`` token written on every successful sync invocation
#: (both dry-run and apply modes).
_AUDIT_ACTION_SYNCED: str = "repo_mapping_synced"

#: Audit ``action`` token written when an actor without the ``admin``
#: role hits the endpoint. Mirrors the foundation-wide convention used
#: by :mod:`automation_service.api.cancel`.
_AUDIT_ACTION_RBAC_DENIED: str = "rbac_denied"

#: Audit ``action`` token written when the Bitbucket workspace scan
#: fails (MCP returned an error or the dept has no workspace
#: configured). Surfaces the failure to the operator without
#: short-circuiting the ``ok``/``denied`` rollup.
_AUDIT_ACTION_SCAN_FAILED: str = "repo_mapping_scan_failed"


# ---------------------------------------------------------------------------
# Collaborator protocols - keep the router trivially mockable
# ---------------------------------------------------------------------------


@runtime_checkable
class BitbucketRepoScanner(Protocol):
    """Structural type for the MCP-side Bitbucket workspace scanner.

    Production wiring binds this to ``mcp_client.atlassian_client
    .bitbucket_list_repos`` (or the equivalent helper) which talks to
    the ``atlassian_mcp_bitbucket`` MCP service. The Protocol is declared
    here rather than imported so:

    * the endpoint is exercisable in unit tests without a live MCP
      service, and
    * the contract documents exactly what the router needs (a
      coroutine returning a sequence of ``{"name", "slug"}`` mappings)
      independently of any future evolution of the broader
      ``AtlassianClient`` surface.

    The callable receives the dept_id (so the scanner can resolve the
    correct credential / workspace from the registry) and returns the
    raw repo descriptors. The router folds those descriptors into a
    :class:`frozenset` of slugs before passing them to the pure
    helper.
    """

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class SupportsDepartmentsRepo(Protocol):
    """Structural type for the departments registry adapter.

    Production wiring binds this to a thin object exposing two
    coroutines around the ``departments.json`` document (or its
    Postgres mirror): :meth:`list_repo_mappings` reads the dept's
    current ``repo_mappings`` array and :meth:`update_repo_mappings`
    atomically replaces it.

    Returning a tuple of :class:`RepoMapping` dataclasses (rather than
    a list of dicts) keeps the boundary between "wire-shape" and
    "domain-shape" explicit: the router never sees raw JSON and the
    pure diff helper never sees an HTTP / DB type.

    The endpoint never calls :meth:`update_repo_mappings` outside of
    apply mode, so dry-run requests always leave the registry
    untouched (R10.7 - "dry-run does not mutate").
    """

    async def list_repo_mappings(
        self, dept_id: str
    ) -> tuple[RepoMapping, ...]: ...

    async def update_repo_mappings(
        self, dept_id: str, new_mappings: tuple[RepoMapping, ...]
    ) -> None: ...


# ---------------------------------------------------------------------------
# Dependency container - injected via ``request.app.state.repo_sync``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoSyncEndpointDeps:
    """Collaborators the repo-sync router pulls from ``app.state``.

    The router owns no state of its own. Production wiring builds one
    of these in :func:`automation_service.app.create_app`; tests
    construct the dataclass directly with hand-built fakes.

    Attributes
    ----------
    oidc_validator:
        :class:`auth_shared.OIDCValidator` authenticating the bearer
        token. Production wiring uses ``OIDCValidator(OIDCConfig
        .from_env())``; dev / test wiring may pass a dev-mode
        validator (``auth_mode="dev"``) so any non-empty token returns
        the canned admin claims.
    bitbucket_scanner:
        Coroutine returning the dept's Bitbucket repo descriptors as
        a sequence of ``{"name", "slug"}`` mappings. Bound to
        ``mcp_client.atlassian_client.bitbucket_list_repos`` in
        production.
    departments_repo:
        Departments registry adapter exposing
        :meth:`list_repo_mappings` (read) and
        :meth:`update_repo_mappings` (atomic write). Apply mode is
        the only path that calls the writer.
    audit_logger:
        Audit sink for ``repo_mapping_synced`` and ``rbac_denied``
        events carrying the diff payload.
    clock:
        Optional callable returning the current UTC datetime. When
        omitted, the router uses ``datetime.now(timezone.utc)``.
        Tests inject a frozen clock so audit timestamps are
        deterministic.
    """

    oidc_validator: OIDCValidator
    bitbucket_scanner: BitbucketRepoScanner
    departments_repo: SupportsDepartmentsRepo
    audit_logger: AuditLogger
    clock: Callable[[], datetime] | None = None


def _deps(request: Request) -> RepoSyncEndpointDeps:
    """Pull the :class:`RepoSyncEndpointDeps` off ``app.state``.

    Surfaces a deployment misconfiguration (router mounted but
    collaborators not wired) as a clear 500 instead of a downstream
    :class:`AttributeError`.
    """

    deps = getattr(request.app.state, "repo_sync", None)
    if not isinstance(deps, RepoSyncEndpointDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="repo_sync router is not wired (app.state.repo_sync missing)",
        )
    return deps


def _now(deps: RepoSyncEndpointDeps) -> datetime:
    """Return the current UTC timestamp using the injected clock."""

    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# AuthN helpers - bearer token extraction + OIDC validation
# ---------------------------------------------------------------------------


def _extract_bearer_token(authorization: str | None) -> str:
    """Return the bearer token from an ``Authorization`` header.

    Raises :class:`HTTPException` ``401`` for missing or malformed
    headers. The error detail is intentionally generic so the
    response body never leaks whether the token format vs the token
    contents was the problem.
    """

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed Authorization header",
        )
    return parts[1].strip()


def _resolve_actor_user_id(claims: Mapping[str, Any]) -> str | None:
    """Pick the ``actor_user_id`` from a decoded claim dict.

    Prefers ``account_id`` (the Atlassian-specific claim sometimes
    minted by an SSO bridge), falling back to the canonical OIDC
    ``sub``. Returns ``None`` when neither is present so the caller
    can map the case to HTTP 401.
    """

    for key in ("account_id", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_auth_context(claims: Mapping[str, Any]) -> AuthContext | None:
    """Construct an :class:`AuthContext` from decoded OIDC claims.

    Returns ``None`` when the claims are missing the ``account_id``
    / ``sub`` pair the foundation guard relies on. Callers map
    that case to HTTP 401 (token missing the required claim) rather
    than leaking it as a permission failure.

    The role is read from the canonical ``role`` claim; missing or
    non-string roles fall through to ``"viewer"`` (the lowest
    privilege) so the foundation guard rejects the request - this
    matches the wider service convention of preferring "deny on
    ambiguous" for admin endpoints.
    """

    actor_id = _resolve_actor_user_id(claims)
    if actor_id is None:
        return None

    raw_role = claims.get("role")
    actor_role = (
        raw_role
        if isinstance(raw_role, str) and raw_role in {"viewer", "lead", "admin", "dept_admin"}
        else "viewer"
    )

    raw_dept_ids = claims.get("dept_ids") or ()
    dept_ids: frozenset[str]
    if isinstance(raw_dept_ids, (list, tuple, frozenset, set)):
        dept_ids = frozenset(
            str(d) for d in raw_dept_ids if isinstance(d, str) and d
        )
    else:
        dept_ids = frozenset()

    return AuthContext(
        actor_id=actor_id,
        actor_role=actor_role,  # type: ignore[arg-type]
        dept_ids=dept_ids,
    )


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    *,
    actor_id: str,
    actor_role: str,
    dept_id: str | None,
    action: str,
    resource: str,
    result: str,
    timestamp: datetime,
    payload: dict[str, Any] | None,
) -> AuditEvent:
    """Construct an :class:`AuditEvent` with a safe ``actor_role``.

    The cancel endpoint runs **after** OIDC authentication so the
    actor role is normally one of the four human roles ("viewer",
    "lead", "admin", "dept_admin"). For the rare case of an unknown
    role slipping through (eg. a malformed JWT claim) we map to
    ``"system"`` and stash the original role on
    ``payload["claimed_role"]`` so audit forensics can still see
    what was offered.
    """

    safe_role = (
        actor_role
        if actor_role in ("viewer", "lead", "admin", "dept_admin", "system")
        else "system"
    )
    enriched_payload: dict[str, Any] | None = payload
    if safe_role != actor_role:
        enriched_payload = dict(payload or {})
        enriched_payload["claimed_role"] = actor_role
    return AuditEvent(
        actor_id=actor_id,
        actor_role=safe_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=timestamp,
        payload=enriched_payload,
    )


async def _emit_audit(audit_logger: AuditLogger, event: AuditEvent) -> None:
    """Best-effort audit write - never let an audit error 500 the call.

    Mirrors the pattern used by :mod:`automation_service.api.cancel`:
    failures are warning-logged locally so the operator can
    investigate but the user-visible response is unaffected.
    """

    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        _LOG.warning(
            "repo_sync.audit_write_failed action=%s resource=%s err=%s",
            event.action,
            event.resource,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Scan helpers - fold the MCP response into a frozenset of slugs
# ---------------------------------------------------------------------------


def _scanned_slugs(repos: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Project the MCP response into the canonical slug set.

    The MCP returns a sequence of repo descriptors (``{"name",
    "slug", ...}``). The diff helper only needs slugs, so we extract
    them here and silently skip entries that are missing a
    non-empty ``slug`` field - those would be malformed in the MCP
    response and are not actionable for the operator.

    Any extra fields on the MCP response are ignored so the helper
    stays compatible with future schema additions.
    """

    slugs: set[str] = set()
    for entry in repos:
        slug = entry.get("slug") if isinstance(entry, Mapping) else None
        if isinstance(slug, str) and slug:
            slugs.add(slug)
    return frozenset(slugs)


def _build_new_mapping_list(
    scanned_repos: Sequence[Mapping[str, Any]],
    current_mappings: tuple[RepoMapping, ...],
    diff: RepoMappingDiff,
) -> tuple[RepoMapping, ...]:
    """Compose the new ``repo_mappings`` array from scan + diff.

    For ``apply=true`` mode we replace the dept's mapping list with
    the union of:

    * existing mappings whose slug is **not** in ``diff.removed``
      (so we preserve the human-readable ``name`` operators may
      have customised), and
    * fresh mappings for every slug in ``diff.added`` (using the
      MCP-supplied ``name`` when available; otherwise echoing the
      slug as a placeholder).

    The order is deterministic - preserved current entries first
    (in their original order), added entries next (sorted by slug)
    - so two consecutive apply runs over the same input produce
    identical mapping lists, matching the idempotence invariant
    the diff helper itself guarantees.
    """

    # Start with the surviving entries from the current list (those
    # whose slug is *not* being removed). Preserving the operator's
    # ``name`` field for these is important - they may have edited
    # the human-readable name in ``departments.json`` and we should
    # not silently overwrite it with the MCP's value.
    surviving: list[RepoMapping] = [
        m for m in current_mappings if m.slug not in diff.removed
    ]

    # Build a lookup from slug  name based on the MCP scan so we can
    # populate the human-readable name for added entries. Falling
    # back to the slug when the MCP omits a name keeps the resulting
    # ``departments.json`` document valid even if the upstream is
    # missing data.
    scan_name_by_slug: dict[str, str] = {}
    for entry in scanned_repos:
        if not isinstance(entry, Mapping):
            continue
        slug = entry.get("slug")
        name = entry.get("name")
        if isinstance(slug, str) and slug:
            scan_name_by_slug[slug] = (
                name if isinstance(name, str) and name else slug
            )

    # Append the added entries in slug-sorted order so the apply
    # output is deterministic and a future run that would re-add the
    # same slugs produces the same tuple ordering.
    added_entries: list[RepoMapping] = [
        RepoMapping(
            name=scan_name_by_slug.get(slug, slug),
            slug=slug,
        )
        for slug in sorted(diff.added)
    ]

    return tuple(surviving) + tuple(added_entries)


def _diff_payload(diff: RepoMappingDiff) -> dict[str, list[str]]:
    """Render a :class:`RepoMappingDiff` as a JSON-friendly mapping.

    Sorting the slugs keeps the response stable across runs; the
    audit event payload re-uses the same shape so the dry-run JSON
    body and the audit row diff are byte-identical.
    """

    return {
        "added": sorted(diff.added),
        "removed": sorted(diff.removed),
        "unchanged": sorted(diff.unchanged),
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/admin", tags=["admin", "repo-sync"])


@router.post(
    "/departments/{dept_id}/repo-mappings/sync",
    status_code=status.HTTP_200_OK,
)
async def sync_repo_mappings(
    dept_id: str,
    request: Request,
    apply: bool = Query(default=False),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Scan Bitbucket and diff vs current dept ``repo_mappings``.

    The endpoint runs in two modes selected by the ``apply`` query
    parameter:

    * ``apply=False`` (default - dry-run):
        1. Validates the OIDC bearer token via the injected
           :class:`OIDCValidator`. Missing / malformed / invalid
           tokens receive HTTP 401.
        2. Enforces the ``admin`` role via
           :func:`auth_shared.check`. Failures emit a single
           ``rbac_denied`` audit row and respond HTTP 403.
        3. Scans the dept's Bitbucket workspace through the injected
           :class:`BitbucketRepoScanner` and folds the result into a
           :class:`frozenset` of slugs.
        4. Reads the dept's current ``repo_mappings`` array via
           :class:`SupportsDepartmentsRepo.list_repo_mappings`.
        5. Computes the three-way diff via
           :func:`temporal_shared.repo_sync.compute_repo_mapping_diff`.
        6. Returns the diff as JSON. **No mutations**.

    * ``apply=True``:
        Steps 1-5 are identical, then:
        6. Composes the new ``repo_mappings`` array (preserved
           survivors + sorted-slug additions).
        7. Calls
           :class:`SupportsDepartmentsRepo.update_repo_mappings` to
           atomically replace the dept's mapping list.
        8. Emits one ``repo_mapping_synced`` audit row carrying the
           diff in the payload.
        9. Returns the diff JSON plus an ``"applied": true`` marker
           so the caller can confirm the persistence happened.

    Audit invariants
    ----------------

    * Every successful invocation (dry-run or apply) emits exactly
      one ``repo_mapping_synced`` audit row. Dry-run uses
      ``result="ok"`` with ``payload["mode"] = "dry_run"``; apply
      mode uses ``result="ok"`` with ``payload["mode"] = "apply"``.
    * Authorization failures emit exactly one ``rbac_denied`` audit
      row and return HTTP 403.
    * Bitbucket scan failures emit exactly one
      ``repo_mapping_scan_failed`` audit row and return HTTP 502 so
      the operator can distinguish "no permission" from "Bitbucket
      flaked".
    """

    deps = _deps(request)

    # ---------- 1. AuthN -----------------------------------------------------
    token = _extract_bearer_token(authorization)
    try:
        claims = deps.oidc_validator.validate(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
        ) from exc

    actor_ctx = _build_auth_context(claims)
    if actor_ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing account_id / sub claim",
        )
    actor_id = actor_ctx.actor_id
    actor_role = actor_ctx.actor_role

    # ---------- 2. AuthZ - admin role required -------------------------------
    try:
        auth_check(actor_ctx, "admin")
    except PermissionDenied:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=dept_id,
                action=_AUDIT_ACTION_RBAC_DENIED,
                resource=f"repo_mappings:{dept_id}",
                result="denied",
                timestamp=_now(deps),
                payload={
                    "endpoint": "POST /admin/departments/{dept_id}/repo-mappings/sync",
                    "required_role": "admin",
                    "apply": apply,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="repo-mappings sync requires admin role",
        )

    # ---------- 3. Bitbucket scan -------------------------------------------
    try:
        scanned_response: Sequence[Mapping[str, Any]] = (
            await deps.bitbucket_scanner(dept_id)
        )
    except Exception as exc:  # noqa: BLE001 - translate to 502
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=dept_id,
                action=_AUDIT_ACTION_SCAN_FAILED,
                resource=f"repo_mappings:{dept_id}",
                result="error",
                timestamp=_now(deps),
                payload={
                    "endpoint": "POST /admin/departments/{dept_id}/repo-mappings/sync",
                    "reason": type(exc).__name__,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"bitbucket scan failed: {type(exc).__name__}",
        ) from exc

    scanned_slugs = _scanned_slugs(scanned_response)

    # ---------- 4. Read current mappings ------------------------------------
    try:
        current_mappings: tuple[RepoMapping, ...] = (
            await deps.departments_repo.list_repo_mappings(dept_id)
        )
    except Exception as exc:  # noqa: BLE001 - translate to 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"departments registry read failed: {type(exc).__name__}",
        ) from exc

    # ---------- 5. Compute diff (pure helper) -------------------------------
    diff = compute_repo_mapping_diff(
        scanned_repos=scanned_slugs,
        current_mappings=current_mappings,
    )
    diff_payload = _diff_payload(diff)

    # ---------- 6. Apply mode (optional) ------------------------------------
    applied = False
    if apply:
        new_mappings = _build_new_mapping_list(
            scanned_response, current_mappings, diff
        )
        try:
            await deps.departments_repo.update_repo_mappings(
                dept_id, new_mappings
            )
        except Exception as exc:  # noqa: BLE001 - translate to 502
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"departments registry write failed: {type(exc).__name__}",
            ) from exc
        applied = True

    # ---------- 7. Audit (synced) -------------------------------------------
    audit_payload: dict[str, Any] = {
        "endpoint": "POST /admin/departments/{dept_id}/repo-mappings/sync",
        "mode": "apply" if applied else "dry_run",
        "diff": diff_payload,
        "scanned_count": len(scanned_slugs),
        "current_count": len(current_mappings),
    }
    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            actor_id=actor_id,
            actor_role=actor_role,
            dept_id=dept_id,
            action=_AUDIT_ACTION_SYNCED,
            resource=f"repo_mappings:{dept_id}",
            result="ok",
            timestamp=_now(deps),
            payload=audit_payload,
        ),
    )

    response_body: dict[str, Any] = {
        "added": diff_payload["added"],
        "removed": diff_payload["removed"],
        "unchanged": diff_payload["unchanged"],
        "applied": applied,
    }
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response_body,
    )
