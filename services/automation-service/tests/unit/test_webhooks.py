"""Unit tests for the webhook endpoints.

Exercises the end-to-end response code matrix from
the webhook endpoints by spinning up the canonical FastAPI app, parking a hand-rolled
:class:`WebhooksEndpointDeps` on ``app.state.webhooks``, and posting
to ``POST /webhooks/jira`` / ``POST /webhooks/bitbucket``.

The collaborators (HMAC verifier, dept resolver, bot registry,
``processed_events`` repo, mention sets, iter counts, reporter
resolver, Temporal client, audit logger) are all in-memory fakes so
the tests never touch Postgres / Vault / Temporal. The chain itself
is the real :class:`WebhookFilterChain` — keeping the chain in the
loop ensures the endpoint correctly translates every chain verdict
to the matching HTTP response.

Test matrix (each test pins one row of the design's decision table):

==================================  ===============================  ======
Test                                Outcome                          HTTP
==================================  ===============================  ======
HMAC valid Jira issue_commented     filter pass + workflow dispatch  202
HMAC invalid                        WebhookHmacInvalidError          401
Dept not configured                 WebhookDeptUnresolvedError       400
Filter chain drops (mention filter) drop                             200
Unsupported event type              webhook_event_ignored audit      200
Bitbucket pullrequest:fulfilled     loop_guard_dropped audit         200
Bitbucket happy path                filter pass + workflow dispatch  202
Replay dedup drop                   duplicate_event_dropped          200
``signalWithStart`` failure         release claim + bubble up        500
==================================  ===============================  ======
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves without an ``hatch build``-generated install. Mirrors the
# bootstrap used by the sibling unit tests.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.api.webhooks import (  # noqa: E402
    WebhooksEndpointDeps,
)
from automation_service.app import create_app  # noqa: E402
from automation_service.webhook_filters import (  # noqa: E402
    WebhookFilterChain,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAuditWriter:
    """Append-only sink for :class:`AuditEvent` rows."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FakeProcessedEventsRepo:
    """In-memory replacement for :class:`ProcessedEventsRepo`.

    Mirrors the sync contract the real repo exposes via ``async``
    methods: :meth:`claim` returns ``True`` on first insert, ``False``
    on duplicate; :meth:`is_processed` returns membership; and
    :meth:`release` removes the row.
    """

    def __init__(self) -> None:
        self.claimed: dict[str, str] = {}
        self.released: list[str] = []
        # Hooks that tests can flip to drive the rare-race + failure
        # paths — claim_returns_false simulates the
        # "another worker won the race" branch in
        # :func:`_dispatch_pass`; raise_on_release lets us confirm the
        # release-failure branch logs without breaking the response.
        self.claim_returns_false = False

    async def claim(self, delivery_id: str, provider: str) -> bool:
        if self.claim_returns_false:
            return False
        if delivery_id in self.claimed:
            return False
        self.claimed[delivery_id] = provider
        return True

    async def is_processed(self, delivery_id: str) -> bool:
        return delivery_id in self.claimed

    async def release(self, delivery_id: str) -> bool:
        self.released.append(delivery_id)
        existed = self.claimed.pop(delivery_id, None) is not None
        return existed


