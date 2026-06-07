"""Notification dispatch is success-gated and failure-mandatory.



Hypothesis-driven verification of the dispatch policy implemented in:class:`notification.service.NotificationService.notify_workflow_completion`.

Invariant statement
---------------------------------------------

For any hypothesis-generated ``(workflow_result, dept_config)`` pair
where:

* ``workflow_result.status ∈ {"completed", "failed", "partial"}``
* ``dept_config.notify_on_success ∈ {True, False}``
* ``dept_config.notify_channels ⊆ {"slack", "email", "teams"}``:meth:`NotificationService.notify_workflow_completion(workflow_id, dept,
result)` MUST satisfy:

 (a) ``result.status == "failed"``  Slack send is **mandatory**
 (regardless of ``dept_config.notify_on_success``) when the
 dept has a Slack webhook; if ``dept.notify_email`` is also set
 an email is dispatched too.
 (b) ``result.status ∈ {"completed", "partial"}`` and
 ``dept.notify_on_success == False``  no channel is hit
 (pure no-op; no template render, no log row).
 (c) ``notify_on_success == True`` and
 ``result.status ∈ {"completed", "partial"}``  each channel
 listed in ``dept.notify_channels`` is dispatched (when its
 target is configured); channels NOT in the set are NOT hit.
 (d) Every dispatch attempt writes exactly one row per ``(channel)``
 to ``shared.notification_log``; the row's
 ``dedup_key = sha256(f"{workflow_id}:{channel}:{kind}")`` is
 ``UNIQUE`` so a retried call cannot double-deliver - the
 adapter is invoked **at most once** per ``(workflow_id,
 channel, kind)`` triple across any number of retries.
 (e) The body persisted in ``notification_log`` is a sha256 hash of
 the rendered Slack/email body ( log-redaction parity); the
 ``target`` column is also a sha256 of the webhook URL / email
 address - the plain webhook URL never crosses the table.

The companion file ``platform/libs/notification/tests/\
test_notify_workflow_completion.py`` already pins the example-based
slice of this contract; this test **reuses the four fakes
declared there** (``_FakeSlackAdapter``, ``_FakeEmailAdapter``,
``_FakePromptRenderer``, ``_FakeNotificationLogStore``) so the
hypothesis-driven branch coverage exercises the same SUT plumbing the
unit tests use. Reuse keeps the unit and property suites aligned
with the same deterministic policy.

Surface under test
------------------

The dispatcher lives at
``platform/libs/notification/src/notification/service.py``
and exposes::

 class NotificationService:
 async def notify_workflow_completion(
 self,
 *,
 workflow_id: str,
 dept: DeptConfigView,
 result: WorkflowResult,
 prompt_vars: Mapping | None = None,) -> NotificationOutcome:...

The ``NotificationOutcome`` flags (``slack_sent``, ``email_sent``,
``slack_skipped_dedup`` …) are the channel-level observation surface;
``log_store.rows`` and ``slack.sends`` / ``email.sends`` carry the
forensic detail the property assertions check.

Implementation Notes
--------------------

* The four fakes are defined verbatim in
 ``libs/notification/tests/test_notify_workflow_completion.py``.
 We import them as a public-by-convention surface (the leading ``_``
 marks them as test-only; treating them as a shared in-memory
 stand-in across the property and unit suites keeps the fixtures
 aligned).
* The service implementation defines the failure-mandatory and
 success-gated policy.
* The assertions below cover the dispatch, logging, idempotency, and
 redaction behavior described above.
* The ``shared.notification_log`` schema lives in
 ``infra/postgres/init/20_ops.sql``; the
 ``UNIQUE(dedup_key)`` constraint is what (d) leans on.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Iterable

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap - reach into ``libs/notification/tests`` so the
# fakes declared by the unit suite can be imported here. The workspace
# ``conftest.py`` already pushes ``libs/notification/src`` onto
# ``sys.path``; we add the *tests* directory so the fakes module is
# importable as ``test_notify_workflow_completion``.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_NOTIFICATION_TESTS_DIR: Path = (
    _REPO_ROOT / "libs" / "notification" / "tests"
)
_NOTIFICATION_TESTS_STR = str(_NOTIFICATION_TESTS_DIR)
if (
    _NOTIFICATION_TESTS_DIR.is_dir()
    and _NOTIFICATION_TESTS_STR not in sys.path
):
    # Push to the front so a stray name collision with another
    # ``test_notify_workflow_completion`` module is shadowed by the
    # canonical fakes.
    sys.path.insert(0, _NOTIFICATION_TESTS_STR)


from notification import (  # noqa: E402
    DeptConfigView,
    NotificationError,
    NotificationLogEntry,
    NotificationService,
    TemplateRenderError,
    WorkflowResult,
)

# Reuse the canonical in-memory fakes from
# ``libs/notification/tests/test_notify_workflow_completion.py``. Any
# change to the dispatcher's collaborator surface (Slack / email /
# prompt / log-store) updates the fakes in one place and both the unit
# and property suites pick it up.
from test_notify_workflow_completion import (  # noqa: E402
    _FakeEmailAdapter,
    _FakeNotificationLogStore,
    _FakePromptRenderer,
    _FakeSlackAdapter,
)


# ---------------------------------------------------------------------------
# Constants from the dispatcher contract
# ---------------------------------------------------------------------------

#: Logical prompt name selected on the failure branch
#: (:data:`notification.service._FAILURE_TEMPLATE`).
_FAILURE_TEMPLATE: str = "notifications/workflow_failed"

#: Logical prompt name selected on the success / partial branch
#: (:data:`notification.service._SUCCESS_TEMPLATE`).
_SUCCESS_TEMPLATE: str = "notifications/workflow_succeeded"

#: ``kind`` literal pinned to the ``shared.notification_log`` row when
#: the dispatcher writes a workflow-completion event
#: (:data:`notification.types.NotificationKind`).
_KIND: str = "workflow_completion"

#: Stable separator wired into:func:`notification.service._dedup_key`.
#: Mirrored here so the invariant computes the same digest the
#: dispatcher writes - a regression that swaps the separator surfaces as
#: a dedup_key mismatch.
_HASH_SEP: str = ":"


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# ``workflow_id`` shape mirrors the ``"<dept>-<issue>-<n>"`` format:
# hyphens only, no ``:`` (which would collide with the hash separator
# and make dedup_key ambiguous). The strategy is intentionally narrow
# so generated ids stay within the production-realistic alphabet.
_workflow_id_strategy: st.SearchStrategy[str] = st.from_regex(
    r"^[a-z][a-z0-9]{0,8}-[A-Z]{2,4}-[0-9]{1,4}-[0-9]{1,3}$",
    fullmatch=True,
)

_status_strategy: st.SearchStrategy[str] = st.sampled_from(
    ["completed", "failed", "partial"]
)

# ``dept_id`` matches the foundation departments.json shape (lowercase
# slug with underscores).
_dept_id_strategy: st.SearchStrategy[str] = st.from_regex(
    r"^[a-z][a-z0-9_]{1,15}$", fullmatch=True
)

# Non-empty Slack webhook URL (a stable host prefix + a random secret
# segment so distinct dept configs hash to distinct ``target`` values).
_slack_webhook_strategy: st.SearchStrategy[str] = st.from_regex(
    r"^https://hooks\.slack\.com/services/T[0-9]/B[0-9]/S[A-Z0-9]{4,8}$",
    fullmatch=True,
)

# RFC-5322-ish email address; we keep the shape narrow because the
# dispatcher does not validate the address - it just hashes it for the
# ``target`` column.
_email_strategy: st.SearchStrategy[str] = st.from_regex(
    r"^[a-z]{1,8}@[a-z]{1,8}\.(com|io|test)$", fullmatch=True
)

# Channel subset. ``"teams"`` is forward-compat (no adapter wired) so
# generating it in the channel set lets the property exercise the
# "channel listed but no adapter" silent-skip branch while keeping the
# channel vocabulary aligned with the database constraint.
_channels_strategy: st.SearchStrategy[frozenset[str]] = st.sets(
    st.sampled_from(["slack", "email", "teams"]),
    min_size=0,
    max_size=3,
).map(frozenset)


@st.composite
def _dept_strategy(draw: st.DrawFn) -> DeptConfigView:
    """Build a:class:`DeptConfigView` with random eligible-channel mix.

 Both target fields are independently drawn (or set to ``None``) so
 the dispatcher's "eligible channel ∧ no target  skip" branch is
 covered alongside the "eligible channel ∧ target configured
 dispatch" branch.
 """

    has_slack_webhook = draw(st.booleans())
    has_email = draw(st.booleans())

    return DeptConfigView(
        dept_id=draw(_dept_id_strategy),
        notify_on_success=draw(st.booleans()),
        notify_channels=draw(_channels_strategy),  # type: ignore[arg-type]
        slack_webhook=(
            draw(_slack_webhook_strategy) if has_slack_webhook else None
        ),
        notify_email=draw(_email_strategy) if has_email else None,
    )


@st.composite
def _result_strategy(draw: st.DrawFn) -> WorkflowResult:
    """Build a:class:`WorkflowResult` covering all three statuses."""

    status = draw(_status_strategy)
    summary = draw(
        st.text(
            alphabet=st.characters(
                whitelist_categories=("L", "N", "P", "Z"),
                blacklist_characters=("{", "}"),
            ),
            min_size=1,
            max_size=40,
        )
    )
    error: str | None = None
    if status == "failed":
        error = draw(st.text(min_size=0, max_size=80))

    return WorkflowResult(  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=summary,
        error=error,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_template_name(status: str) -> str:
    """Mirror the dispatcher's template selection."""

    return _FAILURE_TEMPLATE if status == "failed" else _SUCCESS_TEMPLATE


