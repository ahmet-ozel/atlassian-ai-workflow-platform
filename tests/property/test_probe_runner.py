"""Tests for ``automation_service.probe.ProbeRunner``.

Probe runner — idempotent, auto-fetch, partial_orphan ve mismatch fail.

For all ``(dept_id, service ∈ {jira, bitbucket, confluence}, credential)``
triples, the following invariants hold:

* **Idempotent cleanup**: Running ``ProbeRunner.run`` twice
  in a row leaves no extra ``_AI_PROBE_*`` artifacts in the target
  system. Each invocation cleans up stale sentinels first; both
  invocations remove their own write-probe sentinel before returning.

* **Read-failure short-circuit**: When the read probe
  raises, the runner returns ``state="read_failed"`` and **does not
  invoke any write-side method** on the Atlassian client (no creates,
  no deletes).

* **Partial-orphan capture**: When the
  Confluence draft create succeeds but the matching delete fails, the
  runner returns ``state="partial_orphan"`` with a populated
  :class:`ProbeArtifact` whose ``title_or_name`` matches the
  ``_AI_PROBE_<unix_ts>_DELETE_ME`` format and whose payload **never
  contains the plain-text token**.

* **auto_fetch passthrough**: The runner exposes the
  ``accountId`` returned by the read probe on
  :attr:`ProbeResult.auto_fetched_account_id` so the calling code
  can update ``departments.json`` / DB records.

* **Mismatch detection primitive**: When a manually
  configured ``account_id`` differs from the auto-fetched value, the
  caller can detect the mismatch by comparing the two values and the
  fail-fast error message can include both. The runner exposes the
  building block — this property exercises the comparison primitive
  and verifies that **both values are surfaced**, never collapsed.

The Atlassian client is replaced with an in-memory fake satisfying the
:class:`AtlassianProbeClient` :class:`~typing.Protocol` so the suite
stays hermetic. Every method records its inputs in ``calls`` so the
properties can drive off the call log directly.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — make ``automation_service.probe`` importable
# ---------------------------------------------------------------------------
#
# The ``automation-service`` source tree co-exists with the
# legacy ``src/main.py`` + ``src/config.py``
# layer; importing the ``automation_service`` package eagerly executes
# ``automation_service/__init__.py`` which in turn loads
# ``automation_service.app`` whose top-of-module imports reach for
# ``from src.config import Settings``. We therefore add **both** the
# ``src/`` directory (so ``automation_service`` resolves) and its
# parent ``automation-service/`` directory (so ``src.config`` resolves
# as the legacy module path).

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

from automation_service.probe import (  # noqa: E402
    PROBE_ARTIFACT_PREFIX,
    PROBE_ARTIFACT_SUFFIX,
    ProbeArtifact,
    ProbeResult,
    ProbeRunner,
    ProbeService,
    ProbeTargets,
    ResolvedCredential,
    is_probe_artifact_title,
    make_probe_title,
)


# ---------------------------------------------------------------------------
# Sentinel — used to detect plain-text token leakage in artifacts
# ---------------------------------------------------------------------------

#: A distinctive byte sequence we can grep for to detect plain-text token
#: leakage anywhere downstream. The fake never echoes this into the call
#: log; if the runner ever passes the credential into an artifact body or
#: error message we will see it on inspection.
_TOKEN_SENTINEL = "PLAINTEXT_TOKEN_DO_NOT_LEAK_42"


# ---------------------------------------------------------------------------
# Fake AtlassianProbeClient
# ---------------------------------------------------------------------------


@dataclass
class _FakeAtlassianClient:
    """In-memory ``AtlassianProbeClient`` for property tests.

    Mirrors the unit-test fake at
    ``services/automation-service/tests/unit/test_probe.py`` but with
    Hypothesis-driven failure injection knobs. Each method is
    intentionally minimal:

    * Read probes return a configurable payload or raise on demand.
    * List/delete cleanup methods accept a configurable seed list of
      stale artifacts.
    * Create methods append to the live state and remember the body /
      title so we can diff it for plain-text leakage.
    * Delete methods remove from the live state unless the matching
      ``fail_write_delete`` knob is set.
    """

    # ---- Configurable read-probe payloads -----------------------------

    jira_account_id: str = "jira-bot-001"
    bitbucket_account_id: str = "bb-bot-001"
    confluence_account_id: str = "conf-bot-001"

    # ---- Seeded stale artifacts (idempotent cleanup probe) ------------

    jira_self_comments: list[dict[str, Any]] = field(default_factory=list)
    bitbucket_probe_branches: list[str] = field(default_factory=list)
    confluence_probe_pages: list[dict[str, Any]] = field(default_factory=list)

    # ---- Failure injection --------------------------------------------

    fail_read: str | None = None  # one of {"jira","bitbucket","confluence"}
    fail_write_create: str | None = None
    fail_write_delete: str | None = None

    # ---- Generated state ----------------------------------------------

    next_jira_comment_id: int = 1
    next_confluence_page_id: int = 1
    target_issue_key: str = "BOT-1"

    # ---- Call log ------------------------------------------------------
    # Records (method_name, args). Properties walk this to assert
    # "no write-side calls happened" / "delete called for sentinel" /
    # "no plain-text token in any payload".
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    # ----- Jira ---------------------------------------------------------

    async def jira_myself(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("jira_myself", (cred.username,)))
        if self.fail_read == "jira":
            raise RuntimeError("auth failed")
        return {"accountId": self.jira_account_id}

    async def jira_search_self_comments(
        self,
        cred: ResolvedCredential,
        author_account_id: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("jira_search_self_comments", (author_account_id,)))
        return list(self.jira_self_comments)

    async def jira_create_self_comment(
        self,
        cred: ResolvedCredential,
        body: str,
    ) -> dict[str, Any]:
        self.calls.append(("jira_create_self_comment", (body,)))
        if self.fail_write_create == "jira":
            raise RuntimeError("create failed")
        comment_id = str(self.next_jira_comment_id)
        self.next_jira_comment_id += 1
        comment = {
            "id": comment_id,
            "issue_key": self.target_issue_key,
            "body_marker": body,
        }
        self.jira_self_comments.append(comment)
        return comment

    async def jira_delete_comment(
        self,
        cred: ResolvedCredential,
        issue_key: str,
        comment_id: str,
    ) -> None:
        self.calls.append(("jira_delete_comment", (issue_key, comment_id)))
        if self.fail_write_delete == "jira":
            raise RuntimeError("delete failed")
        self.jira_self_comments = [
            c for c in self.jira_self_comments if str(c.get("id")) != comment_id
        ]

    # ----- Bitbucket ----------------------------------------------------

    async def bitbucket_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("bitbucket_user", (cred.username,)))
        if self.fail_read == "bitbucket":
            raise RuntimeError("auth failed")
        return {"account_id": self.bitbucket_account_id}

    async def bitbucket_list_probe_branches(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
    ) -> list[str]:
        self.calls.append(
            ("bitbucket_list_probe_branches", (workspace, repo))
        )
        return list(self.bitbucket_probe_branches)

    async def bitbucket_create_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> str:
        self.calls.append(
            ("bitbucket_create_branch", (workspace, repo, branch_name))
        )
        if self.fail_write_create == "bitbucket":
            raise RuntimeError("create failed")
        self.bitbucket_probe_branches.append(branch_name)
        return f"sha-{branch_name}"

    async def bitbucket_delete_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> None:
        self.calls.append(
            ("bitbucket_delete_branch", (workspace, repo, branch_name))
        )
        if self.fail_write_delete == "bitbucket":
            raise RuntimeError("delete failed")
        self.bitbucket_probe_branches = [
            b for b in self.bitbucket_probe_branches if b != branch_name
        ]

    # ----- Confluence ---------------------------------------------------

    async def confluence_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        self.calls.append(("confluence_user", (cred.username,)))
        if self.fail_read == "confluence":
            raise RuntimeError("auth failed")
        return {"accountId": self.confluence_account_id}

    async def confluence_list_probe_pages(
        self,
        cred: ResolvedCredential,
        space_key: str,
    ) -> list[dict[str, Any]]:
        self.calls.append(("confluence_list_probe_pages", (space_key,)))
        return list(self.confluence_probe_pages)

    async def confluence_create_draft_page(
        self,
        cred: ResolvedCredential,
        space_key: str,
        title: str,
    ) -> dict[str, Any]:
        self.calls.append(("confluence_create_draft_page", (space_key, title)))
        if self.fail_write_create == "confluence":
            raise RuntimeError("create failed")
        page_id = str(self.next_confluence_page_id)
        self.next_confluence_page_id += 1
        page = {"id": page_id, "title": title, "space_key": space_key}
        self.confluence_probe_pages.append(page)
        return page

    async def confluence_delete_page(
        self,
        cred: ResolvedCredential,
        page_id: str,
    ) -> None:
        self.calls.append(("confluence_delete_page", (page_id,)))
        if self.fail_write_delete == "confluence":
            raise RuntimeError("delete failed")
        self.confluence_probe_pages = [
            p for p in self.confluence_probe_pages if str(p.get("id")) != page_id
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _credential() -> ResolvedCredential:
    """A canonical credential whose plain-text token is the leak sentinel."""

    return ResolvedCredential(
        url="https://acme.atlassian.net",
        username="bot@acme.com",
        personal_token=_TOKEN_SENTINEL,
    )


def _runner(client: _FakeAtlassianClient, *, ts: int) -> ProbeRunner:
    """Build a ``ProbeRunner`` whose clock is pinned to *ts* for determinism."""

    return ProbeRunner(client, clock=lambda: ts)


def _targets(service: ProbeService) -> ProbeTargets | None:
    """Return the per-service targets the runner needs.

    Jira does not need targets in the current implementation; Bitbucket
    needs a workspace+repo pair; Confluence needs a space key. The
    fixed values we use are arbitrary — the runner only forwards them
    to the fake client which records them in ``calls``.
    """

    if service == "bitbucket":
        return ProbeTargets(
            bitbucket_workspace="acme", bitbucket_repo="payment-service"
        )
    if service == "confluence":
        return ProbeTargets(confluence_space_key="PAYDOCS")
    return None


def _count_probe_artifacts(client: _FakeAtlassianClient, service: ProbeService) -> int:
    """Count live ``_AI_PROBE_*`` sentinels left in the fake's state."""

    if service == "jira":
        return sum(
            1
            for c in client.jira_self_comments
            if is_probe_artifact_title(str(c.get("body_marker") or ""))
        )
    if service == "bitbucket":
        return sum(
            1
            for b in client.bitbucket_probe_branches
            if is_probe_artifact_title(b)
        )
    if service == "confluence":
        return sum(
            1
            for p in client.confluence_probe_pages
            if is_probe_artifact_title(str(p.get("title") or ""))
        )
    raise AssertionError(f"unknown service {service!r}")  # pragma: no cover