class _FakeWorkflowClient:
    """Minimal :class:`SupportsStartWorkflow` test double.

    Records every ``start_workflow`` invocation and optionally raises
    a configured exception so the failure-path test can cover the
    ``release + re-raise`` branch.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_exc: BaseException | None = None
        self.already_started_for: set[str] = set()

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> str:
        self.calls.append(
            {
                "workflow": workflow,
                "args": list(args),
                "id": id,
                "task_queue": task_queue,
                "kwargs": kwargs,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        if id in self.already_started_for:
            from temporalio.exceptions import WorkflowAlreadyStartedError

            raise WorkflowAlreadyStartedError(workflow_id=id, workflow_type=workflow)
        return id


class _FakeJiraDeptResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def resolve_jira_dept(self, project_key: str) -> str | None:
        return self.mapping.get(project_key)


class _FakeBitbucketDeptResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def resolve_bitbucket_dept(self, repo_slug: str) -> str | None:
        return self.mapping.get(repo_slug)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_JIRA_SECRET = b"jira-test-secret"
_BB_SECRET = b"bitbucket-test-secret"


def _sign(body: bytes, secret: bytes) -> str:
    """Produce an Atlassian-format ``sha256=...`` HMAC header."""

    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_chain(
    *,
    hmac_secret: bytes = _JIRA_SECRET,
    dept_id: str | None = "payments",
    bot_account_ids: frozenset[str] = frozenset(),
    processed_ids: set[str] | None = None,
    mention_set: frozenset[str] = frozenset(),
    iter_count: int = 0,
    reporter: str = "reporter-1",
) -> WebhookFilterChain:
    """Build a real :class:`WebhookFilterChain` wired to in-memory fakes.

    The HMAC verifier reads the raw body + signature out of the event
    via the same private envelope the production handler uses, so the
    chain's ``verify_hmac`` callback exercises the real digest path
    rather than a trivial truthy stub.
    """

    seen = processed_ids if processed_ids is not None else set()

    def _verify_hmac(event: Any) -> bool:
        from automation_service.api.webhooks import _extract_hmac_inputs

        body, signature = _extract_hmac_inputs(event)
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(hmac_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(
            expected, signature[len("sha256=") :]
        )

    return WebhookFilterChain(
        verify_hmac=_verify_hmac,
        resolve_dept=lambda ev: dept_id,
        bot_account_ids=lambda: bot_account_ids,
        is_processed=lambda d: d in seen,
        mention_set_for=lambda i: mention_set,
        iter_count_for=lambda i: iter_count,
        reporter_for=lambda i: reporter,
    )


def _build_app(*, deps: WebhooksEndpointDeps) -> TestClient:
    """Build a fresh app, attach the deps, and return a TestClient."""

    app = create_app()
    app.state.webhooks = deps
    return TestClient(app)


def _frozen_clock() -> datetime:
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_deps(
    *,
    chain: WebhookFilterChain,
    workflow_client: _FakeWorkflowClient | None = None,
    processed_events: _FakeProcessedEventsRepo | None = None,
    audit_writer: _FakeAuditWriter | None = None,
    jira_resolver: _FakeJiraDeptResolver | None = None,
    bitbucket_resolver: _FakeBitbucketDeptResolver | None = None,
) -> WebhooksEndpointDeps:
    return WebhooksEndpointDeps(
        chain=chain,
        processed_events=processed_events or _FakeProcessedEventsRepo(),
        workflow_client=workflow_client or _FakeWorkflowClient(),
        audit_logger=AuditLogger(writer=audit_writer or _FakeAuditWriter()),
        jira_dept_resolver=jira_resolver,
        bitbucket_dept_resolver=bitbucket_resolver,
        clock=_frozen_clock,
        monotonic_clock=lambda: 0.0,
    )


def _make_jira_payload(
    *,
    event_type: str = "jira:issue_commented",
    issue_key: str = "PAY-100",
    project_key: str = "PAY",
    actor_id: str = "reporter-1",
    body_text: str = "please look at this",
) -> dict[str, Any]:
    return {
        "webhookEvent": event_type,
        "user": {"accountId": actor_id},
        "comment": {
            "body": body_text,
            "author": {"accountId": actor_id},
        },
        "issue": {
            "key": issue_key,
            "fields": {"project": {"key": project_key}},
        },
    }


def _make_bitbucket_payload(
    *,
    repo_slug: str = "payment-callbacks",
    pr_id: int = 42,
    actor_account_id: str = "human-1",
    body_text: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "actor": {"account_id": actor_account_id},
        "repository": {
            "full_name": repo_slug,
            "slug": repo_slug.split("/")[-1],
        },
        "pullrequest": {
            "id": pr_id,
            "title": "Add feature X",
        },
    }
    if body_text is not None:
        payload["comment"] = {"content": {"raw": body_text}}
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJiraEndpointHappyPath:
    """A valid Jira ``issue_commented`` delivery dispatches a workflow."""

    def test_dispatches_workflow_with_202(self) -> None:
        """Valid Jira delivery starts a workflow."""

        chain = _make_chain(
            iter_count=1,
            reporter="reporter-1",  # first-iter exception triggers
        )
        wf_client = _FakeWorkflowClient()
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()

        deps = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
            jira_resolver=_FakeJiraDeptResolver({"PAY": "payments"}),
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-jira-1",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["workflow_id"] == "automation-jira-PAY-100"
        assert data["was_existing"] is False
        # The delivery should have been claimed and the workflow
        # started exactly once.
        assert "delivery-jira-1" in repo.claimed
        assert len(wf_client.calls) == 1
        call = wf_client.calls[0]
        assert call["workflow"] == "AutomationWorkflow"
        assert call["id"] == "automation-jira-PAY-100"
        assert call["task_queue"] == "automation-tq"
        # Audit row pinned to the success branch.
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_started" in actions


class TestJiraHmacFailure:
    """Invalid HMAC → 401 with ``webhook_hmac_invalid`` audit row."""

    def test_returns_401_when_signature_mismatches(self) -> None:
        """HMAC failure writes the failure audit row."""

        chain = _make_chain(iter_count=1)
        audit = _FakeAuditWriter()
        deps = _make_deps(chain=chain, audit_writer=audit)
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        # Sign with a wrong secret on purpose.
        bad_signature = _sign(body, b"not-the-real-secret")

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": bad_signature,
                "X-Atlassian-Webhook-Identifier": "delivery-bad-hmac",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 401
        assert resp.json()["reason"] == "webhook_hmac_invalid"
        actions = [e.action for e in audit.events]
        assert actions == ["webhook_hmac_invalid"]


class TestJiraDeptUnresolved:
    """Dept resolution miss → 400 with ``webhook_dept_unresolved``."""

    def test_returns_400_when_dept_resolver_returns_none(self) -> None:
        """Dept resolve failure writes the failure audit row."""

        chain = _make_chain(dept_id=None, iter_count=1)
        audit = _FakeAuditWriter()
        deps = _make_deps(chain=chain, audit_writer=audit)
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-no-dept",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 400
        assert resp.json()["reason"] == "webhook_dept_unresolved"
        actions = [e.action for e in audit.events]
        assert actions == ["webhook_dept_unresolved"]


class TestJiraFilterChainDrop:
    """Mention filter drops a non-mentioned commenter (iter > 1)."""

    def test_returns_200_with_drop_reason(self) -> None:
        """Dropped delivery writes the drop audit row."""

        chain = _make_chain(
            iter_count=2,
            mention_set=frozenset({"someone-else"}),
            reporter="reporter-1",
        )
        wf_client = _FakeWorkflowClient()
        audit = _FakeAuditWriter()

        deps = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            audit_writer=audit,
        )
        client = _build_app(deps=deps)

        # Actor "intruder" is neither the reporter nor in the mention
        # set; iter_count > 1 means Z6 first-iter exception does not
        # apply. Expected: the chain drops the comment.
        payload = _make_jira_payload(actor_id="intruder")
        body = json.dumps(payload).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-mention-drop",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dropped"
        assert data["reason"] == "comment_ignored_unauthorized_actor"
        # No workflow was dispatched.
        assert wf_client.calls == []


class TestJiraUnsupportedEventType:
    """Events outside the allowlist are silently dropped."""

    def test_returns_200_with_webhook_event_ignored_audit(self) -> None:
        """Unsupported event writes the ignored audit row."""

        chain = _make_chain()
        wf_client = _FakeWorkflowClient()
        audit = _FakeAuditWriter()
        deps = _make_deps(
            chain=chain, workflow_client=wf_client, audit_writer=audit
        )
        client = _build_app(deps=deps)

        # ``jira:project_created`` is not in the allowlist.
        payload = _make_jira_payload(event_type="jira:project_created")
        body = json.dumps(payload).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-unsupported",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["reason"] == "webhook_event_ignored"
        actions = [e.action for e in audit.events]
        assert actions == ["webhook_event_ignored"]
        # The chain was never invoked, so nothing was claimed and no
        # workflow was started.
        assert wf_client.calls == []


class TestBitbucketLoopGuardFulfilled:
    """``pullrequest:fulfilled`` is silently loop-guarded."""

    def test_returns_200_with_loop_guard_audit(self) -> None:
        """Loop-guarded event writes the loop guard audit row."""

        chain = _make_chain(hmac_secret=_BB_SECRET)
        wf_client = _FakeWorkflowClient()
        audit = _FakeAuditWriter()
        deps = _make_deps(
            chain=chain, workflow_client=wf_client, audit_writer=audit
        )
        client = _build_app(deps=deps)

        payload = _make_bitbucket_payload()
        body = json.dumps(payload).encode("utf-8")
        signature = _sign(body, _BB_SECRET)

        resp = client.post(
            "/webhooks/bitbucket",
            content=body,
            headers={
                "X-Hub-Signature": signature,
                "X-Event-Key": "pullrequest:fulfilled",
                "X-Request-UUID": "delivery-fulfilled",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["reason"] == "loop_guard_dropped"
        assert data["event_type"] == "pullrequest:fulfilled"
        actions = [e.action for e in audit.events]
        assert actions == ["loop_guard_dropped"]
        assert wf_client.calls == []


class TestBitbucketHappyPath:
    """A valid Bitbucket ``pullrequest:created`` dispatches a workflow."""

    def test_dispatches_workflow_with_202(self) -> None:
        """Valid Bitbucket delivery starts a workflow."""

        chain = _make_chain(
            hmac_secret=_BB_SECRET,
            dept_id="payments",
        )
        wf_client = _FakeWorkflowClient()
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        deps = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
            bitbucket_resolver=_FakeBitbucketDeptResolver(
                {"payment-callbacks": "payments"}
            ),
        )
        client = _build_app(deps=deps)

        payload = _make_bitbucket_payload(repo_slug="payment-callbacks", pr_id=42)
        body = json.dumps(payload).encode("utf-8")
        signature = _sign(body, _BB_SECRET)

        resp = client.post(
            "/webhooks/bitbucket",
            content=body,
            headers={
                "X-Hub-Signature": signature,
                "X-Event-Key": "pullrequest:created",
                "X-Request-UUID": "delivery-bb-1",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["workflow_id"].startswith("automation-bb-")
        assert "pr-42" in data["workflow_id"]
        assert len(wf_client.calls) == 1
        assert "delivery-bb-1" in repo.claimed


class TestReplayDedupDrop:
    """Duplicate ``delivery_id`` is dropped by the chain's replay stage."""

    def test_returns_200_with_duplicate_event_dropped(self) -> None:
        """Duplicate delivery is dropped by replay dedup."""

        already_seen = {"delivery-replay"}
        chain = _make_chain(processed_ids=already_seen, iter_count=1)
        wf_client = _FakeWorkflowClient()
        audit = _FakeAuditWriter()
        deps = _make_deps(
            chain=chain, workflow_client=wf_client, audit_writer=audit
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-replay",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["reason"] == "duplicate_event_dropped"
        assert wf_client.calls == []


class TestStartWorkflowFailureReleasesClaim:
    """Temporal error → release the claim + bubble up as 500.

    The handler must not retain the claim if ``signalWithStart`` fails:
    otherwise Atlassian's webhook retry would trip the replay-dedup
    guard and silently swallow the delivery.
    """

    def test_releases_claim_on_workflow_start_error(self) -> None:
        chain = _make_chain(iter_count=1, reporter="reporter-1")
        wf_client = _FakeWorkflowClient()
        wf_client.raise_exc = RuntimeError("temporal cluster unreachable")
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        deps = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        with pytest.raises(RuntimeError, match="temporal cluster unreachable"):
            client.post(
                "/webhooks/jira",
                content=body,
                headers={
                    "X-Atlassian-Webhook-Signature": signature,
                    "X-Atlassian-Webhook-Identifier": "delivery-fail",
                    "Content-Type": "application/json",
                },
            )

        # The claim should have been released so the retry can re-claim
        # the same delivery_id.
        assert repo.released == ["delivery-fail"]
        assert "delivery-fail" not in repo.claimed
        # Audit row pinned to the failure branch.
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_start_failed" in actions


class TestWebhookHandlerNotWired:
    """Without ``app.state.webhooks`` the handler responds 503."""

    def test_returns_503_when_deps_missing(self) -> None:
        app = create_app()
        # Deliberately do not set ``app.state.webhooks``.
        client = TestClient(app)
        resp = client.post(
            "/webhooks/jira",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 503
        assert resp.json()["reason"] == "webhooks_not_wired"


class TestInvalidJsonBody:
    """Bad JSON → 400 ``invalid_json`` (defensive, not in the design table)."""

    def test_returns_400_for_malformed_body(self) -> None:
        chain = _make_chain()
        deps = _make_deps(chain=chain)
        client = _build_app(deps=deps)

        resp = client.post(
            "/webhooks/jira",
            content=b"{ not json",
            headers={
                "X-Atlassian-Webhook-Signature": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["reason"] == "invalid_json"


# ---------------------------------------------------------------------------
# License-cap enforcement
# ---------------------------------------------------------------------------


class _RecordingLicenseCapEnforcer:
    """Configurable :class:`LicenseCapEnforcer` test double.

    Records every ``(dept_id, issue_key)`` invocation. When
    ``raise_for_dept`` matches the incoming ``dept_id``, raises a
    pre-built :class:`BotLicenseCapExceededError` so the dispatcher
    exercises the rejection branch end-to-end. Otherwise returns
    ``None`` (silent allow) so the green path keeps working.
    """

    def __init__(
        self,
        *,
        raise_for_dept: str | None = None,
        limit_type: str = "concurrent",
        current: int = 10,
        max_value: int = 10,
        license_id: str | None = "enterprise-2025",
    ) -> None:
        self._raise_for_dept = raise_for_dept
        self._limit_type = limit_type
        self._current = current
        self._max = max_value
        self._license_id = license_id
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(
        self, dept_id: str, issue_key: str | None
    ) -> None:
        self.calls.append((dept_id, issue_key))
        if dept_id == self._raise_for_dept:
            from middleware.license_cap import BotLicenseCapExceededError

            raise BotLicenseCapExceededError(
                limit_type=self._limit_type,  # type: ignore[arg-type]
                current=self._current,
                max_value=self._max,
                license_id=self._license_id,
                dept_id=dept_id,
                issue_key=issue_key,
            )


class _RecordingJiraAckCommentPoster:
    """Records best-effort Jira ack-comment posts.

    The poster exposes a ``raise_exc`` hook so the test that proves
    "comment failure does not corrupt the 429 response" can flip a
    runtime error on demand. Calls are recorded on every invocation
    regardless of the outcome so the test can assert wiring.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.raise_exc: BaseException | None = None

    async def __call__(
        self, dept_id: str, issue_key: str, body: str
    ) -> None:
        self.calls.append((dept_id, issue_key, body))
        if self.raise_exc is not None:
            raise self.raise_exc


class TestJiraEndpointLicenseCapEnforcement:
    """Wire :func:`enforce_license_cap` into the Jira start path.

    Covers the Jira start path wiring.
    """

    def test_runs_enforcer_on_pass_and_dispatches_on_allow(self) -> None:
        """A green pass invokes the enforcer once and still hits 202."""

        chain = _make_chain(iter_count=1, reporter="reporter-1")
        wf_client = _FakeWorkflowClient()
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        enforcer = _RecordingLicenseCapEnforcer(raise_for_dept=None)

        deps_kwargs = dict(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
            jira_resolver=_FakeJiraDeptResolver({"PAY": "payments"}),
        )
        base = _make_deps(**deps_kwargs)
        # Frozen dataclass → reconstruct with the cap enforcer wired.
        deps = WebhooksEndpointDeps(
            chain=base.chain,
            processed_events=base.processed_events,
            workflow_client=base.workflow_client,
            audit_logger=base.audit_logger,
            jira_dept_resolver=base.jira_dept_resolver,
            bitbucket_dept_resolver=base.bitbucket_dept_resolver,
            clock=base.clock,
            monotonic_clock=base.monotonic_clock,
            license_cap_enforcer=enforcer,
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-cap-allow",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 202, resp.text
        # Enforcer was called once with the resolved dept + issue key.
        assert enforcer.calls == [("payments", "PAY-100")]
        # Idempotency claim still landed (cap allowed → green path).
        assert "delivery-cap-allow" in repo.claimed
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_started" in actions
        assert "webhook_workflow_start_blocked_license_cap" not in actions

    def test_returns_429_with_structured_body_when_cap_exceeded(self) -> None:
        """Cap breach → 429 + audit + best-effort Jira comment."""

        chain = _make_chain(iter_count=1, reporter="reporter-1")
        wf_client = _FakeWorkflowClient()
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        enforcer = _RecordingLicenseCapEnforcer(
            raise_for_dept="payments",
            limit_type="concurrent",
            current=10,
            max_value=10,
            license_id="enterprise-2025",
        )
        poster = _RecordingJiraAckCommentPoster()

        base = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
            jira_resolver=_FakeJiraDeptResolver({"PAY": "payments"}),
        )
        deps = WebhooksEndpointDeps(
            chain=base.chain,
            processed_events=base.processed_events,
            workflow_client=base.workflow_client,
            audit_logger=base.audit_logger,
            jira_dept_resolver=base.jira_dept_resolver,
            bitbucket_dept_resolver=base.bitbucket_dept_resolver,
            clock=base.clock,
            monotonic_clock=base.monotonic_clock,
            license_cap_enforcer=enforcer,
            jira_ack_comment_poster=poster,
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-cap-block",
                "Content-Type": "application/json",
            },
        )

        # 429 with the design's structured body.
        assert resp.status_code == 429, resp.text
        data = resp.json()
        assert data == {
            "error": "bot_license_cap_exceeded",
            "limit": "concurrent",
            "current": 10,
            "max": 10,
        }

        # No claim was taken (Atlassian retry redelivers later).
        assert "delivery-cap-block" not in repo.claimed
        # No workflow was dispatched.
        assert wf_client.calls == []

        # Best-effort Jira ack comment fired with the design template.
        assert len(poster.calls) == 1
        dept_id, issue_key, comment_body = poster.calls[0]
        assert dept_id == "payments"
        assert issue_key == "PAY-100"
        assert "Bot lisans limiti" in comment_body
        assert "concurrent: 10/10" in comment_body

        # Webhook-layer audit row records the delivery metadata.
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_start_blocked_license_cap" in actions
        cap_event = next(
            e
            for e in audit.events
            if e.action == "webhook_workflow_start_blocked_license_cap"
        )
        assert cap_event.result == "denied"
        assert cap_event.dept_id == "payments"
        payload = cap_event.payload or {}
        assert payload["delivery_id"] == "delivery-cap-block"
        assert payload["limit_type"] == "concurrent"
        assert payload["current_value"] == 10
        assert payload["max_value"] == 10
        assert payload["license_id"] == "enterprise-2025"
        assert payload["issue_key"] == "PAY-100"

    def test_jira_comment_failure_does_not_mask_429(self) -> None:
        """A broken comment poster must not change the 429 response."""

        chain = _make_chain(iter_count=1, reporter="reporter-1")
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        enforcer = _RecordingLicenseCapEnforcer(
            raise_for_dept="payments",
            limit_type="daily",
            current=100,
            max_value=100,
        )
        poster = _RecordingJiraAckCommentPoster()
        poster.raise_exc = RuntimeError("MCP unreachable")

        base = _make_deps(
            chain=chain,
            processed_events=repo,
            audit_writer=audit,
            jira_resolver=_FakeJiraDeptResolver({"PAY": "payments"}),
        )
        deps = WebhooksEndpointDeps(
            chain=base.chain,
            processed_events=base.processed_events,
            workflow_client=base.workflow_client,
            audit_logger=base.audit_logger,
            jira_dept_resolver=base.jira_dept_resolver,
            bitbucket_dept_resolver=base.bitbucket_dept_resolver,
            clock=base.clock,
            monotonic_clock=base.monotonic_clock,
            license_cap_enforcer=enforcer,
            jira_ack_comment_poster=poster,
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-cap-comment-fail",
                "Content-Type": "application/json",
            },
        )

        assert resp.status_code == 429
        # The poster was still invoked; its failure was caught.
        assert len(poster.calls) == 1
        # And the audit row landed regardless.
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_start_blocked_license_cap" in actions

    def test_skips_enforcer_when_dept_unresolved(self) -> None:
        """Without a resolved dept_id the cap check is skipped.

        The chain itself rejects unresolved Jira deliveries with
        ``webhook_dept_unresolved`` *before* the dispatcher runs, so
        in practice this code path only fires for Bitbucket flows
        where the optional resolver returned ``None`` — but the
        dispatcher must still be defensive: a missing dept_id means
        no license to check against.
        """

        chain = _make_chain(
            iter_count=1, reporter="reporter-1", dept_id="payments"
        )
        wf_client = _FakeWorkflowClient()
        repo = _FakeProcessedEventsRepo()
        audit = _FakeAuditWriter()
        # Configure the enforcer to *always* raise so we can prove it
        # was never called when the resolver bails.
        enforcer = _RecordingLicenseCapEnforcer(
            raise_for_dept="payments"
        )

        # Note: no jira_resolver wired — the audit dept_id remains None
        # (the chain still resolves dept internally for HMAC).
        base = _make_deps(
            chain=chain,
            workflow_client=wf_client,
            processed_events=repo,
            audit_writer=audit,
        )
        deps = WebhooksEndpointDeps(
            chain=base.chain,
            processed_events=base.processed_events,
            workflow_client=base.workflow_client,
            audit_logger=base.audit_logger,
            jira_dept_resolver=None,  # explicit: no resolver wired
            bitbucket_dept_resolver=None,
            clock=base.clock,
            monotonic_clock=base.monotonic_clock,
            license_cap_enforcer=enforcer,
        )
        client = _build_app(deps=deps)

        body = json.dumps(_make_jira_payload()).encode("utf-8")
        signature = _sign(body, _JIRA_SECRET)

        resp = client.post(
            "/webhooks/jira",
            content=body,
            headers={
                "X-Atlassian-Webhook-Signature": signature,
                "X-Atlassian-Webhook-Identifier": "delivery-cap-no-dept",
                "Content-Type": "application/json",
            },
        )

        # Workflow ran (cap check skipped because dept_id is None).
        assert resp.status_code == 202
        assert enforcer.calls == []
        actions = [e.action for e in audit.events]
        assert "webhook_workflow_start_blocked_license_cap" not in actions
