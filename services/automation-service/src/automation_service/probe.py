"""Probe runner — read / write credential validation for department bots.

Implements the ``ProbeRunner.run(dept_id, service, cred) -> ProbeResult``
API for validating department bot credentials.

What this module owns
---------------------

* A **read probe** for each Atlassian surface: Jira
  ``GET /rest/api/3/myself``, Bitbucket ``GET /2.0/user``, Confluence
  ``GET /wiki/rest/api/user/current``. The read probe must succeed
  before the write probe runs; if it fails, the credential is rejected
  and ``ProbeResult.state == "read_failed"`` is returned.
* A **write probe** for each surface — Confluence draft create+delete,
  Bitbucket temporary branch create+delete, Jira self-comment on a
  bot-owned issue. Every artifact uses the canonical sentinel
  title / branch name format ``_AI_PROBE_<unix_ts>_DELETE_ME``
  (Invariant 10).
* **Idempotent cleanup**: before every probe call the runner searches
  the target system for orphan ``_AI_PROBE_*`` artifacts and deletes
  them. Repeated invocations therefore leave no extra residue
  by the cleanup pass.
* **Sensitive data hygiene**: the probe artifact body / branch
  description never contains plain-text credentials, tokens or
  passwords. The artifact carries only the sentinel marker
  string defined in :data:`PROBE_ARTIFACT_PREFIX`.

Out-of-scope here
-----------------

* ``account_id`` auto-fetch and manual / fetched mismatch fail-fast
  is handled by startup validation. The read probe **does** capture an
  ``auto_fetched_account_id`` for the caller, but the assertion logic
  is not enforced here.
* ``partial_orphan`` row insertion into ``automation.probe_artifacts``
  is handled by the cleanup persistence layer; the dataclass shape is provided
  (:class:`ProbeArtifact`) and ``ProbeResult.state`` already exposes
  ``"partial_orphan"`` as a possible value so callers can branch
  today.

Atlassian client abstraction
----------------------------

Every outbound Atlassian HTTP call must go through the
``atlassian_unified`` MCP service. The probe runner
does **not** issue raw HTTP itself; it depends on a thin
:class:`AtlassianProbeClient` :class:`~typing.Protocol` that the
production wiring backs with an MCP-routed implementation.
Tests inject an in-memory fake satisfying the protocol — the suite at
``tests/unit/test_probe.py`` and the property test at
``platform/tests/property/test_probe_runner.py`` exercise
the runner this way.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel prefix every probe artifact (Confluence draft title,
#: Bitbucket branch name, Jira comment marker) carries. Required by
#: listing this prefix in a target system is
#: the canonical way to find probe leftovers and clean them up.
PROBE_ARTIFACT_PREFIX: Final[str] = "_AI_PROBE_"

#: Suffix appended to the prefix + timestamp. The full pattern is
#: ``_AI_PROBE_<unix_ts>_DELETE_ME`` and signals to humans that the
#: artifact is safe to delete unattended.
PROBE_ARTIFACT_SUFFIX: Final[str] = "_DELETE_ME"

#: Atlassian service identifiers. Mirrors the
#: ``probe_artifacts.service`` ``CHECK`` constraint declared in
#: ``infra/postgres/init/10_automation.sql``.
ProbeService = Literal["jira", "bitbucket", "confluence"]

#: Possible artifact types per service. Mirrors the
#: ``probe_artifacts.artifact_type`` ``CHECK`` constraint.
ProbeArtifactType = Literal["confluence_page", "bitbucket_branch", "jira_comment"]

#: Terminal states surfaced on :class:`ProbeResult`.
#:
#: * ``ok`` — both read and write probes succeeded; no residue left.
#: * ``read_failed`` — read probe failed; write probe was skipped.
#: * ``write_failed`` — read probe succeeded but the write probe
#:   could not create / delete the artifact (clean failure, no
#:   leftover).
#: * ``partial_orphan`` — write probe created an artifact but failed
#:   to delete it. The artifact is captured on
#:   :attr:`ProbeResult.artifact` so the caller can persist
#:   it for admin cleanup.
ProbeState = Literal["ok", "read_failed", "write_failed", "partial_orphan"]


def make_probe_title(now_unix_ts: int | None = None) -> str:
    """Return the canonical probe sentinel string.

    Format: ``_AI_PROBE_<unix_ts>_DELETE_ME``. The timestamp is
    seconds-since-epoch UTC (``time.time()`` rounded to ``int``).

    Args:
        now_unix_ts: Optional fixed timestamp — useful in tests so the
            generated title is deterministic. Defaults to the current
            wall-clock value.
    """

    ts = int(time.time()) if now_unix_ts is None else int(now_unix_ts)
    return f"{PROBE_ARTIFACT_PREFIX}{ts}{PROBE_ARTIFACT_SUFFIX}"


def is_probe_artifact_title(title: str) -> bool:
    """Return whether *title* matches the probe sentinel format.

    The check is intentionally loose: any string that starts with
    :data:`PROBE_ARTIFACT_PREFIX` is considered a probe artifact for
    cleanup purposes, even if the timestamp / suffix portion is
    malformed. This keeps cleanup robust against historical formats
    or human-edited variants.
    """

    return isinstance(title, str) and title.startswith(PROBE_ARTIFACT_PREFIX)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """Resolved credential triple passed into the probe runner.

    The runner never reads the raw value from Vault directly — that
    work is done by ``CredentialResolver``. The
    runner accepts an opaque ``ResolvedCredential`` so the same
    surface can probe department org-default and per-user session
    credentials uniformly (Q6/Q7).

    Attributes:
        url: Atlassian site URL (``https://acme.atlassian.net``,
            ``https://bitbucket.org``, ...).
        username: Account email or username — used for Basic auth and
            for tagging the write artifact (Jira self-comment author
            check).
        personal_token: API token / app password. **Must not** appear
            in any probe artifact body or log line.
    """

    url: str
    username: str
    personal_token: str


@dataclass(frozen=True, slots=True)
class ProbeArtifact:
    """A leftover probe artifact captured for admin cleanup.

    The shape mirrors the ``automation.probe_artifacts`` table column
    set so callers can persist instances directly via ``db_shared``.

    Attributes:
        dept_id: Department that owns the bot the probe ran against.
        service: One of ``"jira"``, ``"bitbucket"`` or
            ``"confluence"``.
        artifact_type: Concrete artifact subtype — see
            :data:`ProbeArtifactType`.
        external_id: Identifier returned by the target system (e.g.
            Confluence page id, Bitbucket branch ref, Jira comment id)
            used to drive the cleanup action.
        title_or_name: The literal sentinel string. Mirrors the
            ``title_or_name`` column on the table.
    """

    dept_id: str
    service: ProbeService
    artifact_type: ProbeArtifactType
    external_id: str
    title_or_name: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a single ``ProbeRunner.run(...)`` invocation.

    Attributes:
        read_ok: ``True`` iff the read probe succeeded.
        write_ok: ``True`` iff the write probe round-tripped
            (create + delete). ``False`` whenever the read probe
            failed (the write probe is skipped).
        auto_fetched_account_id: ``account_id`` returned by the read
            probe. ``None`` when the target system did not surface one
            or the read probe failed. Startup validation consumes this field to
            decide whether to update ``departments.json`` /
            ``automation.departments``.
        artifact: Populated only when ``state == "partial_orphan"`` —
            the leftover artifact the caller should record
            in ``automation.probe_artifacts``.
        state: Terminal state (see :data:`ProbeState`).
        error_message: Human-readable detail when ``state`` is anything
            other than ``"ok"``. Sanitised — never contains tokens or
            passwords.
    """

    read_ok: bool
    write_ok: bool
    auto_fetched_account_id: str | None
    artifact: ProbeArtifact | None
    state: ProbeState
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Atlassian probe client protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AtlassianProbeClient(Protocol):
    """Minimum surface the probe runner needs from the MCP wrapper.

    The protocol is intentionally narrow — only the calls required to
    implement read and write probes. The production implementation
    routes every call through the ``atlassian_unified`` MCP service; tests
    inject a fake whose method signatures match this protocol.

    All methods are ``async`` so the runner can be wired into the
    FastAPI request lifecycle without blocking the event loop.
    """

    # ----- Jira ----------------------------------------------------------

    async def jira_myself(self, cred: ResolvedCredential) -> dict[str, Any]:
        """``GET /rest/api/3/myself`` — returns the authenticated user.

        Used by the read probe. The implementation must surface
        the response JSON unchanged so the probe runner can extract
        ``accountId`` for auto-fetch.
        """
        ...

    async def jira_search_self_comments(
        self,
        cred: ResolvedCredential,
        author_account_id: str,
    ) -> list[dict[str, Any]]:
        """Find Jira comments authored by *author_account_id* whose body
        starts with the probe sentinel prefix.

        Used during idempotent cleanup before issuing a fresh
        write probe. Each returned dict must carry at least the
        comment ``id`` and the parent ``issue_key`` so the runner can
        delete the comment with :meth:`jira_delete_comment`.
        """
        ...

    async def jira_create_self_comment(
        self,
        cred: ResolvedCredential,
        body: str,
    ) -> dict[str, Any]:
        """Create a comment on a bot-owned issue.

        Used by the Jira write probe. The implementation is
        responsible for picking a target issue the bot owns (typically
        a long-lived dedicated probe issue per departman). The
        returned dict must carry the new comment's ``id`` and the
        parent ``issue_key`` so :meth:`jira_delete_comment` can
        round-trip cleanup.
        """
        ...

    async def jira_delete_comment(
        self,
        cred: ResolvedCredential,
        issue_key: str,
        comment_id: str,
    ) -> None:
        """Delete the comment identified by ``issue_key``+``comment_id``."""
        ...

    # ----- Bitbucket -----------------------------------------------------

    async def bitbucket_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        """``GET /2.0/user`` — returns the authenticated user."""
        ...

    async def bitbucket_list_probe_branches(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
    ) -> list[str]:
        """Return branch names starting with the probe sentinel prefix.

        Used during idempotent cleanup before each write probe.
        """
        ...

    async def bitbucket_create_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> str:
        """Create *branch_name* off the repo's default branch.

        Returns the new branch's reference identifier (typically the
        commit hash of the branch tip).
        """
        ...

    async def bitbucket_delete_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> None:
        """Delete *branch_name* from the repo."""
        ...

    # ----- Confluence ----------------------------------------------------

    async def confluence_user(self, cred: ResolvedCredential) -> dict[str, Any]:
        """Confluence "current user" read probe.

        Returns the response JSON; the runner extracts ``accountId``
        for auto-fetch.
        """
        ...

    async def confluence_list_probe_pages(
        self,
        cred: ResolvedCredential,
        space_key: str,
    ) -> list[dict[str, Any]]:
        """Return draft pages in *space_key* whose title matches the
        probe sentinel prefix."""
        ...

    async def confluence_create_draft_page(
        self,
        cred: ResolvedCredential,
        space_key: str,
        title: str,
    ) -> dict[str, Any]:
        """Create a draft Confluence page named *title* in *space_key*.

        The body is a single sentinel marker line; the runner never
        sends credentials, tokens or PII.
        """
        ...

    async def confluence_delete_page(
        self,
        cred: ResolvedCredential,
        page_id: str,
    ) -> None:
        """Delete the Confluence page identified by *page_id*."""
        ...


# ---------------------------------------------------------------------------
# Bitbucket / Confluence probe targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeTargets:
    """Per-department targets the runner needs to know about.

    The probe runner is service-agnostic but Bitbucket and Confluence
    write probes need a workspace/repo and a space key respectively.
    These are sourced from ``departments.json`` at the call site and
    handed to the runner alongside the credential.

    Attributes:
        bitbucket_workspace: Workspace slug used for the Bitbucket
            branch round-trip. Required when probing Bitbucket; may be
            ``None`` for departments that do not use Bitbucket.
        bitbucket_repo: Repository slug for the branch round-trip.
            Required when probing Bitbucket.
        confluence_space_key: Space key for the Confluence draft
            round-trip. Required when probing Confluence.
    """

    bitbucket_workspace: str | None = None
    bitbucket_repo: str | None = None
    confluence_space_key: str | None = None


# ---------------------------------------------------------------------------
# ProbeRunner
# ---------------------------------------------------------------------------


class ProbeRunner:
    """Run read + write probes against a single Atlassian surface.

    Args:
        client: An :class:`AtlassianProbeClient` implementation
            (typically a thin wrapper over the ``atlassian_unified``
            MCP). Tests inject an in-memory fake.
        clock: Callable returning the current Unix timestamp in
            seconds. Defaults to :func:`time.time`. Overridable so the
            generated artifact titles are deterministic in unit tests.

    The runner is **stateless** between calls — every invocation does
    its own idempotent cleanup before issuing the new write probe so
    repeated calls leave no extra residue.
    """

    def __init__(
        self,
        client: AtlassianProbeClient,
        *,
        clock: Any = None,
    ) -> None:
        self._client = client
        # ``time.time`` returns ``float``; we coerce to ``int`` inside
        # ``make_probe_title`` so callers see a stable
        # seconds-since-epoch sentinel.
        self._clock = clock if clock is not None else time.time

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        dept_id: str,
        service: ProbeService,
        cred: ResolvedCredential,
        *,
        targets: ProbeTargets | None = None,
    ) -> ProbeResult:
        """Run read + write probes for *(dept_id, service)*.

        The flow is:

        1. **Idempotent cleanup** — list and delete any existing
           ``_AI_PROBE_*`` artifacts on the target service.
        2. **Read probe** — verify the credential can authenticate
           and capture ``account_id`` for auto-fetch.
        3. **Write probe** — create + delete a sentinel artifact
           On delete failure return ``state="partial_orphan"``
           with the artifact attached.

        Args:
            dept_id: Department identifier.
            service: One of ``"jira"``, ``"bitbucket"``, ``"confluence"``.
            cred: Resolved credential triple. The plain-text
                ``personal_token`` is **never** echoed into artifacts
                or logs.
            targets: Per-department target metadata (Bitbucket
                workspace+repo, Confluence space). Required for
                ``service == "bitbucket"`` and ``service == "confluence"``.

        Returns:
            A :class:`ProbeResult` describing the terminal state. The
            method never raises for "expected" probe failures — they
            are surfaced via the ``state`` field so callers can branch
            on a single switch.
        """

        if service == "jira":
            return await self._run_jira(dept_id, cred)
        if service == "bitbucket":
            return await self._run_bitbucket(dept_id, cred, targets)
        if service == "confluence":
            return await self._run_confluence(dept_id, cred, targets)
        # Defensive — Literal narrowing should make this unreachable.
        raise ValueError(  # pragma: no cover - defensive only
            f"unsupported probe service {service!r}"
        )

    # ------------------------------------------------------------------
    # Jira flow
    # ------------------------------------------------------------------

    async def _run_jira(
        self,
        dept_id: str,
        cred: ResolvedCredential,
    ) -> ProbeResult:
        # ----- 1. Read probe -------------------------------------------
        try:
            myself = await self._client.jira_myself(cred)
        except Exception as exc:  # noqa: BLE001 — surface as terminal state
            return _read_failed("jira", exc)

        account_id = _extract_account_id(myself)

        # ----- 2. Idempotent cleanup -----------------------------------
        # Cleanup runs *after* the read probe succeeds so a totally
        # broken credential does not waste time listing comments.
        if account_id:
            try:
                stale = await self._client.jira_search_self_comments(
                    cred, account_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "probe.cleanup.jira_search_failed dept=%s err=%s",
                    dept_id, type(exc).__name__,
                )
                stale = []
            for comment in stale:
                title = comment.get("body_marker") or comment.get("body") or ""
                if not is_probe_artifact_title(title):
                    continue
                try:
                    await self._client.jira_delete_comment(
                        cred,
                        issue_key=str(comment["issue_key"]),
                        comment_id=str(comment["id"]),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "probe.cleanup.jira_delete_failed dept=%s id=%s err=%s",
                        dept_id, comment.get("id"), type(exc).__name__,
                    )

        # ----- 3. Write probe ------------------------------------------
        sentinel = make_probe_title(int(self._clock()))
        try:
            created = await self._client.jira_create_self_comment(
                cred, body=sentinel
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=None,
                state="write_failed",
                error_message=f"jira write probe create failed: {type(exc).__name__}",
            )

        comment_id = str(created["id"])
        issue_key = str(created["issue_key"])
        try:
            await self._client.jira_delete_comment(
                cred, issue_key=issue_key, comment_id=comment_id
            )
        except Exception as exc:  # noqa: BLE001
            artifact = ProbeArtifact(
                dept_id=dept_id,
                service="jira",
                artifact_type="jira_comment",
                external_id=f"{issue_key}/{comment_id}",
                title_or_name=sentinel,
            )
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=artifact,
                state="partial_orphan",
                error_message=(
                    f"jira write probe delete failed: {type(exc).__name__}"
                ),
            )

        return ProbeResult(
            read_ok=True,
            write_ok=True,
            auto_fetched_account_id=account_id,
            artifact=None,
            state="ok",
        )

    # ------------------------------------------------------------------
    # Bitbucket flow
    # ------------------------------------------------------------------

    async def _run_bitbucket(
        self,
        dept_id: str,
        cred: ResolvedCredential,
        targets: ProbeTargets | None,
    ) -> ProbeResult:
        if targets is None or not targets.bitbucket_workspace or not targets.bitbucket_repo:
            return ProbeResult(
                read_ok=False,
                write_ok=False,
                auto_fetched_account_id=None,
                artifact=None,
                state="read_failed",
                error_message=(
                    "bitbucket probe requires targets.bitbucket_workspace "
                    "and targets.bitbucket_repo"
                ),
            )

        # ----- 1. Read probe -------------------------------------------
        try:
            user = await self._client.bitbucket_user(cred)
        except Exception as exc:  # noqa: BLE001
            return _read_failed("bitbucket", exc)
        account_id = _extract_account_id(user)

        workspace = targets.bitbucket_workspace
        repo = targets.bitbucket_repo

        # ----- 2. Idempotent cleanup -----------------------------------
        try:
            stale_branches = await self._client.bitbucket_list_probe_branches(
                cred, workspace=workspace, repo=repo
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "probe.cleanup.bitbucket_list_failed dept=%s err=%s",
                dept_id, type(exc).__name__,
            )
            stale_branches = []
        for branch_name in stale_branches:
            if not is_probe_artifact_title(branch_name):
                continue
            try:
                await self._client.bitbucket_delete_branch(
                    cred, workspace=workspace, repo=repo, branch_name=branch_name
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "probe.cleanup.bitbucket_delete_failed dept=%s "
                    "branch=%s err=%s",
                    dept_id, branch_name, type(exc).__name__,
                )

        # ----- 3. Write probe ------------------------------------------
        sentinel = make_probe_title(int(self._clock()))
        try:
            await self._client.bitbucket_create_branch(
                cred, workspace=workspace, repo=repo, branch_name=sentinel
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=None,
                state="write_failed",
                error_message=(
                    f"bitbucket write probe create failed: {type(exc).__name__}"
                ),
            )

        try:
            await self._client.bitbucket_delete_branch(
                cred, workspace=workspace, repo=repo, branch_name=sentinel
            )
        except Exception as exc:  # noqa: BLE001
            artifact = ProbeArtifact(
                dept_id=dept_id,
                service="bitbucket",
                artifact_type="bitbucket_branch",
                external_id=f"{workspace}/{repo}@{sentinel}",
                title_or_name=sentinel,
            )
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=artifact,
                state="partial_orphan",
                error_message=(
                    f"bitbucket write probe delete failed: {type(exc).__name__}"
                ),
            )

        return ProbeResult(
            read_ok=True,
            write_ok=True,
            auto_fetched_account_id=account_id,
            artifact=None,
            state="ok",
        )

    # ------------------------------------------------------------------
    # Confluence flow
    # ------------------------------------------------------------------

    async def _run_confluence(
        self,
        dept_id: str,
        cred: ResolvedCredential,
        targets: ProbeTargets | None,
    ) -> ProbeResult:
        if targets is None or not targets.confluence_space_key:
            return ProbeResult(
                read_ok=False,
                write_ok=False,
                auto_fetched_account_id=None,
                artifact=None,
                state="read_failed",
                error_message=(
                    "confluence probe requires targets.confluence_space_key"
                ),
            )

        # ----- 1. Read probe -------------------------------------------
        try:
            user = await self._client.confluence_user(cred)
        except Exception as exc:  # noqa: BLE001
            return _read_failed("confluence", exc)
        account_id = _extract_account_id(user)

        space_key = targets.confluence_space_key

        # ----- 2. Idempotent cleanup -----------------------------------
        try:
            stale_pages = await self._client.confluence_list_probe_pages(
                cred, space_key=space_key
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "probe.cleanup.confluence_list_failed dept=%s err=%s",
                dept_id, type(exc).__name__,
            )
            stale_pages = []
        for page in stale_pages:
            title = str(page.get("title") or "")
            if not is_probe_artifact_title(title):
                continue
            try:
                await self._client.confluence_delete_page(
                    cred, page_id=str(page["id"])
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "probe.cleanup.confluence_delete_failed dept=%s "
                    "page=%s err=%s",
                    dept_id, page.get("id"), type(exc).__name__,
                )

        # ----- 3. Write probe ------------------------------------------
        sentinel = make_probe_title(int(self._clock()))
        try:
            created = await self._client.confluence_create_draft_page(
                cred, space_key=space_key, title=sentinel
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=None,
                state="write_failed",
                error_message=(
                    f"confluence write probe create failed: {type(exc).__name__}"
                ),
            )

        page_id = str(created["id"])
        try:
            await self._client.confluence_delete_page(cred, page_id=page_id)
        except Exception as exc:  # noqa: BLE001
            # Confluence partial-orphan is the canonical example of
            # Surface the artifact so callers can persist it for admin cleanup.
            artifact = ProbeArtifact(
                dept_id=dept_id,
                service="confluence",
                artifact_type="confluence_page",
                external_id=page_id,
                title_or_name=sentinel,
            )
            return ProbeResult(
                read_ok=True,
                write_ok=False,
                auto_fetched_account_id=account_id,
                artifact=artifact,
                state="partial_orphan",
                error_message=(
                    "confluence write probe delete failed: "
                    f"{type(exc).__name__}"
                ),
            )

        return ProbeResult(
            read_ok=True,
            write_ok=True,
            auto_fetched_account_id=account_id,
            artifact=None,
            state="ok",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_failed(service: str, exc: BaseException) -> ProbeResult:
    """Build a ``read_failed`` :class:`ProbeResult` with a sanitised message.

    The error message includes only the exception class name — never
    the raw ``str(exc)`` — to avoid accidentally leaking
    ``Authorization: Basic ...`` strings or token suffixes that some
    HTTP clients echo into their exceptions.
    """

    return ProbeResult(
        read_ok=False,
        write_ok=False,
        auto_fetched_account_id=None,
        artifact=None,
        state="read_failed",
        error_message=f"{service} read probe failed: {type(exc).__name__}",
    )


def _extract_account_id(payload: Any) -> str | None:
    """Pick ``accountId`` (or ``account_id``) out of a JSON-shaped dict.

    Atlassian Cloud surfaces vary in casing across services:

    * Jira / Confluence cloud return ``accountId`` (camelCase).
    * Bitbucket cloud sometimes returns ``account_id`` (snake_case)
      and sometimes ``uuid`` — we accept ``accountId`` first, then
      ``account_id``, then ``uuid``.

    Returns ``None`` for falsy / missing values so the caller never
    sees an empty string masquerading as an id (which would later
    trigger the mismatch fail-fast path).
    """

    if not isinstance(payload, dict):
        return None
    for key in ("accountId", "account_id", "uuid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Inline bot identity probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotIdentityProbeResult:
    """Outcome of :func:`probe_bot_identity`.

    Attributes:
        success: Whether the probe resolved an account_id.
        account_id: Resolved account_id (None on failure).
        error: Error description on failure (None on success).
    """

    success: bool
    account_id: str | None = None
    error: str | None = None


async def probe_bot_identity(
    dept_id: str,
    service: ProbeService,
    client: AtlassianProbeClient,
    cred: ResolvedCredential,
) -> BotIdentityProbeResult:
    """Probe Atlassian to resolve the bot's account_id for *(dept_id, service)*.

    This is the inline probe called by the post-create credential
    endpoint after Vault write + DB upsert succeed. It issues
    a lightweight read-only call:

    * Jira / Confluence: ``GET /rest/api/3/myself`` or equivalent.
    * Bitbucket: ``GET /2.0/user``.

    The function **never raises** — failures are surfaced via the
    returned :class:`BotIdentityProbeResult` so the caller can
    include ``account_id_probe_status`` in the HTTP response without
    breaking the 200 contract.

    Args:
        dept_id: Department identifier (for logging context).
        service: Atlassian surface to probe.
        client: An :class:`AtlassianProbeClient` implementation.
        cred: Resolved credential triple for the bot.

    Returns:
        :class:`BotIdentityProbeResult` with ``success=True`` and
        ``account_id`` populated on success, or ``success=False``
        with an ``error`` description on failure.
    """

    try:
        if service == "jira":
            payload = await client.jira_myself(cred)
        elif service == "confluence":
            payload = await client.confluence_user(cred)
        elif service == "bitbucket":
            payload = await client.bitbucket_user(cred)
        else:
            return BotIdentityProbeResult(
                success=False,
                error=f"unsupported service: {service}",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "probe_bot_identity.failed dept=%s service=%s err=%s",
            dept_id,
            service,
            type(exc).__name__,
        )
        return BotIdentityProbeResult(
            success=False,
            error=f"probe_failed: {type(exc).__name__}",
        )

    account_id = _extract_account_id(payload)
    if not account_id:
        return BotIdentityProbeResult(
            success=False,
            error="account_id not found in probe response",
        )

    return BotIdentityProbeResult(
        success=True,
        account_id=account_id,
    )


__all__ = [
    "AtlassianProbeClient",
    "BotIdentityProbeResult",
    "PROBE_ARTIFACT_PREFIX",
    "PROBE_ARTIFACT_SUFFIX",
    "ProbeArtifact",
    "ProbeArtifactType",
    "ProbeResult",
    "ProbeRunner",
    "ProbeService",
    "ProbeState",
    "ProbeTargets",
    "ResolvedCredential",
    "is_probe_artifact_title",
    "make_probe_title",
    "probe_bot_identity",
]
