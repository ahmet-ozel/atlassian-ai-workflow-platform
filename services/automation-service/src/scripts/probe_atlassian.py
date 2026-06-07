"""Atlassian connectivity probe for automation-service.

This module is the script referenced by the
``connectivity_probe_command`` field on the ``automation-service``
entry in ``platform/config/services.manifest.json``::

    "connectivity_probe_command": "python -m src.scripts.probe_atlassian"

``LifecycleService.start`` (admin-dashboard-api) invokes the configured
command after ``_wait_for_healthy`` reports the container ready and
before writing the final ``service_started`` audit. The script itself
is also runnable from the operator shell::

    python -m src.scripts.probe_atlassian

Behaviour
---------

For every ``(department_id, service)`` pair that has a row in
``automation.department_bots``, the script:

1. Resolves the credential reference by reading the row's
   ``credential_ref`` (a ``vault:atlassian/<dept>/<service>`` path).
2. Reads the secret from Vault via :func:`vault_client.make_client`.
3. Issues a single authenticated ``GET`` against the canonical "who
   am I?" endpoint for the service:

   * Jira          ``GET <base>/rest/api/3/myself``
   * Confluence    ``GET <base>/wiki/rest/api/user/current``
   * Bitbucket     ``GET <base>/2.0/user``

   Authentication uses HTTP Basic with the secret's ``username`` +
   ``personal_token`` fields, mirroring the Atlassian REST API
   "Basic auth + API token" pattern (the same fields the Foundation
   probe runner uses, see
   :mod:`automation_service.probe`). A non-2xx response or any
   transport-level error counts as a failure.

Exit contract
-------------

* All probes succeed  process exits with status ``0`` and no output
  on stderr.
* One or more probes fail  process exits with status ``1``; for each
  failure exactly one line is written to stderr in the format::

    dept=<dept_id> service=<service> reason=<short reason>

  No personal tokens, full URLs, or Basic-auth headers are ever
  written to stdout / stderr - the ``reason`` is a structural label
  ("http_401", "http_500", "vault_missing", "incomplete_secret",
  "transport_error:<class-name>", ...).

Configuration
-------------

The script is configured purely through environment variables so
the same image / command line works in Compose, in the LifecycleService
subprocess, and in operator shells:

* ``POSTGRES_DSN`` - required; standard ``postgresql://...`` URI used
  by the rest of the automation-service.
* ``VAULT_BACKEND`` / ``VAULT_ADDR`` / ``VAULT_TOKEN`` /
  ``VAULT_KV_MOUNT`` - forwarded verbatim to
  :func:`vault_client.make_client`.
* ``PROBE_HTTP_TIMEOUT_SECONDS`` - optional float, default ``10``.
  Per-request HTTP timeout for the Atlassian read probe.
* ``PROBE_DRY_RUN`` - optional truthy flag. When set, the script
  enumerates the rows it would probe and exits ``0`` without making
  any HTTP requests. Useful for smoke-testing the manifest wiring
  without touching real Atlassian instances.

The script never logs the secret or the resolved Basic-auth header;
all surfaces (stdout, stderr, traceback messages) are restricted to
the structural labels described above.

This module is intentionally self-contained: it does **not** import
from ``automation_service.app`` so it can be invoked while the FastAPI
process is still in early startup (Step 9.5 happens before the final
ready transition).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Final, Iterable, Literal, Mapping

import asyncpg
import httpx

from vault_client import VaultPath, make_client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Atlassian services this probe knows how to read. Mirrors the
#: ``automation.department_bots.service`` ``CHECK`` constraint
#: (``infra/postgres/init/10_automation.sql``) and the
#: :data:`automation_service.probe.ProbeService` literal.
ProbeService = Literal["jira", "bitbucket", "confluence"]

_SERVICE_PATHS: Final[Mapping[ProbeService, str]] = {
    # Cloud Jira REST v3 "who am I?" endpoint. The DC equivalent
    # ``/rest/api/2/myself`` returns the same shape - the script
    # treats either as success because Atlassian Cloud and DC both
    # return 200 OK for a valid Basic-auth pair.
    "jira": "/rest/api/3/myself",
    # Confluence Cloud "current user" endpoint. The path lives below
    # the ``/wiki`` mount point on Cloud sites; the script does not
    # special-case DC because the same ``/wiki/rest/api/user/current``
    # path is canonical there too.
    "confluence": "/wiki/rest/api/user/current",
    # Bitbucket Cloud current-user endpoint. The DC equivalent
    # (``/rest/api/1.0/users/<slug>``) requires a slug which the
    # bot account doesn't carry in band; for the connectivity probe
    # we restrict ourselves to the Cloud path which mirrors the
    # behaviour of :mod:`automation_service.probe`.
    "bitbucket": "/2.0/user",
}

#: Default per-request HTTP timeout. Operators can override via
#: ``PROBE_HTTP_TIMEOUT_SECONDS``; the default is generous enough for
#: a Cloud round-trip but well below the 30-second budget the
#: LifecycleService allocates to ``connectivity_probe_command`` as a
#: whole.
_DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BotRow:
    """A row from ``automation.department_bots`` we want to probe."""

    department_id: str
    service: ProbeService
    credential_ref: str


@dataclass(frozen=True, slots=True)
class _ProbeFailure:
    """A single probe failure recorded for stderr emission.

    The ``reason`` field is intentionally a short, structural label
    (no URLs, no auth material, no full exception messages) so we can
    safely echo it to stderr without leaking secrets. The
    LifecycleService trims the captured stderr to 500 characters
    before writing it into ``credentials_probe_detail``; keeping the
    label compact ensures every failure survives that trim.
    """

    department_id: str
    service: ProbeService
    reason: str


# ---------------------------------------------------------------------------
# Postgres + Vault helpers
# ---------------------------------------------------------------------------


async def _fetch_bot_rows(dsn: str) -> tuple[_BotRow, ...]:
    """Read every ``(department_id, service, credential_ref)`` triple.

    The query joins through ``automation.department_bots`` only -
    the same table :class:`automation_service.decision.credential_resolver.CredentialResolver`
    consumes - so the script always probes the *same* set of
    credentials the worker actually uses.

    Returns:
        A tuple of :class:`_BotRow` instances ordered by
        ``(department_id, service)`` so the script's stderr output is
        deterministic across runs (helps with operator diff'ing).
    """

    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT department_id, service, credential_ref
            FROM automation.department_bots
            ORDER BY department_id, service
            """,
        )
    finally:
        await conn.close()

    out: list[_BotRow] = []
    for row in rows:
        service = row["service"]
        if service not in _SERVICE_PATHS:
            # Defensive: the table-level CHECK constraint already
            # restricts this to {jira, bitbucket, confluence}; an
            # unexpected value indicates a schema drift bug. We skip
            # rather than crash so operators still get probe results
            # for every well-formed row.
            continue
        out.append(
            _BotRow(
                department_id=str(row["department_id"]),
                service=service,  # type: ignore[arg-type]
                credential_ref=str(row["credential_ref"]),
            )
        )
    return tuple(out)