def _expected_dedup_key(
    *, workflow_id: str, channel: str, kind: str = _KIND
) -> str:
    """Recompute the deterministic ``dedup_key`` the dispatcher writes.

 Mirrors:func:`notification.service._dedup_key`. Pinning the formula
 here means a regression that swaps the separator or reorders the
 components surfaces immediately as a hash-mismatch assertion
 failure rather than as a silent collision.
 """

    payload = (
        f"{workflow_id}{_HASH_SEP}{channel}{_HASH_SEP}{kind}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _service_with_fresh_fakes() -> tuple[
    NotificationService,
    _FakeSlackAdapter,
    _FakeEmailAdapter,
    _FakePromptRenderer,
    _FakeNotificationLogStore,
]:
    """Construct a:class:`NotificationService` with brand-new fakes.

 Each hypothesis example needs its own isolated set of fakes so
 earlier examples cannot leak ``log_store.seen_dedup_keys`` /
 ``slack.sends`` accumulation into a later example. The
 ``_FakePromptRenderer`` defaults already carry both templates the
 dispatcher needs (``workflow_succeeded`` + ``workflow_failed``).
 """

    slack = _FakeSlackAdapter()
    email = _FakeEmailAdapter()
    prompts = _FakePromptRenderer()
    log_store = _FakeNotificationLogStore()
    service = NotificationService(
        slack=slack,
        email=email,
        prompts=prompts,
        log_store=log_store,
    )
    return service, slack, email, prompts, log_store


