"""Integration coverage for ``probe_atlassian.py`` script end-to-end.



Scenario coverage
-----------------

The script under test
(:mod:`automation_service.scripts.probe_atlassian` - installed at
``platform/services/automation-service/src/scripts/probe_atlassian.py``)
is the ``connectivity_probe_command`` referenced from
``platform/config/services.manifest.json`` for the ``automation-service``
entry. ``LifecycleService.start`` invokes it during startup
connectivity checks.

Per the script MUST:

* exit ``0`` when every ``(dept, service)`` row in
 ``automation.department_bots`` resolves to a Vault secret whose
 ``GET <base>/<service-specific-myself>`` returns 2xx;
* exit ``1`` when **any** probe fails, and write one
 ``dept=<dept_id> service=<service> reason=<short reason>`` line per
 failing probe to stderr (no plain tokens, no full URLs).

The test exercises both branches against a real
:class:`vault_client.LocalDevBackend` (an encrypted-file Vault that
provides a hermetic encrypted-file Vault for the run - the file lives
under :func:`tmp_path` and is teared down by
pytest) and a real :class:`httpx.MockTransport` (the in-tree
equivalent of ``pytest-httpx`` - same wire-level mock semantics
without the extra dependency the workspace's ``tests/requirements.txt``
deliberately keeps lean).

Postgres is the third collaborator. The script reads the
``department_bots`` table once via :mod:`asyncpg` to discover which
``(dept, service)`` pairs to probe; for hermeticity we patch
``asyncpg.connect`` with a tiny in-memory stand-in that returns the
fixture rows. This mirrors the pattern in
:mod:`tests.integration.test_capability_denied`, which boots the full
FastAPI app against in-memory stubs of every collaborator.

What the test deliberately does NOT cover
-----------------------------------------

* The Vault factory's ``hashicorp`` backend selection - covered by
 :mod:`platform.libs.vault_client.tests.test_basic`.
* The manifest schema for ``connectivity_probe_command`` - covered by
 :mod:`platform.tests.ci.test_manifest_schema`.
* ``LifecycleService.start`` connectivity-probe wiring (subprocess invocation,
 state cache update, audit emission) - covered by
 :mod:`platform.services.admin-dashboard-api.tests.property.test_connectivity_probe`.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
import nacl.utils
import pytest

# ---------------------------------------------------------------------------
# Make the in-tree ``services/automation-service/src`` importable so the
# ``src.scripts.probe_atlassian`` module resolves without an editable
# install. Mirrors the bootstrap used by
# :mod:`tests.integration.test_capability_denied`.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "automation-service"
)
for _bootstrap_path in (_AUTOMATION_ROOT, _AUTOMATION_ROOT / "src"):
    _bs = str(_bootstrap_path)
    if _bs not in sys.path:
        sys.path.insert(0, _bs)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Atlassian-side base URLs we'll plant in Vault. Kept distinct per
#: department so a misrouted probe surfaces immediately (the mock
#: transport's request handler routes by ``request.url.host``).
_PAYMENTS_JIRA_BASE = "https://payments.atlassian.net"
_PAYMENTS_CONFLUENCE_BASE = "https://payments.atlassian.net"
_PAYMENTS_BITBUCKET_BASE = "https://api.bitbucket.org"

#: Service → expected probe path (the script's private
#: ``_SERVICE_PATHS`` mapping; we duplicate the constants here so a
#: regression in the script is caught by URL-path mismatch in the mock
#: transport rather than by an opaque ``http_404`` reason).
_PROBE_PATHS = {
    "jira": "/rest/api/3/myself",
    "confluence": "/wiki/rest/api/user/current",
    "bitbucket": "/2.0/user",
}


# ---------------------------------------------------------------------------
# Fake asyncpg connection - returns a fixed list of bot rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    """Tiny stand-in for an :class:`asyncpg.Record` row.

 The script only accesses ``row["department_id"]``,
 ``row["service"]``, and ``row["credential_ref"]``, so a frozen
 dataclass with ``__getitem__`` covers the surface area exactly.
 """

    department_id: str
    service: str
    credential_ref: str

    def __getitem__(self, key: str) -> str:  # type: ignore[override]
        return getattr(self, key)


class _FakeConnection:
    """Minimal ``asyncpg.Connection`` stand-in.

 Only the methods the script actually invokes are implemented:
 ``fetch`` (returns the pre-canned rows) and ``close``. Any other
 attribute access raises so a regression that adds an unexpected
 DB call surfaces loudly rather than silently no-op'ing.
 """

    def __init__(self, rows: tuple[_Row, ...]) -> None:
        self._rows = rows
        self.fetched_queries: list[str] = []

    async def fetch(self, query: str) -> tuple[_Row, ...]:
        self.fetched_queries.append(query)
        return self._rows

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Mock transport - emulates Atlassian "who am I?" endpoints
# ---------------------------------------------------------------------------


@dataclass
class _RecordedCall:
    """One request seen by :func:`_make_transport`."""

    method: str
    url: str
    auth_header: str | None


def _make_transport(
    *,
    failures: Mapping[tuple[str, str], int] | None = None,
    transport_errors: frozenset[tuple[str, str]] = frozenset(),
    record: list[_RecordedCall] | None = None,
) -> httpx.MockTransport:
    """Build a :class:`httpx.MockTransport` that mimics Atlassian.

 Parameters
 ----------
 failures:
 Optional mapping of ``(host, path)`` → HTTP status code. Any
 ``(host, path)`` listed here returns the configured non-2xx
 status; the rest return 200 with an Atlassian-shaped body.
 transport_errors:
 Optional set of ``(host, path)`` tuples. Any incoming request
 whose ``(host, path)`` is in this set raises
 :class:`httpx.ConnectError`, which the script must surface as
 ``transport_error:ConnectError`` (the structural label used
 by ``probe_atlassian._probe_one``).
 record:
 Optional list to which every served request is appended for
 post-test assertions.
 """

    failures = failures or {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(
                _RecordedCall(
                    method=request.method,
                    url=str(request.url),
                    auth_header=request.headers.get("authorization"),
                )
            )
        host = request.url.host
        path = request.url.path
        key = (host, path)

        if key in transport_errors:
            raise httpx.ConnectError(
                "simulated transport failure",
                request=request,
            )

        if key in failures:
            return httpx.Response(
                failures[key],
                json={"errorMessages": ["forced failure for test"]},
            )

        # Success-shaped JSON. Atlassian Cloud returns slightly
        # different shapes per service, but the script only inspects
        # the HTTP status - any 2xx body is fine.
        return httpx.Response(
            200,
            json={"accountId": "stub-account", "displayName": "Bot"},
        )

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vault_env(tmp_path: Path) -> dict[str, str]:
    """Return an env dict that builds a fresh local-dev Vault under tmp."""

    key_hex = nacl.utils.random(32).hex()
    return {
        "VAULT_BACKEND": "local-dev",
        "VAULT_LOCAL_KEY": key_hex,
        "VAULT_LOCAL_STORE": str(tmp_path / "vault.json"),
    }


def _seed_vault(env: Mapping[str, str], secrets: Mapping[str, Mapping[str, str]]) -> None:
    """Plant *secrets* into the local-dev Vault under their full paths.

 *secrets* maps a full ``vault:atlassian/<dept>/<service>``
 reference to its ``{url, username, personal_token}`` payload.
 """

    from vault_client import VaultPath, make_client

    client = make_client(env)
    for ref, payload in secrets.items():
        client.write(VaultPath.parse(ref), dict(payload))


def _import_probe_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the script under test fresh so monkeypatched globals stick.

 The script lives at ``services/automation-service/src/scripts/
 probe_atlassian.py`` and is normally addressed as
 ``src.scripts.probe_atlassian`` (the dotted name baked into
 ``connectivity_probe_command``). We re-import it inside the test
 so each test gets a clean copy whose ``asyncpg`` and ``httpx``
 references can be redirected via :func:`monkeypatch.setattr`.
 """

    # Drop any previously-loaded copy so monkeypatched module-level
    # references don't leak between tests.
    for name in list(sys.modules):
        if name == "src.scripts.probe_atlassian" or name == "src.scripts":
            sys.modules.pop(name)
    return importlib.import_module("src.scripts.probe_atlassian")