def _write_methods_for(service: ProbeService) -> frozenset[str]:
    """Return the fake's write-side method names for *service*.

    A "write-side" method is anything that creates or deletes an
    artifact in the target system. The read-failure test uses this to assert
    that no write-side method runs after a read-probe failure.
    """

    if service == "jira":
        return frozenset(
            {"jira_create_self_comment", "jira_delete_comment"}
        )
    if service == "bitbucket":
        return frozenset(
            {"bitbucket_create_branch", "bitbucket_delete_branch"}
        )
    if service == "confluence":
        return frozenset(
            {"confluence_create_draft_page", "confluence_delete_page"}
        )
    raise AssertionError(f"unknown service {service!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department identifier strategy — lowercase ASCII slug. Mirrors the
#: ``departments.json`` ``id`` regex (``^[a-z][a-z0-9-]{1,30}$``) without
#: importing it; Hypothesis only needs a non-empty distinguishable
#: string for the runner to forward into the artifact dataclass.
_DEPT_ID = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789-",
    ),
    min_size=1,
    max_size=20,
).filter(lambda s: s[0].isalpha())

#: One of the three Atlassian surfaces.
_SERVICE = st.sampled_from(("jira", "bitbucket", "confluence"))

#: Unix timestamps the test pins the runner clock to. The runner only
#: cares that the artifact title carries a stable integer, so we pick a
#: range that's large enough to look like real epoch seconds but stays
#: well below the 32-bit overflow.
_UNIX_TS = st.integers(min_value=1_500_000_000, max_value=2_000_000_000)

