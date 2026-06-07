"""Shared Vault staging helpers for atomic department create flows.

This module owns the small set of pure helpers used by both the
"single-shot" atomic create endpoint (task 5.3 - ``POST
/admin/departments``) and the multi-step setup wizard (task 5.4 -
``POST /admin/departments/wizard``):

* :data:`VALID_SERVICES` - the closed set of Atlassian services a
  department bot may probe (``jira``, ``bitbucket``, ``confluence``).
* :func:`staging_vault_path` - canonical ``vault:atlassian/_staging/...``
  path layout used during the staging phase of an atomic create.
* :func:`final_vault_path` - canonical ``vault:atlassian/<dept>/...``
  path layout used after a successful commit.
* :func:`scrub_plain_text_token` - best-effort heap zeroing for a
  plain-text token bytearray. Returns nothing; callers MUST drop the
  reference after the scrub.
* :func:`build_credential_payload` - minimal, schema-aligned mapping
  written into Vault for a single ``(dept_id, service)`` pair.

Both endpoints rely on the same Vault path conventions documented in
``design.md`` §"Vault path domeni":

* ``vault:atlassian/_staging/<request_id>/<service>`` - temporary
  staging slot, deleted on rollback (R3.6).
* ``vault:atlassian/<dept_id>/<service>`` - final slot, written after
  the DB transaction commits (R3.4).

The helpers are deliberately framework-agnostic: no FastAPI / asyncpg
imports here so the same module is usable from CLI tools, integration
tests and the sibling ``staging`` flow inside the wizard state
machine.
"""

from __future__ import annotations

import re
from typing import Final, Literal, Mapping

from vault_client import VaultPath