def _patch_collaborators(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    rows: tuple[_Row, ...],
    transport: httpx.MockTransport,
    connect_calls: list[str],
) -> None:
    """Redirect the script's ``asyncpg`` and ``httpx.AsyncClient`` deps.

 * ``asyncpg.connect`` returns a :class:`_FakeConnection` carrying
 the supplied *rows*. Calls are appended to ``connect_calls`` so
 tests can assert the DSN was honoured.
 * ``httpx.AsyncClient`` is wrapped so every instance picks up the
 supplied :class:`httpx.MockTransport`. This avoids any
 real-network egress.
 """

    async def _fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        # ``asyncpg.connect(dsn=...)`` is the only call the script
        # makes; capturing the kwarg is enough to assert the DSN
        # plumbing without depending on positional arg shape.
        connect_calls.append(str(kwargs.get("dsn") or (args[0] if args else "")))
        return _FakeConnection(rows)

    monkeypatch.setattr(module.asyncpg, "connect", _fake_connect)

    real_async_client = module.httpx.AsyncClient

    def _async_client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _async_client_factory)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_credentials_succeed_returns_exit_zero_and_no_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates happy path.

 Every dept/service pair has a valid Vault secret and the mock
 Atlassian transport returns 200 for every probe. The script MUST
 exit ``0`` and write **nothing** to stderr 's
 second sentence: "tüm probe'lar başarılı → exit_code=0").
 """

    env = _vault_env(tmp_path)
    env["POSTGRES_DSN"] = "postgresql://probe:probe@localhost:5432/probe"

    secrets = {
        "vault:atlassian/payments/jira": {
            "url": _PAYMENTS_JIRA_BASE,
            "username": "bot@payments",
            "personal_token": "tok-payments-jira",
        },
        "vault:atlassian/payments/confluence": {
            "url": _PAYMENTS_CONFLUENCE_BASE,
            "username": "bot@payments",
            "personal_token": "tok-payments-confluence",
        },
        "vault:atlassian/payments/bitbucket": {
            "url": _PAYMENTS_BITBUCKET_BASE,
            "username": "bot-payments",
            "personal_token": "tok-payments-bitbucket",
        },
    }
    _seed_vault(env, secrets)

    rows = (
        _Row("payments", "jira", "vault:atlassian/payments/jira"),
        _Row("payments", "confluence", "vault:atlassian/payments/confluence"),
        _Row("payments", "bitbucket", "vault:atlassian/payments/bitbucket"),
    )

    recorded: list[_RecordedCall] = []
    transport = _make_transport(record=recorded)

    module = _import_probe_module(monkeypatch)
    connect_calls: list[str] = []
    _patch_collaborators(
        monkeypatch,
        module,
        rows=rows,
        transport=transport,
        connect_calls=connect_calls,
    )

    exit_code = asyncio.run(module._run(env))

    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"all-success scenario must exit 0 ; got {exit_code} "
        f"with stderr={captured.err!r}"
    )
    assert captured.err == "", (
        f"all-success scenario must produce empty stderr ; "
        f"got {captured.err!r}"
    )

    # ---- Wiring sanity checks ----------------------------------------
    assert connect_calls == [env["POSTGRES_DSN"]], (
        f"script must connect using the configured DSN; got {connect_calls!r}"
    )
    served_paths = sorted({call.url for call in recorded})
    expected_urls = sorted(
        f"{secrets[ref]['url']}{_PROBE_PATHS[row.service]}"
        for ref, row in zip(secrets, rows, strict=True)
    )
    assert served_paths == expected_urls, (
        f"script must hit the canonical 'who am I?' endpoint per service "
        f"; got {served_paths!r} expected {expected_urls!r}"
    )
    # Every request must carry HTTP Basic auth - never anonymous.
    assert all(call.auth_header and call.auth_header.startswith("Basic ")
               for call in recorded), (
        "every probe request must use HTTP Basic auth derived from the "
        f"Vault secret; got headers={[c.auth_header for c in recorded]!r}"
    )
    # The token MUST NOT appear in stderr (defence-in-depth - the
    # script's reason labels are structural; tokens are out-of-band).
    assert "tok-payments-jira" not in captured.err
    assert "tok-payments-confluence" not in captured.err
    assert "tok-payments-bitbucket" not in captured.err


def test_any_credential_failure_returns_exit_one_with_dept_service_reason_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates failure path & stderr format.

 Two of the three probes succeed and a third fails (HTTP 401).
 The script MUST exit ``1`` and write exactly one stderr line for
 the failing probe in the contractual
 ``dept=<dept> service=<service> reason=<reason>`` format.
 """

    env = _vault_env(tmp_path)
    env["POSTGRES_DSN"] = "postgresql://probe:probe@localhost:5432/probe"

    secrets = {
        "vault:atlassian/payments/jira": {
            "url": _PAYMENTS_JIRA_BASE,
            "username": "bot@payments",
            "personal_token": "tok-payments-jira",
        },
        "vault:atlassian/payments/confluence": {
            "url": _PAYMENTS_CONFLUENCE_BASE,
            "username": "bot@payments",
            "personal_token": "tok-payments-confluence",
        },
        "vault:atlassian/payments/bitbucket": {
            "url": _PAYMENTS_BITBUCKET_BASE,
            "username": "bot-payments",
            "personal_token": "tok-payments-bitbucket",
        },
    }
    _seed_vault(env, secrets)

    rows = (
        _Row("payments", "jira", "vault:atlassian/payments/jira"),
        _Row("payments", "confluence", "vault:atlassian/payments/confluence"),
        _Row("payments", "bitbucket", "vault:atlassian/payments/bitbucket"),
    )

    # Force the bitbucket probe to come back 401 - the canonical
    # signal for a token that's been revoked or has the wrong scope.
    transport = _make_transport(
        failures={
            ("api.bitbucket.org", _PROBE_PATHS["bitbucket"]): 401,
        },
    )

    module = _import_probe_module(monkeypatch)
    connect_calls: list[str] = []
    _patch_collaborators(
        monkeypatch,
        module,
        rows=rows,
        transport=transport,
        connect_calls=connect_calls,
    )

    exit_code = asyncio.run(module._run(env))

    captured = capsys.readouterr()
    assert exit_code == 1, (
        f"any-failure scenario must exit 1 ; got {exit_code} "
        f"with stderr={captured.err!r}"
    )

    stderr_lines = [
        line for line in captured.err.splitlines() if line.strip()
    ]
    assert len(stderr_lines) == 1, (
        f"exactly one failing probe → exactly one stderr line; got "
        f"{stderr_lines!r}"
    )
    line = stderr_lines[0]
    # Format contract per the implementation / :
    # ``dept={...} service={...} reason={...}``
    assert line == "dept=payments service=bitbucket reason=http_401", (
        f"stderr line must follow the 'dept=… service=… reason=…' "
        f"format with the structural http_<code> reason label; got {line!r}"
    )

    # Tokens MUST NEVER reach stderr.
    assert "tok-payments-bitbucket" not in captured.err
    assert "tok-payments-jira" not in captured.err
    assert "tok-payments-confluence" not in captured.err