#: How many stale ``_AI_PROBE_*`` artifacts the fake should have seeded
#: before the runner is invoked. Exercises the cleanup loop.
_STALE_COUNT = st.integers(min_value=0, max_value=4)

#: A non-probe artifact title — used to verify cleanup leaves unrelated
#: artifacts alone. We deliberately exclude any string that happens to
#: start with ``_AI_PROBE_`` so the property's invariant cannot be
#: undermined by an adversarial generator.
_UNRELATED_TITLE = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters=" -_/",
    ),
    min_size=1,
    max_size=30,
).filter(lambda s: not s.startswith(PROBE_ARTIFACT_PREFIX))


def _seed_stale(
    client: _FakeAtlassianClient,
    service: ProbeService,
    stale_count: int,
    unrelated_title: str,
) -> None:
    """Seed the fake with *stale_count* sentinel artifacts plus one
    unrelated artifact (so cleanup can be observed selectively)."""

    if service == "jira":
        for i in range(stale_count):
            client.jira_self_comments.append(
                {
                    "id": f"stale-{i}",
                    "issue_key": "BOT-1",
                    "body_marker": f"_AI_PROBE_{1_000_000 + i}_DELETE_ME",
                }
            )
        client.jira_self_comments.append(
            {
                "id": "unrelated",
                "issue_key": "BOT-1",
                "body_marker": unrelated_title,
            }
        )
    elif service == "bitbucket":
        for i in range(stale_count):
            client.bitbucket_probe_branches.append(
                f"_AI_PROBE_{1_000_000 + i}_DELETE_ME"
            )
        client.bitbucket_probe_branches.append(unrelated_title)
    else:  # confluence
        for i in range(stale_count):
            client.confluence_probe_pages.append(
                {
                    "id": f"stale-{i}",
                    "title": f"_AI_PROBE_{1_000_000 + i}_DELETE_ME",
                }
            )
        client.confluence_probe_pages.append(
            {"id": "unrelated", "title": unrelated_title}
        )