def _read_credential(
    vault, credential_ref: str
) -> Mapping[str, str] | None:
    """Read the credential at *credential_ref* from Vault.

    Returns ``None`` when the path is missing (idiomatic
    ``KeyError`` from the :class:`vault_client.VaultClient`
    protocol). Any other Vault failure (network, permission) is
    propagated up so the caller can label it ``vault_error:<class>``.

    The :class:`VaultPath` constructor validates the
    ``vault:<path>`` grammar; an invalid ``credential_ref`` (e.g. a
    legacy plain-text token written before Vault path enforcement) raises
    :class:`ValueError`, which the caller maps to a
    ``credential_ref_invalid`` reason.
    """

    path = VaultPath.parse(credential_ref)
    try:
        return vault.read(path)
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# HTTP probe
# ---------------------------------------------------------------------------


def _classify_status(status_code: int) -> str:
    """Return a short structural label for a non-2xx response."""

    return f"http_{status_code}"


async def _probe_one(
    client: httpx.AsyncClient,
    row: _BotRow,
    secret: Mapping[str, str],
) -> _ProbeFailure | None:
    """Issue a single authenticated GET; return ``None`` on success.

    The function never raises - every error path is converted into a
    :class:`_ProbeFailure` with a sanitised reason label so the
    caller's aggregation stays branchless.
    """

    url = secret.get("url", "").rstrip("/")
    username = secret.get("username", "")
    token = secret.get("personal_token", "")

    if not (url and username and token):
        return _ProbeFailure(
            department_id=row.department_id,
            service=row.service,
            reason="incomplete_secret",
        )

    full_url = f"{url}{_SERVICE_PATHS[row.service]}"

    try:
        response = await client.get(
            full_url,
            auth=(username, token),
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        # ``httpx.HTTPError`` covers TimeoutException, ConnectError,
        # ReadError, etc. Surface only the class name - the message
        # may include the URL or other operationally-sensitive
        # detail.
        return _ProbeFailure(
            department_id=row.department_id,
            service=row.service,
            reason=f"transport_error:{type(exc).__name__}",
        )
    except Exception as exc:  # noqa: BLE001 - defensive, never echo message
        return _ProbeFailure(
            department_id=row.department_id,
            service=row.service,
            reason=f"unexpected_error:{type(exc).__name__}",
        )

    if 200 <= response.status_code < 300:
        return None

    return _ProbeFailure(
        department_id=row.department_id,
        service=row.service,
        reason=_classify_status(response.status_code),
    )


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _format_failure(failure: _ProbeFailure) -> str:
    """Render a :class:`_ProbeFailure` in the contractual stderr format.

    The trailing newline is added by the caller (``print(..., file=sys.stderr)``).
    """

    return (
        f"dept={failure.department_id} "
        f"service={failure.service} "
        f"reason={failure.reason}"
    )


def _emit_failures(failures: Iterable[_ProbeFailure]) -> None:
    """Write one line per failure to stderr in the contractual format."""

    for failure in failures:
        print(_format_failure(failure), file=sys.stderr)


def _truthy_env(name: str, env: Mapping[str, str]) -> bool:
    """Parse a flag-style environment variable.

    Treats ``"1"``, ``"true"``, ``"yes"`` (case-insensitive) as true;
    everything else as false. Centralised here so PROBE_DRY_RUN and
    any future toggles share the same semantics.
    """

    raw = env.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _http_timeout_seconds(env: Mapping[str, str]) -> float:
    """Resolve the per-request timeout from the environment."""

    raw = env.get("PROBE_HTTP_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_HTTP_TIMEOUT_SECONDS
    return value


async def _run(env: Mapping[str, str]) -> int:
    """Run the probe end-to-end; return a process exit code.

    Separated from :func:`main` so unit / integration tests can call
    it with an explicit fake environment instead of having to
    monkeypatch ``os.environ``.
    """

    dsn = env.get("POSTGRES_DSN", "").strip()
    if not dsn:
        # Treat missing DSN as a probe-wide failure rather than a
        # crash - uyumluluk Q10 expects exit_code=1 on *any* probe
        # problem so the dashboard banner surfaces it.
        print(
            "dept=<all> service=<all> reason=missing_postgres_dsn",
            file=sys.stderr,
        )
        return 1

    try:
        rows = await _fetch_bot_rows(dsn)
    except Exception as exc:  # noqa: BLE001 - surface as structural label
        print(
            "dept=<all> service=<all> "
            f"reason=postgres_error:{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    if not rows:
        # No registered bots  nothing to probe. The probe is
        # informational only; an empty table is not a failure
        # condition (a brand-new install will exercise this branch).
        return 0

    if _truthy_env("PROBE_DRY_RUN", env):
        return 0

    try:
        vault = make_client(env)
    except Exception as exc:  # noqa: BLE001
        print(
            "dept=<all> service=<all> "
            f"reason=vault_factory_error:{type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    failures: list[_ProbeFailure] = []
    timeout = _http_timeout_seconds(env)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for row in rows:
            try:
                secret = _read_credential(vault, row.credential_ref)
            except ValueError:
                # Malformed ``credential_ref`` (does not match the
                # ``vault:<path>`` grammar). Recorded as a probe
                # failure rather than aborting the whole run.
                failures.append(
                    _ProbeFailure(
                        department_id=row.department_id,
                        service=row.service,
                        reason="credential_ref_invalid",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    _ProbeFailure(
                        department_id=row.department_id,
                        service=row.service,
                        reason=f"vault_error:{type(exc).__name__}",
                    )
                )
                continue

            if secret is None:
                failures.append(
                    _ProbeFailure(
                        department_id=row.department_id,
                        service=row.service,
                        reason="vault_missing",
                    )
                )
                continue

            failure = await _probe_one(client, row, secret)
            if failure is not None:
                failures.append(failure)

    if failures:
        _emit_failures(failures)
        return 1

    return 0


def main() -> int:
    """Module entry point used by ``python -m src.scripts.probe_atlassian``."""

    return asyncio.run(_run(os.environ))


if __name__ == "__main__":  # pragma: no cover - manual entry
    raise SystemExit(main())