def test_multiple_failures_emit_one_stderr_line_per_failed_probe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates multi-failure aggregation.

 Two distinct failure modes hit two different (dept, service)
 pairs in the same run. The script MUST exit ``1`` and emit one
 stderr line per failure, each in the contractual format. Reasons
 must reflect the actual failure mode (HTTP status vs transport
 error class).
 """

    env = _vault_env(tmp_path)
    env["POSTGRES_DSN"] = "postgresql://probe:probe@localhost:5432/probe"

    # Two depts - separate Atlassian sites - make the per-line
    # ``dept=`` field meaningful.
    secrets = {
        "vault:atlassian/payments/jira": {
            "url": "https://payments.atlassian.net",
            "username": "bot@payments",
            "personal_token": "tok-payments-jira",
        },
        "vault:atlassian/platform/jira": {
            "url": "https://platform.atlassian.net",
            "username": "bot@platform",
            "personal_token": "tok-platform-jira",
        },
        "vault:atlassian/platform/confluence": {
            "url": "https://platform.atlassian.net",
            "username": "bot@platform",
            "personal_token": "tok-platform-confluence",
        },
    }
    _seed_vault(env, secrets)

    rows = (
        _Row("payments", "jira", "vault:atlassian/payments/jira"),
        _Row("platform", "jira", "vault:atlassian/platform/jira"),
        _Row(
            "platform",
            "confluence",
            "vault:atlassian/platform/confluence",
        ),
    )

    transport = _make_transport(
        failures={
            ("payments.atlassian.net", _PROBE_PATHS["jira"]): 500,
        },
        transport_errors=frozenset(
            {("platform.atlassian.net", _PROBE_PATHS["confluence"])}
        ),
    )

    module = _import_probe_module(monkeypatch)
    connect_calls: list[str] = []
    _patch_collaborators(
        monkeypatch,
        module,
        rows=rows,
        transport=transport,
        connect_calls=connect_calls,
    )

    exit_code = asyncio.run(module._run(env))
    captured = capsys.readouterr()

    assert exit_code == 1
    lines = sorted(
        line for line in captured.err.splitlines() if line.strip()
    )
    assert lines == sorted(
        [
            "dept=payments service=jira reason=http_500",
            "dept=platform service=confluence "
            "reason=transport_error:ConnectError",
        ]
    ), (
        f"multi-failure scenario must emit one structured stderr line "
        f"per failure ; got {lines!r}"
    )


def test_vault_missing_credential_is_reported_with_vault_missing_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates Vault read miss surfaces structurally.

 A row in ``department_bots`` references a Vault path that has not
 been written. The script MUST treat this as a probe failure
 (exit 1) with the structural reason ``vault_missing`` rather than
 crashing or leaking the path/token; this matches the failure-
 handling contract spelled out in :mod:`probe_atlassian`'s
 docstring ("vault_missing", "incomplete_secret", ...).
 """

    env = _vault_env(tmp_path)
    env["POSTGRES_DSN"] = "postgresql://probe:probe@localhost:5432/probe"

    # Initialise the encrypted store but leave the referenced path
    # absent. Touching the store first guarantees the local-dev
    # backend's ``read`` returns ``KeyError`` instead of raising the
    # "store unreadable" error path.
    _seed_vault(
        env,
        {
            "vault:atlassian/_unused/jira": {
                "url": "https://example.atlassian.net",
                "username": "x",
                "personal_token": "x",
            }
        },
    )

    rows = (
        _Row(
            "ghost-dept",
            "jira",
            "vault:atlassian/ghost-dept/jira",  # never written
        ),
    )

    # The transport must never be hit - if the script tries to fetch
    # an HTTP endpoint without a secret, the test fails loudly.
    recorded: list[_RecordedCall] = []
    transport = _make_transport(record=recorded)

    module = _import_probe_module(monkeypatch)
    connect_calls: list[str] = []
    _patch_collaborators(
        monkeypatch,
        module,
        rows=rows,
        transport=transport,
        connect_calls=connect_calls,
    )

    exit_code = asyncio.run(module._run(env))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorded == [], (
        "vault_missing path must short-circuit before any HTTP probe; "
        f"got served calls={recorded!r}"
    )

    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines == [
        "dept=ghost-dept service=jira reason=vault_missing"
    ], (
        f"vault miss must produce a single 'reason=vault_missing' "
        f"stderr line in dept/service/reason format ; got {lines!r}"
    )