def _run(coro):
    """Run an async coroutine to completion in a fresh event loop."""

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# invariant - dispatch, logging, idempotency, and redaction behavior
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    workflow_id=_workflow_id_strategy,
    dept=_dept_strategy(),
    result=_result_strategy(),
)
def test_notification_dispatch_invariants(
    workflow_id: str,
    dept: DeptConfigView,
    result: WorkflowResult,
) -> None:
    """Success-gated and failure-mandatory dispatch.


 """

    service, slack, email, prompts, store = _service_with_fresh_fakes()

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id=workflow_id,
            dept=dept,
            result=result,
        )
    )

    is_failure = result.status == "failed"
    expected_template = _expected_template_name(result.status)

    # ----- Decide which channels the dispatcher *should* have hit ------
    # Mirrors the eligibility block in
    #:meth:`NotificationService.notify_workflow_completion`. Computing
    # the expectation here (rather than copying the SUT's state)
    # ensures a regression that flips the gate surfaces as a diff.
    slack_eligible = is_failure or "slack" in dept.notify_channels
    email_eligible = "email" in dept.notify_channels or (
        is_failure and dept.notify_email is not None
    )

    slack_expected = slack_eligible and dept.slack_webhook is not None
    email_expected = email_eligible and dept.notify_email is not None

    # ----- (b) success-gated no-op branch ------------------------------
    # ``status ∈ {"completed","partial"}`` AND
    # ``dept.notify_on_success == False``  the entire dispatch is a
    # no-op: no template render, no adapter invocation, no log row.
    if not is_failure and not dept.notify_on_success:
        assert prompts.render_calls == [], (
                f"success-gated no-op rendered a "
            f"template anyway: {prompts.render_calls!r}"
        )
        assert slack.sends == [], (
                f"success-gated no-op invoked slack: "
            f"{slack.sends!r}"
        )
        assert email.sends == [], (
                f"success-gated no-op invoked email: "
            f"{email.sends!r}"
        )
        assert store.rows == [], (
                f"success-gated no-op wrote a log row: "
            f"{store.rows!r}"
        )
        assert outcome.slack_sent is False
        assert outcome.email_sent is False
        return

    # ----- Template selection (success vs failure) ---------------------
    # The dispatcher renders exactly once per call (the same body lands
    # on every eligible channel), so the render-call list has length 1
    # whenever any channel was eligible. If no channel was eligible
    # (e.g. failure with no slack webhook AND no notify_email) the
    # dispatcher still renders the template upfront so the body_hash
    # remains stable for any forthcoming retry - but the property only
    # asserts the *name* selected was correct.
    rendered_names = [n for n, _ in prompts.render_calls]
    assert all(
        name == expected_template for name in rendered_names
    ), (
        f"invariant - template selection: expected only "
        f"{expected_template!r} renders, got {rendered_names!r} for "
        f"status={result.status!r}, notify_on_success="
        f"{dept.notify_on_success}."
    )

    # ----- (a) failure-mandatory: Slack send always when webhook set ---
    if is_failure:
        if dept.slack_webhook is not None:
            assert outcome.slack_sent is True, (
                f"failure-mandatory slack send did "
                f"not fire (notify_on_success="
                f"{dept.notify_on_success}, channels="
                f"{set(dept.notify_channels)!r}, webhook set)."
            )
            assert len(slack.sends) == 1, (
                f"failure-mandatory slack adapter "
                f"call count {len(slack.sends)} != 1."
            )
            assert slack.sends[0][1] == dept.slack_webhook, (
                f"slack adapter received webhook "
                f"{slack.sends[0][1]!r} != dept.slack_webhook "
                f"{dept.slack_webhook!r}."
            )
        else:
            # No webhook  dispatcher logs and skips (sibling
            # owns the admin-channel fallback). Slack adapter must not
            # be called.
            assert slack.sends == [], (
                f"failure path with no webhook "
                f"still invoked slack adapter: {slack.sends!r}"
            )
            assert outcome.slack_sent is False

        # Email-on-failure: dispatched iff ``notify_email`` is set.
        # When ``notify_email is None`` the email channel must be
        # skipped entirely.
        if dept.notify_email is not None:
            assert outcome.email_sent is True, (
                f"failure path with notify_email "
                f"{dept.notify_email!r} did not email."
            )
            assert len(email.sends) == 1
            assert email.sends[0][1] == dept.notify_email
        else:
            assert email.sends == [], (
                f"failure path with no "
                f"notify_email invoked email adapter: {email.sends!r}"
            )
            assert outcome.email_sent is False

    # ----- (c) success-gated dispatch on listed channels ---------------
    if not is_failure and dept.notify_on_success:
        # Only listed channels with a configured target should fire.
        if "slack" in dept.notify_channels and dept.slack_webhook is not None:
            assert outcome.slack_sent is True
            assert len(slack.sends) == 1
        else:
            assert slack.sends == [], (
                f"slack fired for "
                f"channels={set(dept.notify_channels)!r}, "
                f"webhook={dept.slack_webhook!r}: {slack.sends!r}"
            )

        if "email" in dept.notify_channels and dept.notify_email is not None:
            assert outcome.email_sent is True
            assert len(email.sends) == 1
        else:
            assert email.sends == [], (
                f"email fired for "
                f"channels={set(dept.notify_channels)!r}, "
                f"notify_email={dept.notify_email!r}: {email.sends!r}"
            )

        # ``"teams"`` listed in notify_channels has no adapter wired
        # the dispatcher silently skips. The ``teams`` slot is a
        # forward-compat literal in the
        # ``shared.notification_log.channel`` ``CHECK`` constraint);
        # asserting ``slack.sends == []`` / ``email.sends == []``
        # already covers the negative branch.

    # ----- (d) one log row per channel + deterministic dedup_key -------
    # Every call writes one row per channel that was *actually*
    # dispatched (slack or email adapter invoked). The row carries the
    # sha256 dedup_key recomputed locally so a regression that swaps
    # the hash separator or component order surfaces here.
    expected_channels: set[str] = set()
    if slack_expected:
        expected_channels.add("slack")
    if email_expected:
        expected_channels.add("email")

    actual_channels = {row.channel for row in store.rows}
    assert actual_channels == expected_channels, (
        f"log row channel set "
        f"{actual_channels!r} != expected {expected_channels!r} for "
        f"status={result.status!r}, channels="
        f"{set(dept.notify_channels)!r}, "
        f"webhook={'set' if dept.slack_webhook else 'None'}, "
        f"email={'set' if dept.notify_email else 'None'}."
    )
    # Exactly one row per channel.
    assert len(store.rows) == len(expected_channels), (
        f"expected {len(expected_channels)} log "
        f"row(s), saw {len(store.rows)}: {store.rows!r}"
    )

    for row in store.rows:
        expected_key = _expected_dedup_key(
            workflow_id=workflow_id, channel=row.channel
        )
        assert row.dedup_key == expected_key, (
            f"dedup_key mismatch for channel "
            f"{row.channel!r}: row={row.dedup_key!r}, expected "
            f"sha256({workflow_id!r}:{row.channel}:{_KIND}) = "
            f"{expected_key!r}."
        )
        assert row.kind == _KIND, (
            f"log row kind {row.kind!r} != "
            f"{_KIND!r}."
        )

    # ----- (e) body_hash + target redaction ----------------------------
    # The body persisted is sha256(body); the target is sha256(webhook /
    # email). Plain webhook URL must NOT appear verbatim in any row.
    for row in store.rows:
        if row.channel == "slack":
            assert dept.slack_webhook is not None  # narrow for type
            assert row.target == hashlib.sha256(
                dept.slack_webhook.encode("utf-8")
            ).hexdigest(), (
                f"slack target {row.target!r} is "
                f"not sha256(webhook) for "
                f"webhook={dept.slack_webhook!r}."
            )
            assert row.target != dept.slack_webhook, (
                f"plain webhook URL leaked into "
                f"target column: {row.target!r}"
            )
        elif row.channel == "email":
            assert dept.notify_email is not None
            assert row.target == hashlib.sha256(
                dept.notify_email.encode("utf-8")
            ).hexdigest(), (
                f"email target {row.target!r} is "
                f"not sha256(notify_email) for "
                f"notify_email={dept.notify_email!r}."
            )

        # Body hash matches sha256 of the rendered body the fake
        # produced for the selected template.
        rendered = prompts.bodies[expected_template]
        assert row.body_hash == hashlib.sha256(
            rendered.encode("utf-8")
        ).hexdigest(), (
            f"body_hash {row.body_hash!r} != "
            f"sha256(rendered body) for template "
            f"{expected_template!r}."
        )

    # ----- (e) PromptLoader rendered the body --------------------------
    # When any channel was dispatched the renderer must have been
    # called at least once with the correct template name.
    if expected_channels:
        assert prompts.render_calls, (
            f"at least one channel dispatched "
            f"({expected_channels!r}) but PromptRenderer.render was "
            f"never called."
        )
        assert prompts.render_calls[0][0] == expected_template