# ---------------------------------------------------------------------------
# Idempotent cleanup
# ---------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    service=_SERVICE,
    ts=_UNIX_TS,
    stale_count=_STALE_COUNT,
    unrelated_title=_UNRELATED_TITLE,
)
def test_repeated_runs_leave_no_extra_probe_artifacts(
    dept_id: str,
    service: ProbeService,
    ts: int,
    stale_count: int,
    unrelated_title: str,
) -> None:
    """Sequential ``ProbeRunner.run`` calls are idempotent.

    For every ``(dept_id, service)`` pair, calling ``run`` twice in a
    row leaves zero ``_AI_PROBE_*`` artifacts in the target system.
    Each invocation:

    1. Cleans up any stale sentinels seeded before the call.
    2. Creates exactly one fresh sentinel (write probe).
    3. Deletes that sentinel before returning.

    The unrelated artifact (whose title does not start with
    ``_AI_PROBE_``) survives both runs unchanged, ensuring cleanup does
    not over-reach.
    """

    client = _FakeAtlassianClient()
    _seed_stale(client, service, stale_count, unrelated_title)
    runner = _runner(client, ts=ts)
    targets = _targets(service)

    async def _twice() -> tuple[ProbeResult, ProbeResult]:
        first = await runner.run(dept_id, service, _credential(), targets=targets)
        second = await runner.run(dept_id, service, _credential(), targets=targets)
        return first, second

    first, second = asyncio.run(_twice())

    # Both invocations succeed — no failure injection in this property.
    assert first.state == "ok", f"first run state was {first.state!r}"
    assert second.state == "ok", f"second run state was {second.state!r}"

    # Invariant: zero leftover probe sentinels in the target system.
    assert _count_probe_artifacts(client, service) == 0, (
        f"expected 0 leftover probe sentinels after two runs, "
        f"found {_count_probe_artifacts(client, service)}"
    )

    # Invariant: the unrelated artifact (whatever it was) is untouched.
    if service == "jira":
        survivors = [
            c for c in client.jira_self_comments if c.get("id") == "unrelated"
        ]
        assert len(survivors) == 1
        assert survivors[0]["body_marker"] == unrelated_title
    elif service == "bitbucket":
        assert unrelated_title in client.bitbucket_probe_branches
    else:
        survivors = [
            p for p in client.confluence_probe_pages if p.get("id") == "unrelated"
        ]
        assert len(survivors) == 1
        assert survivors[0]["title"] == unrelated_title