def test_no_registered_bots_returns_exit_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validates empty registry is not a failure.

 A brand-new install with zero ``department_bots`` rows must not
 fail the connectivity probe (would block ``LifecycleService.start``
 unnecessarily). The script must exit ``0`` and produce
 no stderr output.
 """

    env = _vault_env(tmp_path)
    env["POSTGRES_DSN"] = "postgresql://probe:probe@localhost:5432/probe"
    # Touch the Vault store so ``make_client`` can be called without
    # blowing up if we were to reach it (we shouldn't - empty rows
    # short-circuits before Vault is even instantiated).
    _seed_vault(
        env,
        {
            "vault:atlassian/_unused/jira": {
                "url": "https://example.atlassian.net",
                "username": "x",
                "personal_token": "x",
            }
        },
    )

    transport = _make_transport()
    module = _import_probe_module(monkeypatch)
    connect_calls: list[str] = []
    _patch_collaborators(
        monkeypatch,
        module,
        rows=(),
        transport=transport,
        connect_calls=connect_calls,
    )

    exit_code = asyncio.run(module._run(env))
    captured = capsys.readouterr()

    assert exit_code == 0, (
        f"empty registry must exit 0; got {exit_code} stderr={captured.err!r}"
    )
    assert captured.err == ""
    assert connect_calls == [env["POSTGRES_DSN"]]
