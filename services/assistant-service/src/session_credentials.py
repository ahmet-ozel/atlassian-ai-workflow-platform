"""Per-user session credential relay endpoint.

The Streamlit UI (``platform/ui/streamlit-app/pages/0_credentials.py``)
posts plain-text service credentials to this router so they land in
Vault under
``vault:atlassian/_user_session/<session_id>/<service>``. The router
is the **only** place plain-text user-supplied tokens land on the
server - they are forwarded to Vault and the in-memory bytearray is
zeroed before the response is returned.

Gmail/Outlook are accepted for route compatibility. They use a mail
OAuth payload instead of the Atlassian API-token shape. For shared
deployments the per-user credential must contain either a short-lived
access token or the full refresh flow material (refresh token plus
provider client id/secret); mail user tokens are not sourced from
service-local .env files by default. The mail MCP services own token
refresh and read the stored material by credential ref.

Lifecycle:

* ``POST /session/credentials``  write or refresh.
* ``DELETE /session/credentials?session_id=...&service=...``
  remove the path so ``CredentialResolver.resolve`` falls back to
  the org-default.
* ``GET /session/credentials/{session_id}/{service}`` is **not**
  exposed over HTTP - the only consumer is
  :class:`automation_service.credentials.CredentialResolver` which
  reaches Vault directly via :class:`vault_client.VaultClient`.

The router does not authenticate the caller - Streamlit's session
context is the gate (the Streamlit page is only reachable from the
authenticated UI). In production the endpoint sits behind the
admin-dashboard-api proxy whose OIDC layer enforces the user's
identity claim.

The Vault path layout shares the canonical helper
:func:`automation_service.credentials.build_user_session_path` so
the writer (this module) and reader
(:class:`CredentialResolver.resolve`) cannot drift on the path
format.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

# ``automation_service.credentials.build_user_session_path`` is the
# canonical path-builder. We import it lazily to keep the
# assistant-service free of an import-time dependency on the
# automation-service package - production wiring exposes the
# function from a shared lib in a future spec.

__all__ = ["SessionCredentialDeps", "build_user_session_path", "router"]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path builder (mirror of automation_service.credentials)
# ---------------------------------------------------------------------------


def build_user_session_path(session_id: str, service: str) -> str:
    """Return the Vault path for ``(session_id, service)``.

    Mirrors :func:`automation_service.credentials.build_user_session_path`
    byte-for-byte. The two helpers are kept in lockstep by the
    path parity test which
    drives both the writer (this module's POST handler) and the
    reader (``CredentialResolver``) through the same path.
    """

    return f"vault:atlassian/_user_session/{session_id}/{service}"


# ---------------------------------------------------------------------------
# Vault client protocol - duck-typed
# ---------------------------------------------------------------------------


class VaultClient(Protocol):
    """Minimal write/delete surface needed by the relay."""

    def write(self, path: Any, data: Mapping[str, str]) -> None: ...

    def delete(self, path: Any) -> None: ...


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionCredentialDeps:
    """Collaborators the router pulls from ``app.state.session_creds``.

    Production wiring builds this on service startup; tests inject
    a fake :class:`VaultClient` to assert the round-trip.
    """

    vault: VaultClient


def _deps(request: Request) -> SessionCredentialDeps:
    deps = getattr(request.app.state, "session_creds", None)
    if not isinstance(deps, SessionCredentialDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="session credentials router not wired (app.state.session_creds missing)",
        )
    return deps


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/session", tags=["session-credentials"])


_VALID_SERVICES = {"jira", "bitbucket", "confluence", "gmail", "outlook"}


def _to_vault_path(raw: str) -> Any:
    """Lazily build a :class:`vault_client.VaultPath` if the lib is present.

    The relay is happy to call ``vault.write(path_str, data)`` when
    the backend accepts strings - keeps the assistant-service free
    of a hard import-time dependency on ``vault_client``. When the
    lib is available we wrap the string in :class:`VaultPath` for
    the structural validation + canonical regex enforcement.
    """

    try:
        from vault_client import VaultPath  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - optional local-dev deps may be absent
        return raw
    return VaultPath.parse(raw)


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
async def post_session_credential(
    request: Request,
) -> JSONResponse:
    """Write a per-user credential to Vault.

    Body shape:

    .. code-block:: json

        {
            "session_id": "<opaque-string>",
            "service": "jira" | "bitbucket" | "confluence",
            "url": "https://acme.atlassian.net",
            "username": "user@example.com",
            "personal_token": "<plain-text-token>"
        }

    Mail services use:

    .. code-block:: json

        {
            "session_id": "<opaque-string>",
            "service": "gmail" | "outlook",
            "email": "user@example.com",
            "refresh_token": "<oauth-refresh-token>",
            "access_token": "<optional-short-lived-token>",
            "client_id": "<provider-client-id-for-refresh>",
            "client_secret": "<provider-client-secret-for-refresh>",
            "scopes": "<optional-scope-string>"
        }

    Returns ``{"vault_path": "vault:atlassian/_user_session/.../jira"}``
    on success.
    """

    deps = _deps(request)
    body = await request.json()

    session_id = body.get("session_id")
    service = body.get("service")
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id is required",
        )
    if service not in _VALID_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"service must be one of {sorted(_VALID_SERVICES)}",
        )

    raw_path = build_user_session_path(session_id, service)
    path = _to_vault_path(raw_path)

    if service in {"gmail", "outlook"}:
        payload, token_to_scrub = _mail_oauth_payload(service, body)
    else:
        payload, token_to_scrub = _atlassian_payload(body)

    # Hold token in a bytearray so we can scrub our local copy.
    token_buf = bytearray(token_to_scrub.encode("utf-8"))
    try:
        try:
            deps.vault.write(path, payload)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "session_creds.write_failed session=%s service=%s err=%s",
                session_id, service, type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"vault write failed: {type(exc).__name__}",
            )
    finally:
        for i in range(len(token_buf)):
            token_buf[i] = 0
        del token_buf
        del token_to_scrub

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"vault_path": raw_path, "service": service},
    )


def _atlassian_payload(body: Mapping[str, Any]) -> tuple[dict[str, str], str]:
    url = body.get("url")
    username = body.get("username")
    personal_token = body.get("personal_token")
    if not all(isinstance(v, str) and v for v in (url, username, personal_token)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="url, username and personal_token are required",
        )
    return (
        {
            "url": url,
            "username": username,
            "personal_token": personal_token,
        },
        personal_token,
    )


def _mail_oauth_payload(
    service: str,
    body: Mapping[str, Any],
) -> tuple[dict[str, str], str]:
    refresh_token = body.get("refresh_token") or body.get("personal_token")
    access_token = body.get("access_token")
    email = body.get("email") or body.get("username")
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")
    scopes = body.get("scopes") or body.get("scope")

    if email is not None and not isinstance(email, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email must be a string when provided",
        )

    has_access_token = isinstance(access_token, str) and bool(access_token)
    has_refresh_flow = all(
        isinstance(value, str) and bool(value)
        for value in (refresh_token, client_id, client_secret)
    )
    if not has_access_token and not has_refresh_flow:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "gmail/outlook credentials require refresh_token with "
                "client_id/client_secret, or access_token"
            ),
        )

    payload: dict[str, str] = {"provider": service}
    if isinstance(refresh_token, str) and refresh_token:
        payload["refresh_token"] = refresh_token
    if has_access_token:
        payload["access_token"] = access_token
    if isinstance(email, str) and email:
        payload["email"] = email
        payload["username"] = email
    if isinstance(client_id, str) and client_id:
        payload["client_id"] = client_id
    if isinstance(client_secret, str) and client_secret:
        payload["client_secret"] = client_secret
    if isinstance(scopes, str) and scopes:
        payload["scopes"] = scopes
    scrub_token = (
        refresh_token
        if isinstance(refresh_token, str) and refresh_token
        else access_token
    )
    return payload, str(scrub_token)


@router.delete("/credentials")
async def delete_session_credential(
    request: Request,
    session_id: str = Query(..., min_length=1),
    service: str = Query(...),
) -> JSONResponse:
    """Remove the per-user credential."""

    if service not in _VALID_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"service must be one of {sorted(_VALID_SERVICES)}",
        )

    deps = _deps(request)
    raw_path = build_user_session_path(session_id, service)
    path = _to_vault_path(raw_path)

    try:
        deps.vault.delete(path)
    except KeyError:
        # Idempotent delete - already gone.
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "absent", "vault_path": raw_path},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"vault delete failed: {type(exc).__name__}",
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "deleted", "vault_path": raw_path},
    )