__all__ = [
    "VALID_SERVICES",
    "AtlassianService",
    "build_credential_payload",
    "final_vault_path",
    "scrub_plain_text_token",
    "staging_vault_path",
    "validate_dept_id",
    "validate_request_id",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The three Atlassian services a department bot may carry credentials
#: for (mirrors ``departments.schema.json`` ``bot.{jira,bitbucket,
#: confluence}`` and the ``probe_artifacts.service`` ``CHECK``
#: constraint).
AtlassianService = Literal["jira", "bitbucket", "confluence"]

#: Runtime mirror of :data:`AtlassianService` for set-membership checks.
VALID_SERVICES: Final[frozenset[str]] = frozenset({"jira", "bitbucket", "confluence"})

#: Department id pattern from ``departments.schema.json``
#: (``^[a-z][a-z0-9-]{1,30}$``). Mirrors the Postgres
#: ``departments.id`` column shape and the ``db_shared`` helper's own
#: ``_DEPT_ID_PATTERN`` so an unsanitised value cannot reach the
#: Vault path layer.
_DEPT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

#: Request-id pattern. UUID v4 / opaque slug - the staging path is
#: only addressable by the service, but we still constrain the
#: character class so a buggy caller cannot smuggle path traversal
#: characters.
_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_service(service: str) -> AtlassianService:
    """Return ``service`` if it is one of :data:`VALID_SERVICES`.

    Raises:
        ValueError: When ``service`` is not a recognised Atlassian
            surface. The error message lists the accepted values so the
            caller sees the contract immediately.
    """

    if not isinstance(service, str) or service not in VALID_SERVICES:
        raise ValueError(
            f"service must be one of {sorted(VALID_SERVICES)!r}; got {service!r}"
        )
    return service  # type: ignore[return-value]


def validate_dept_id(dept_id: str) -> str:
    """Return ``dept_id`` if it matches the schema regex.

    Mirrors the ``Department.id`` pattern from
    ``config/departments.schema.json`` (``^[a-z][a-z0-9-]{1,30}$``).
    """

    if not isinstance(dept_id, str) or not _DEPT_ID_PATTERN.fullmatch(dept_id):
        raise ValueError(
            "dept_id must match ^[a-z][a-z0-9-]{1,30}$ "
            f"(see config/departments.schema.json); got {dept_id!r}"
        )
    return dept_id


def validate_request_id(request_id: str) -> str:
    """Return ``request_id`` if it is a safe slug for path interpolation."""

    if not isinstance(request_id, str) or not _REQUEST_ID_PATTERN.fullmatch(
        request_id
    ):
        raise ValueError(
            "request_id must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$; "
            f"got {request_id!r}"
        )
    return request_id


# ---------------------------------------------------------------------------
# Path builders
# ---------------------------------------------------------------------------


def staging_vault_path(request_id: str, service: str) -> VaultPath:
    """Return the staging Vault path for ``(request_id, service)``.

    Layout: ``vault:atlassian/_staging/<request_id>/<service>``. This
    slot is written to **before** the read+write probes run and the DB
    insert is attempted; on rollback the slot is deleted (R3.6).

    Args:
        request_id: Opaque slug identifying the in-flight create
            attempt. Validated via :func:`validate_request_id`.
        service: One of :data:`VALID_SERVICES`.

    Returns:
        A validated :class:`VaultPath`.
    """

    rid = validate_request_id(request_id)
    svc = _validate_service(service)
    return VaultPath.parse(f"vault:atlassian/_staging/{rid}/{svc}")


def final_vault_path(dept_id: str, service: str) -> VaultPath:
    """Return the final Vault path for ``(dept_id, service)``.

    Layout: ``vault:atlassian/<dept_id>/<service>``. This is the
    location callers reference from ``credential_ref`` after the
    department row is committed (R3.4).
    """

    did = validate_dept_id(dept_id)
    svc = _validate_service(service)
    return VaultPath.parse(f"vault:atlassian/{did}/{svc}")


# ---------------------------------------------------------------------------
# Plain-text token hygiene
# ---------------------------------------------------------------------------


def scrub_plain_text_token(buffer: bytearray) -> None:
    """Best-effort scrub of a mutable token buffer.

    The function overwrites every byte of ``buffer`` with zero so a
    later memory snapshot does not surface the plain-text credential
    (R3.4 - "plain-text token … heap'ten ``bytearray.zero()`` ile
    silinir"). Python does not guarantee the GC will not have already
    copied the value elsewhere, so this is **best-effort** - callers
    SHOULD still drop their reference immediately after the call.

    Calling this on an immutable ``bytes`` object is a no-op (you
    cannot zero an immutable buffer); the function silently ignores
    that case so callers can wrap a defensive ``isinstance`` check
    in ``finally`` clauses without re-throwing.

    Args:
        buffer: A mutable :class:`bytearray` carrying token material.
    """

    if not isinstance(buffer, bytearray):
        # Immutable bytes are not scrubbable; silently no-op so
        # ``finally``-block scrubs can be unconditional.
        return
    for i in range(len(buffer)):
        buffer[i] = 0


# ---------------------------------------------------------------------------
# Vault payload builder
# ---------------------------------------------------------------------------


def build_credential_payload(
    *,
    username: str,
    personal_token: str,
    account_id: str | None = None,
) -> Mapping[str, str]:
    """Return the flat KV-v2 payload written into Vault for a credential.

    The shape mirrors what :class:`automation_service.probe.ResolvedCredential`
    expects on the read side:

    * ``username`` - Atlassian email or username.
    * ``personal_token`` - API token / app password. Stored verbatim
      in Vault; never echoed in HTTP responses, logs or DB columns
      (R3.4).
    * ``account_id`` - Optional. Populated by the auto-fetch step
      (task 6.2) when known at write time so subsequent reads do not
      need a second round-trip to the IdP.

    Args:
        username: Atlassian login (email or username).
        personal_token: Plain-text API token; will be encrypted by the
            Vault KV-v2 mount on write.
        account_id: Optional ``accountId`` returned by the Atlassian
            API for this credential.

    Returns:
        A flat ``dict[str, str]`` ready to hand to
        :meth:`vault_client.VaultClient.write`. The mapping is built
        fresh on every call so callers may freely scrub local buffers
        afterwards.
    """

    payload: dict[str, str] = {
        "username": username,
        "personal_token": personal_token,
    }
    if account_id:
        payload["account_id"] = account_id
    return payload