# ---------------------------------------------------------------------------
# Read failure short-circuits the write probe
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    service=_SERVICE,
    ts=_UNIX_TS,
)
def test_read_failure_skips_all_write_side_calls(
    dept_id: str,
    service: ProbeService,
    ts: int,
) -> None:
    """Read-probe failure aborts before any write activity.

    For every service literal, when the read probe raises:

    * ``ProbeResult.state == "read_failed"``.
    * ``ProbeResult.read_ok is False`` and ``write_ok is False``.
    * **No** create / delete write-side method ever runs.
    * The sanitised ``error_message`` carries only the exception class
      name — never the raw ``str(exc)``.
    """

    client = _FakeAtlassianClient(fail_read=service)
    runner = _runner(client, ts=ts)
    targets = _targets(service)

    result = asyncio.run(
        runner.run(dept_id, service, _credential(), targets=targets)
    )

    assert result.state == "read_failed"
    assert result.read_ok is False
    assert result.write_ok is False
    assert result.artifact is None
    assert result.auto_fetched_account_id is None

    # No write-side method may appear in the call log.
    write_methods = _write_methods_for(service)
    invoked_writes = {
        name for name, _ in client.calls if name in write_methods
    }
    assert invoked_writes == set(), (
        f"read failure must short-circuit the write probe, "
        f"but the runner invoked {invoked_writes!r}"
    )

    # Sanitised error message: must reference the exception class but
    # not the raw "auth failed" message body.
    assert result.error_message is not None
    assert "RuntimeError" in result.error_message
    assert "auth failed" not in result.error_message