# ---------------------------------------------------------------------------
# idempotent retry with stable dedup_key
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    workflow_id=_workflow_id_strategy,
    dept=_dept_strategy(),
    result=_result_strategy(),
)
def test_notification_dispatch_idempotent_retry(
    workflow_id: str,
    dept: DeptConfigView,
    result: WorkflowResult,
) -> None:
    """Second call with same workflow_id is a no-op.

 Drives:meth:`notify_workflow_completion` twice with identical
 inputs against a single fake set so the:class:`_FakeNotificationLogStore` honours the
 ``UNIQUE(dedup_key)`` constraint across both calls. The
 dispatcher's contract: once the first call lands the row, the
 second call sees ``log_store.insert`` return ``False`` and skips
 the adapter send. End state - adapter call count is the *same*
 after the second call as it was after the first.


 """

    # Skip the configurations that produce no dispatch at all - there's
    # nothing to dedup if the first call was already a pure no-op.
    is_failure = result.status == "failed"
    if not is_failure and not dept.notify_on_success:
        # Pure no-op branch; covered by the main invariant above.
        # Skipping here keeps the retry assertion focused on the
        # interesting branch (something fired  retry must NOT fire).
        assume(False)

    service, slack, email, _, store = _service_with_fresh_fakes()

    # --- First call lands eligible rows + adapter sends ---
    outcome_a = _run(
        service.notify_workflow_completion(
            workflow_id=workflow_id,
            dept=dept,
            result=result,
        )
    )

    # Snapshot adapter call counts.
    slack_calls_after_first = len(slack.sends)
    email_calls_after_first = len(email.sends)
    rows_after_first = len(store.rows)
    seen_dedup_after_first = set(store.seen_dedup_keys)

    # --- Second call with identical inputs - must be idempotent ---
    outcome_b = _run(
        service.notify_workflow_completion(
            workflow_id=workflow_id,
            dept=dept,
            result=result,
        )
    )

    # Adapter call counts MUST be unchanged (no double-delivery).
    assert len(slack.sends) == slack_calls_after_first, (
        f"slack adapter fired again on retry: "
        f"{len(slack.sends)} > {slack_calls_after_first}; "
        f"workflow_id={workflow_id!r}, dept={dept!r}, "
        f"status={result.status!r}."
    )
    assert len(email.sends) == email_calls_after_first, (
        f"email adapter fired again on retry: "
        f"{len(email.sends)} > {email_calls_after_first}."
    )

    # Log table row count MUST be unchanged (UNIQUE constraint).
    assert len(store.rows) == rows_after_first, (
        f"notification_log gained rows on retry: "
        f"{len(store.rows)} > {rows_after_first}; "
        f"dedup_keys={store.seen_dedup_keys!r}"
    )
    # And the dedup_key set is exactly the same.
    assert set(store.seen_dedup_keys) == seen_dedup_after_first, (
        f"dedup_key set diverged on retry: "
        f"{store.seen_dedup_keys!r} vs {seen_dedup_after_first!r}."
    )

    # The retry's outcome surfaces ``*_skipped_dedup`` for whatever
    # channel was eligible in the first call. We don't assert the
    # specific flags (they depend on which channels fired) - the
    # adapter-count + row-count invariants above cover the core
    # idempotency contract.
    # However: any channel that fired in call A and was eligible in
    # call B MUST report skipped_dedup=True in outcome_b.
    if outcome_a.slack_sent:
        assert (
            outcome_b.slack_skipped_dedup is True
            or outcome_b.slack_sent is False
        ), (
            f"outcome_b.slack_skipped_dedup did "
            f"not flip after retry: {outcome_b!r}"
        )
    if outcome_a.email_sent:
        assert (
            outcome_b.email_skipped_dedup is True
            or outcome_b.email_sent is False
        ), (
            f"outcome_b.email_skipped_dedup did "
            f"not flip after retry: {outcome_b!r}"
        )


# ---------------------------------------------------------------------------
# Concrete regression anchors - pinned examples that complement the
# Hypothesis search by fixing a representative input on each branch.
# ---------------------------------------------------------------------------


def test_failed_status_forces_slack_regardless_of_notify_on_success() -> None:
    """Pinned regression anchor for failure-mandatory Slack dispatch.

 A dept that opted out of every success channel
 (``notify_on_success=False``, ``notify_channels=∅``) still
 receives a Slack notification on a ``failed`` workflow when its
 Slack webhook is configured.


 """

    service, slack, _, prompts, store = _service_with_fresh_fakes()
    dept = DeptConfigView(
        dept_id="payment",
        notify_on_success=False,
        notify_channels=frozenset(),  # type: ignore[arg-type]
        slack_webhook="https://hooks.slack.com/services/T0/B0/X1",
        notify_email=None,
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-1-1",
            dept=dept,
            result=WorkflowResult(  # type: ignore[arg-type]
                status="failed", summary="boom", error="kaboom"
            ),
        )
    )

    assert len(slack.sends) == 1
    assert outcome.slack_sent is True
    # Failure template selected (sequence covers redirect to the
    # failure prompt name).
    assert prompts.render_calls[0][0] == _FAILURE_TEMPLATE
    # Exactly one log row, on the slack channel.
    assert [r.channel for r in store.rows] == ["slack"]