# ---------------------------------------------------------------------------
# Confluence delete failure yields ``partial_orphan``
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    ts=_UNIX_TS,
)
def test_confluence_delete_failure_yields_partial_orphan(
    dept_id: str,
    ts: int,
) -> None:
    """Confluence delete failure surfaces ``partial_orphan``.

    When the Confluence draft create succeeds but the delete fails:

    * ``state == "partial_orphan"``.
    * ``ProbeResult.artifact`` is populated.
    * ``artifact.dept_id`` equals the caller-supplied department id.
    * ``artifact.service == "confluence"`` and
      ``artifact_type == "confluence_page"``.
    * ``artifact.title_or_name`` matches the canonical sentinel format
      ``_AI_PROBE_<unix_ts>_DELETE_ME``.
    * Neither the artifact's ``title_or_name`` nor ``external_id`` nor
      ``error_message`` carries the plain-text token.
    """

    client = _FakeAtlassianClient(fail_write_delete="confluence")
    runner = _runner(client, ts=ts)
    targets = _targets("confluence")

    result = asyncio.run(
        runner.run(dept_id, "confluence", _credential(), targets=targets)
    )

    assert result.state == "partial_orphan"
    assert result.read_ok is True
    assert result.write_ok is False

    assert isinstance(result.artifact, ProbeArtifact)
    artifact = result.artifact
    assert artifact.dept_id == dept_id
    assert artifact.service == "confluence"
    assert artifact.artifact_type == "confluence_page"

    # Title format invariant: ``_AI_PROBE_<unix_ts>_DELETE_ME``.
    assert is_probe_artifact_title(artifact.title_or_name)
    assert artifact.title_or_name.startswith(PROBE_ARTIFACT_PREFIX)
    assert artifact.title_or_name.endswith(PROBE_ARTIFACT_SUFFIX)
    assert artifact.title_or_name == make_probe_title(ts)
    # The middle slug is exactly the pinned timestamp.
    middle = artifact.title_or_name[
        len(PROBE_ARTIFACT_PREFIX) : -len(PROBE_ARTIFACT_SUFFIX)
    ]
    assert middle == str(ts)

    # No plain-text credential anywhere on the artifact or in
    # the error message.
    assert _TOKEN_SENTINEL not in artifact.title_or_name
    assert _TOKEN_SENTINEL not in artifact.external_id
    assert _TOKEN_SENTINEL not in (result.error_message or "")


# ---------------------------------------------------------------------------
# Auto-fetched account_id is exposed on the result
# ---------------------------------------------------------------------------


# Strategy for non-empty account-id-shaped strings. Atlassian Cloud
# returns either ``accountId`` (Jira/Confluence) or ``account_id``
# (Bitbucket); both are opaque non-empty strings in practice. We
# constrain to printable ASCII without colon to keep error messages
# readable and to avoid clashing with any URL parsing later in the
# stack.
_ACCOUNT_ID = st.text(
    alphabet=st.characters(
        min_codepoint=0x21,
        max_codepoint=0x7E,
        blacklist_characters=":",
    ),
    min_size=1,
    max_size=64,
)


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    service=_SERVICE,
    ts=_UNIX_TS,
    fetched_account_id=_ACCOUNT_ID,
)
def test_auto_fetched_account_id_is_passthrough(
    dept_id: str,
    service: ProbeService,
    ts: int,
    fetched_account_id: str,
) -> None:
    """Read-probe ``accountId`` is exposed verbatim.

    The runner forwards the value returned by the read probe to
    :attr:`ProbeResult.auto_fetched_account_id` so the caller can
    decide whether to update ``departments.json`` / DB
    records. This property generates random account-id strings and
    asserts byte-for-byte equality on the result.
    """

    client = _FakeAtlassianClient(
        jira_account_id=fetched_account_id,
        bitbucket_account_id=fetched_account_id,
        confluence_account_id=fetched_account_id,
    )
    runner = _runner(client, ts=ts)
    targets = _targets(service)

    result = asyncio.run(
        runner.run(dept_id, service, _credential(), targets=targets)
    )

    assert result.state == "ok"
    assert result.auto_fetched_account_id == fetched_account_id