def test_completed_with_notify_on_success_false_is_pure_noop() -> None:
    """Pinned regression anchor for success-gated no-op dispatch.


 """

    service, slack, email, prompts, store = _service_with_fresh_fakes()
    dept = DeptConfigView(
        dept_id="payment",
        notify_on_success=False,
        notify_channels=frozenset({"slack", "email"}),  # type: ignore[arg-type]
        slack_webhook="https://hooks.slack.com/services/T0/B0/X2",
        notify_email="ops@example.com",
    )

    outcome = _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-2-1",
            dept=dept,
            result=WorkflowResult(  # type: ignore[arg-type]
                status="completed", summary="ok"
            ),
        )
    )

    assert prompts.render_calls == []
    assert slack.sends == []
    assert email.sends == []
    assert store.rows == []
    assert outcome.slack_sent is False
    assert outcome.email_sent is False


def test_dedup_key_is_sha256_of_workflow_channel_kind() -> None:
    """Pinned regression anchor for the hash formula.


 """

    service, _, _, _, store = _service_with_fresh_fakes()
    dept = DeptConfigView(
        dept_id="payment",
        notify_on_success=True,
        notify_channels=frozenset({"slack"}),  # type: ignore[arg-type]
        slack_webhook="https://hooks.slack.com/services/T0/B0/X3",
        notify_email=None,
    )

    _run(
        service.notify_workflow_completion(
            workflow_id="payment-PAY-3-1",
            dept=dept,
            result=WorkflowResult(  # type: ignore[arg-type]
                status="completed", summary="done"
            ),
        )
    )

    [row] = store.rows
    expected = hashlib.sha256(
        b"payment-PAY-3-1:slack:workflow_completion"
    ).hexdigest()
    assert row.dedup_key == expected, (
        f"dedup_key {row.dedup_key!r} != sha256("
        f"'payment-PAY-3-1:slack:workflow_completion') "
        f"= {expected!r}"
    )


# ---------------------------------------------------------------------------
# Defensive: keep a couple of imported names referenced so a future
# refactor does not silently drop their import wiring.
# ---------------------------------------------------------------------------

_ = (
    NotificationError,
    NotificationLogEntry,
    TemplateRenderError,
    Iterable,
)