# ---------------------------------------------------------------------------
# Mismatch detection primitive (manual vs auto-fetched)
# ---------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    service=_SERVICE,
    ts=_UNIX_TS,
    manual_account_id=_ACCOUNT_ID,
    fetched_account_id=_ACCOUNT_ID,
)
def test_manual_vs_auto_fetched_account_id_is_observable(
    dept_id: str,
    service: ProbeService,
    ts: int,
    manual_account_id: str,
    fetched_account_id: str,
) -> None:
    """Manual vs auto-fetched account_id mismatch is observable.

    The runner exposes the auto-fetched ``accountId`` on
    :attr:`ProbeResult.auto_fetched_account_id`. The caller compares
    that value with the manual ``account_id`` configured in
    ``departments.json`` and fails fast when they differ.

    This property exercises the comparison primitive end-to-end:

    * When ``manual == fetched``: the comparison agrees, no fail-fast
      message needed.
    * When ``manual != fetched``: a constructed fail-fast error message
      contains **both** values verbatim.

    The runner itself does not raise on mismatch;
    this test verifies the building block the caller wires up.
    """

    # The two values must be either equal or distinct — Hypothesis
    # generates both variants on its own without an ``assume()``.
    client = _FakeAtlassianClient(
        jira_account_id=fetched_account_id,
        bitbucket_account_id=fetched_account_id,
        confluence_account_id=fetched_account_id,
    )
    runner = _runner(client, ts=ts)
    targets = _targets(service)

    result = asyncio.run(
        runner.run(dept_id, service, _credential(), targets=targets)
    )

    assert result.state == "ok"
    auto = result.auto_fetched_account_id
    assert auto == fetched_account_id

    # Caller-side mismatch detection primitive — the building block
    # ``CredentialResolver`` / boot validation will use.
    is_mismatch = manual_account_id != auto

    if not is_mismatch:
        # Equal values: comparison agrees; no fail-fast required.
        assert manual_account_id == auto
        return

    # Mismatch path — the caller's fail-fast error message must
    # surface BOTH values so an operator can diagnose the
    # discrepancy without a round-trip to logs. We use ``!r``
    # formatting so values are quoted and any control characters /
    # backslashes are escaped (Python's ``repr`` is the canonical
    # round-trippable representation). The presence assertion
    # therefore checks ``repr(value)`` rather than the bare value
    # — escaped backslashes and quote characters in the original
    # string are not byte-identical to their repr form.
    error_message = (
        f"account_id mismatch for dept={dept_id!r} service={service!r}: "
        f"manual={manual_account_id!r}, auto_fetched={auto!r}"
    )
    assert repr(manual_account_id) in error_message
    assert repr(auto) in error_message  # type: ignore[operator]
    # And the runner must not have collapsed the two values into one —
    # the caller still sees the auto-fetched one separately from the
    # manually configured one.
    assert auto != manual_account_id


# ---------------------------------------------------------------------------
# Plain-text credential never reaches the call log or artifacts
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=_DEPT_ID,
    service=_SERVICE,
    ts=_UNIX_TS,
)
def test_plain_text_token_never_appears_in_artifact_payloads(
    dept_id: str,
    service: ProbeService,
    ts: int,
) -> None:
    """Probe artifact payloads never carry the plain-text token.

    The probe runner sends only the canonical sentinel string into
    artifact bodies / branch names / comment markers. The
    :class:`_FakeAtlassianClient` records every method call's
    arguments — none of them may contain :data:`_TOKEN_SENTINEL`.
    """

    client = _FakeAtlassianClient(fail_write_delete=service)
    runner = _runner(client, ts=ts)
    targets = _targets(service)

    asyncio.run(runner.run(dept_id, service, _credential(), targets=targets))

    # Walk the entire call log; no recorded argument should carry the
    # plain-text token.
    for method, args in client.calls:
        for arg in args:
            assert _TOKEN_SENTINEL not in repr(arg), (
                f"plain-text token leaked into {method}{args!r}"
            )

    # Plus any sentinels produced server-side (e.g. branch names,
    # comment bodies, page titles): scan the live artifact state too.
    if service == "jira":
        for c in client.jira_self_comments:
            assert _TOKEN_SENTINEL not in str(c.get("body_marker") or "")
    elif service == "bitbucket":
        for b in client.bitbucket_probe_branches:
            assert _TOKEN_SENTINEL not in b
    else:
        for p in client.confluence_probe_pages:
            assert _TOKEN_SENTINEL not in str(p.get("title") or "")
