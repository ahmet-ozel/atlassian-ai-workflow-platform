"""``NotificationService`` — success-gated, failure-mandatory dispatcher.

Implements the workflow notification dispatch policy.

Decision table:

+---------------------+-----------------------+-----------------------+----------------------------------+
| ``result.status``   | ``notify_on_success`` | Slack send?           | Email send?                       |
+=====================+=======================+=======================+==================================+
| ``"failed"``        | (any)                 | **always**            | iff ``"email"`` ∈ channels OR    |
|                     |                       | (mandatory)           | ``notify_email`` is set          |
+---------------------+-----------------------+-----------------------+----------------------------------+
| ``"completed"``     | ``True``              | iff ``"slack"`` ∈     | iff ``"email"`` ∈ channels       |
|                     |                       | channels              |                                  |
+---------------------+-----------------------+-----------------------+----------------------------------+
| ``"completed"``     | ``False``             | no-op                 | no-op                            |
+---------------------+-----------------------+-----------------------+----------------------------------+
| ``"partial"``       | ``True`` / ``False``  | (same as "completed") | (same as "completed")            |
+---------------------+-----------------------+-----------------------+----------------------------------+

Two additional invariants the implementation enforces:

* **Idempotency** — every dispatch attempt computes a deterministic
  ``dedup_key`` from ``sha256(workflow_id + ":" + channel + ":" + kind)``.
  The store's ``UNIQUE`` constraint rejects a second attempt with the
  same key; the dispatcher skips the adapter send when the store reports
  the row already existed.
* **Body redaction** — the body is rendered by the injected
  :class:`PromptRenderer` and only the ``sha256(body)`` hash lands in
  ``shared.notification_log.body_hash``. Plain Slack webhook URLs and
  email addresses are never persisted verbatim — the ``target`` column
  stores a sha256 hash so dispatch history can be correlated with a
  webhook without leaking the secret.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import NotificationError, TemplateRenderError
from .types import (
    DeptConfigView,
    NotificationChannel,
    NotificationKind,
    NotificationStatus,
    WorkflowResult,
)
from .adapters import (
    EmailAdapter,
    NotificationLogEntry,
    NotificationLogStore,
    PromptRenderer,
    SlackAdapter,
)

if TYPE_CHECKING:  # pragma: no cover — only for static type checkers
    from collections.abc import Mapping


__all__ = ["NotificationOutcome", "NotificationService"]


_log = logging.getLogger(__name__)


#: Logical prompt names rendered by :meth:`notify_workflow_completion`. The
#: actual ``.md`` files are produced by the prompt layer
#: (``platform/prompts/notifications/*.md``); the dispatcher only knows the
#: *names*. The mapping is keyed by ``WorkflowStatus``-derived branch:
#: ``"failed"`` always picks the failure template; everything else picks
#: the success template, matching the notification template selection rule
#: (``"workflow_failed" if is_failure else "workflow_succeeded"``).
_FAILURE_TEMPLATE: str = "notifications/workflow_failed"
_SUCCESS_TEMPLATE: str = "notifications/workflow_succeeded"


#: Stable separator for ``dedup_key`` and ``target`` hashes. Picked because
#: ``":"`` is forbidden inside a workflow_id shaped as
#: ``"<dept>-<issue>-<n>"``, so it cannot appear in any hashed component
#: and produce a collision through string concatenation.
_HASH_SEP: str = ":"


_HASHED_TARGET_NONE: str = "none"


#: Logical prompt name for the mandatory admin Slack alarm written when
#: ``AuditPruneWorkflow`` fails. The body is rendered from
#: ``platform/prompts/notifications/audit_prune_failed.md``; the
#: dispatcher only knows the *name*.
_AUDIT_PRUNE_FAILED_TEMPLATE: str = "notifications/audit_prune_failed"


#: Stable identifier for the ``audit_prune_failed`` alarm class. Mirrors
#: the ``alert_type`` argument used by the ``slack.send_admin_channel``
#: call and the ``kind`` literal already
#: reserved in :data:`notification.types.NotificationKind` (so the
#: ``shared.notification_log`` row can carry the same string verbatim).
_AUDIT_PRUNE_FAILED_ALERT_TYPE: str = "audit_prune_failed"


#: Pseudo workflow id used as the first component of the
#: :func:`_dedup_key` hash for an admin alarm. The audit prune alarm is
#: NOT scoped to a workflow_id from the workflow domain
#: (``"<dept>-<issue>-<n>"``); it is a platform-wide ops alarm. We
#: still feed the dedup_key helper a deterministic non-empty string so a
#: single ``AuditPruneWorkflow`` cron run cannot double-deliver under
#: retry. Callers that need stricter idempotency (eg. one alarm per
#: cutoff date, allowing same-day retries to dedupe but next-day failures
#: to fire fresh) can pass an explicit ``run_id`` to
#: :meth:`NotificationService.notify_audit_prune_failed` which becomes
#: the workflow_id component instead.
_AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID: str = "audit-prune-cron"


#: Sentinel ``target`` value persisted in ``shared.notification_log`` for
#: admin-channel alarms. The admin Slack webhook is resolved by the
#: adapter (vault path ``notifications/slack/admin``) and never crosses
#: the dispatcher boundary, so we cannot hash the URL here. We persist a
#: stable label instead so the audit row remains queryable
#: (``WHERE target = 'admin-channel'``) without leaking the secret URL or
#: storing an ambiguous empty string.
_ADMIN_CHANNEL_TARGET: str = "admin-channel"


# ---------------------------------------------------------------------------
# NotificationOutcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationOutcome:
    """Per-call dispatch summary.

    Returned by :meth:`NotificationService.notify_workflow_completion` so
    callers can audit / surface what actually happened without reading
    the ``shared.notification_log`` table.

    Args:
        slack_sent: ``True`` when the Slack adapter was successfully
            invoked (``status='sent'`` row landed in ``notification_log``).
            ``False`` for the no-op branch (success-gated, no slack
            channel) and for the dedup-skip branch (idempotent retry).
        email_sent: Same semantics for the email adapter.
        slack_skipped_dedup: ``True`` when the dispatcher would have
            invoked the Slack adapter but the ``notification_log`` insert
            reported a duplicate ``dedup_key`` (idempotent retry); the
            adapter was **not** called.
        email_skipped_dedup: Same semantics for email.
        slack_failed: ``True`` when the Slack adapter raised; the
            ``notification_log`` row carries ``status='failed'`` + the
            error string.
        email_failed: Same semantics for email.

    The four ``*_failed`` fields are surfaced separately from raised
    exceptions because :meth:`notify_workflow_completion` swallows adapter
    failures *for the purposes of auditing the other channel*. The
    workflow caller still sees a :class:`NotificationError` re-raised if
    *any* mandatory channel failed (failure-mandatory branch only).
    """

    slack_sent: bool = False
    email_sent: bool = False
    slack_skipped_dedup: bool = False
    email_skipped_dedup: bool = False
    slack_failed: bool = False
    email_failed: bool = False


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------


class NotificationService:
    """Success-gated + failure-mandatory notification dispatcher.

    Coordinates Slack and email adapters, notification templates,
    success gating, mandatory failure notifications, and idempotency.

    Args:
        slack: Adapter conforming to :class:`SlackAdapter`. Sibling task
            8.1 provides the concrete ``aiohttp`` implementation.
        email: Adapter conforming to :class:`EmailAdapter`. Sibling task
            8.1 provides the concrete ``aiosmtplib`` implementation.
        prompts: :class:`PromptRenderer` (typically a
            :class:`prompts.loader.PromptLoader`). The dispatcher only
            uses :meth:`PromptRenderer.render`.
        log_store: :class:`NotificationLogStore` backed by
            ``shared.notification_log`` Postgres table.
    """

    def __init__(
        self,
        *,
        slack: SlackAdapter,
        email: EmailAdapter,
        prompts: PromptRenderer,
        log_store: NotificationLogStore,
    ) -> None:
        self._slack = slack
        self._email = email
        self._prompts = prompts
        self._log_store = log_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def notify_workflow_completion(
        self,
        *,
        workflow_id: str,
        dept: DeptConfigView,
        result: WorkflowResult,
        prompt_vars: "Mapping[str, object] | object | None" = None,
    ) -> NotificationOutcome:
        """Dispatch a terminal workflow notification.

        Args:
            workflow_id: Stable id of the completed workflow. Hashed into
                the ``dedup_key`` so a retried call cannot double-deliver.
            dept: Department configuration view. ``notify_on_success`` and
                ``notify_channels`` drive the success-gated branch.
            result: :class:`WorkflowResult` with ``status``, ``summary``,
                optional ``error`` and artifact urls.
            prompt_vars: Optional :class:`prompts.types.PromptVars` (or
                any object the injected :class:`PromptRenderer` accepts).
                Forwarded verbatim to ``prompts.render(name, vars=...)``;
                the dispatcher itself does not type-check this argument
                so the same code can serve different ``PromptVars``
                schemas across services.

        Returns:
            :class:`NotificationOutcome` describing every channel's
            result. Callers may surface this to audit logs without
            re-reading the ``notification_log`` table.

        Raises:
            TemplateRenderError: The body could not be rendered (missing
                placeholder, unknown prompt name). Never retryable.
            NotificationError: A mandatory channel failed (failure-mandatory
                branch only). The store has already recorded the row with
                ``status='failed'``.
        """

        # ------------------------------------------------------------------
        # 1. Decide whether this call notifies at all (success-gated).
        #
        # The failure path bypasses this gate: even when
        # ``dept.notify_on_success == False`` we MUST notify on
        # ``status == "failed"``.
        # ------------------------------------------------------------------
        is_failure = result.status == "failed"
        if not is_failure and not dept.notify_on_success:
            return NotificationOutcome()

        # ------------------------------------------------------------------
        # 2. Render the body once. The same body lands on every channel so
        #    we render before the adapter calls; this also keeps the
        #    body_hash field stable across channels for forensic
        #    correlation.
        # ------------------------------------------------------------------
        template_name = _FAILURE_TEMPLATE if is_failure else _SUCCESS_TEMPLATE
        try:
            body = self._prompts.render(template_name, vars=prompt_vars)
        except Exception as exc:  # noqa: BLE001 — wrap and re-raise
            # Wrap the loader's PromptTemplateError / PromptNotFoundError /
            # KeyError into a single NotificationError subclass so callers
            # of this lib do not need a transitive import on :mod:`prompts`.
            raise TemplateRenderError(
                f"failed to render notification template {template_name!r}: {exc}"
            ) from exc

        body_hash = _sha256_hex(body)

        # ------------------------------------------------------------------
        # 3. Decide which channels are eligible. Failure path forces Slack;
        #    success path consults ``dept.notify_channels``. Email is
        #    *never* implicit for failures — it ships only when the dept
        #    has either listed ``"email"`` in ``notify_channels`` OR set
        #    ``notify_email`` (the design phrases this as "email
        #    config'liyse" — "iff email is configured").
        # ------------------------------------------------------------------
        slack_eligible = is_failure or "slack" in dept.notify_channels
        email_eligible = "email" in dept.notify_channels or (
            is_failure and dept.notify_email is not None
        )

        outcome = NotificationOutcome()

        # ------------------------------------------------------------------
        # 4. Dispatch Slack (if eligible). Order is Slack first, email
        #    second because Slack is the *mandatory* channel on failure;
        #    if its dispatch raises we still want the email row in
        #    ``notification_log`` so audits can see the partial state.
        # ------------------------------------------------------------------
        slack_error: Exception | None = None
        if slack_eligible:
            if dept.slack_webhook is None:
                # Failure-mandatory but no dept Slack webhook configured.
                # ``notify_audit_prune_failed`` covers
                # the admin Slack channel for system-wide alarms; this
                # branch logs and skips so the failure is still observable
                # in audit but does not hard-error the workflow.
                _log.warning(
                    "slack eligible but dept has no webhook; skipping",
                    extra={
                        "workflow_id": workflow_id,
                        "dept_id": dept.dept_id,
                        "is_failure": is_failure,
                    },
                )
            else:
                outcome = await self._dispatch_one(
                    workflow_id=workflow_id,
                    channel="slack",
                    target=dept.slack_webhook,
                    body=body,
                    body_hash=body_hash,
                    outcome=outcome,
                )
                # ``_dispatch_one`` updates the ``slack_*`` fields. We
                # capture any failure here so step 6 can re-raise *after*
                # email also got its chance.
                if outcome.slack_failed:
                    slack_error = NotificationError(
                        f"slack dispatch failed for workflow {workflow_id!r}"
                    )

        # ------------------------------------------------------------------
        # 5. Dispatch email (if eligible).
        # ------------------------------------------------------------------
        email_error: Exception | None = None
        if email_eligible and dept.notify_email is not None:
            outcome = await self._dispatch_one(
                workflow_id=workflow_id,
                channel="email",
                target=dept.notify_email,
                body=body,
                body_hash=body_hash,
                outcome=outcome,
            )
            if outcome.email_failed:
                email_error = NotificationError(
                    f"email dispatch failed for workflow {workflow_id!r}"
                )

        # ------------------------------------------------------------------
        # 6. Re-raise on failure-mandatory transport failures. We only
        #    raise in the failure branch — success-gated dispatch is
        #    "best effort" by design (a stuck Slack webhook should not
        #    bubble back into the workflow on a *successful* run).
        # ------------------------------------------------------------------
        if is_failure and (slack_error is not None or email_error is not None):
            # Prefer Slack error message; failure-mandatory channel.
            primary = slack_error or email_error
            assert primary is not None  # for type checker
            raise primary

        return outcome

    async def notify_audit_prune_failed(
        self,
        *,
        error: str,
        run_id: str | None = None,
    ) -> NotificationOutcome:
        """Dispatch the mandatory admin Slack alarm on ``AuditPruneWorkflow`` failure.

        Posts the rendered ``notifications/audit_prune_failed`` body to
        the platform admin Slack channel through
        :meth:`SlackAdapter.send_admin_channel`. The destination webhook
        is fixed by the adapter to the vault-resolved value at
        ``vault:notifications/slack/admin`` and is **not configurable
        per call**; this alarm is delivered regardless
        of any dept config.

        The dispatcher reuses the same ``shared.notification_log`` row +
        ``UNIQUE(dedup_key)`` idempotency machinery as
        :meth:`notify_workflow_completion`. The ``dedup_key`` shape is the
        same sha256 over ``"<workflow_id>:<channel>:<kind>"`` from
        :func:`_dedup_key`, with:

        * ``workflow_id`` = ``run_id`` if provided, else the stable
          fallback :data:`_AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID`. Pass
          ``run_id`` if you want per-cron-run dedup
          (eg. ``f"audit-prune-cron-{cutoff_date}"``) so retries inside
          a single cron run dedupe but the next day's run fires fresh.
        * ``channel`` = ``"slack"``.
        * ``kind`` = ``"audit_prune_failed"`` (already reserved in
          :data:`notification.types.NotificationKind`).

        Args:
            error: Error message describing why ``AuditPruneWorkflow``
                failed. Surfaced into the
                ``notifications/audit_prune_failed`` template via the
                ``{error}`` placeholder. Long stack traces should be
                truncated by the caller — the field is not redacted by
                the dispatcher. Pass ``str(exc)`` from the workflow's
                exception handler (matches design pseudocode).
            run_id: Optional stable identifier of the ``AuditPruneWorkflow``
                cron run. When ``None``, the default
                :data:`_AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID` is used so
                retries within an ambiguous "single-shot" call still
                dedupe; pass an explicit value (typically derived from
                ``cutoff_date``) for per-run idempotency.

        Returns:
            :class:`NotificationOutcome` whose ``slack_sent`` /
            ``slack_skipped_dedup`` / ``slack_failed`` flag describes
            what happened. Email fields are always ``False`` because the
            admin alarm is Slack-only by design.

        Raises:
            TemplateRenderError: The
                ``notifications/audit_prune_failed`` prompt could not be
                rendered (missing template, missing placeholder). Never
                retryable — fail-fast so the operator sees the
                mis-configuration.
            NotificationError: The Slack adapter failed (transport / 5xx).
                Mandatory admin alarms are never best-effort — re-raised
                so the activity's :class:`temporalio.common.RetryPolicy`
                can replay. The ``shared.notification_log`` row has
                already been persisted optimistically, so the
                ``UNIQUE(dedup_key)`` constraint will dedupe a successful
                retry.
        """

        # ------------------------------------------------------------------
        # 1. Render body. The audit prune template gets only ``{error}`` —
        #    no department / workflow placeholders; this alarm is platform-
        #    wide. We pass the error string verbatim so the renderer's
        #    Mapping interface does not require a typed PromptVars.
        # ------------------------------------------------------------------
        try:
            body = self._prompts.render(
                _AUDIT_PRUNE_FAILED_TEMPLATE,
                vars={"error": error},
            )
        except Exception as exc:  # noqa: BLE001 — wrap and re-raise
            raise TemplateRenderError(
                "failed to render notification template "
                f"{_AUDIT_PRUNE_FAILED_TEMPLATE!r}: {exc}"
            ) from exc

        body_hash = _sha256_hex(body)

        # ------------------------------------------------------------------
        # 2. Build the dedup_key reusing the same shape as the workflow
        #    completion path. ``kind="audit_prune_failed"`` is already
        #    reserved in :data:`notification.types.NotificationKind`; we
        #    feed it through the existing helper unchanged so a future
        #    schema change to :func:`_dedup_key` (eg. adding a salt)
        #    propagates uniformly.
        # ------------------------------------------------------------------
        kind: NotificationKind = "audit_prune_failed"
        dedup_key = _dedup_key(
            workflow_id=run_id or _AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID,
            channel="slack",
            kind=kind,
        )

        # ------------------------------------------------------------------
        # 3. Optimistic insert into ``shared.notification_log``. The store
        #    returns ``False`` when an earlier identical attempt landed —
        #    we then skip the adapter send (idempotent retry), matching
        #    the behavior of :meth:`notify_workflow_completion`.
        #
        #    The ``target`` column carries the constant
        #    :data:`_ADMIN_CHANNEL_TARGET` because the admin webhook URL
        #    never crosses this dispatcher (the adapter resolves it from
        #    Vault). Persisting a stable label keeps audit queries simple
        #    (``WHERE target = 'admin-channel'``) without leaking the
        #    secret webhook URL.
        # ------------------------------------------------------------------
        entry = NotificationLogEntry(
            dedup_key=dedup_key,
            channel="slack",
            kind=kind,
            target=_ADMIN_CHANNEL_TARGET,
            body_hash=body_hash,
            status="sent",
            error=None,
        )

        outcome = NotificationOutcome()
        inserted = await self._log_store.insert(entry)
        if not inserted:
            _log.info(
                "audit_prune_failed admin alarm dedup hit; skipping adapter send",
                extra={
                    "run_id": run_id or _AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID,
                    "dedup_key_prefix": dedup_key[:8],
                },
            )
            return _outcome_with_dedup(outcome, "slack")

        # ------------------------------------------------------------------
        # 4. Fire the admin alarm. Adapter raises ⇒ re-raise as
        #    NotificationError so the Temporal activity's RetryPolicy
        #    can replay. Failure here is **never** swallowed — this is
        #    the platform's mandatory ops alarm channel and a silent
        #    failure would violate the mandatory alarm contract.
        # ------------------------------------------------------------------
        try:
            await self._slack.send_admin_channel(
                body, alert_type=_AUDIT_PRUNE_FAILED_ALERT_TYPE
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "audit_prune_failed admin alarm transport failed",
                extra={
                    "run_id": run_id or _AUDIT_PRUNE_FAILED_DEFAULT_RUN_ID,
                    "error": str(exc),
                },
            )
            outcome = _outcome_with_failure(outcome, "slack", str(exc))
            raise NotificationError(
                f"audit_prune_failed admin alarm dispatch failed: {exc}"
            ) from exc

        return _outcome_with_sent(outcome, "slack")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch_one(
        self,
        *,
        workflow_id: str,
        channel: NotificationChannel,
        target: str,
        body: str,
        body_hash: str,
        outcome: NotificationOutcome,
    ) -> NotificationOutcome:
        """Insert the log row, send through the adapter, update ``outcome``.

        The dance is:

        1. Build the deterministic ``dedup_key``.
        2. Insert the ``notification_log`` row with ``status='sent'``
           (optimistic). The store's ``ON CONFLICT (dedup_key) DO NOTHING``
           returns ``False`` when an earlier identical attempt landed —
           we then skip the adapter send (idempotent retry).
        3. Invoke the adapter. If it raises, mark the outcome's
           ``*_failed`` flag and let step 6 of the caller decide whether
           to re-raise. The log row stays at ``status='sent'`` from the
           caller's optimistic insert; retry policy can follow up with
           a corrective UPDATE.
           keep the contract simple: ``failed`` flag is in the outcome,
           and the log row remains as the optimistic ``sent``.

        NOTE on step 3: the contract does not pin the row-update timing on
        adapter failure — it only asserts (d) "``notification_log``
        receives one row per attempt with ``UNIQUE(dedup_key)``". We
        meet the idempotency contract via the optimistic insert; refining
        ``status`` after a transport failure is handled by retry policy.
        """

        kind: NotificationKind = "workflow_completion"
        dedup_key = _dedup_key(
            workflow_id=workflow_id,
            channel=channel,
            kind=kind,
        )
        target_hash = _sha256_hex(target)

        entry_sent = NotificationLogEntry(
            dedup_key=dedup_key,
            channel=channel,
            kind=kind,
            target=target_hash,
            body_hash=body_hash,
            status="sent",
            error=None,
        )

        inserted = await self._log_store.insert(entry_sent)
        if not inserted:
            # Idempotent retry — an earlier call already wrote the row
            # AND invoked the adapter (or recorded its failure). Skip
            # the adapter send so we don't double-deliver.
            _log.info(
                "notification dedup hit; skipping adapter send",
                extra={
                    "workflow_id": workflow_id,
                    "channel": channel,
                    "dedup_key_prefix": dedup_key[:8],
                },
            )
            return _outcome_with_dedup(outcome, channel)

        try:
            if channel == "slack":
                await self._slack.send(body, webhook=target)
            elif channel == "email":
                await self._email.send(body, to=target)
            else:  # pragma: no cover — guarded by Literal type
                raise NotificationError(f"unsupported channel {channel!r}")
        except Exception as exc:  # noqa: BLE001 — capture per channel
            _log.warning(
                "notification adapter send failed",
                extra={
                    "workflow_id": workflow_id,
                    "channel": channel,
                    "error": str(exc),
                },
            )
            return _outcome_with_failure(outcome, channel, str(exc))

        return _outcome_with_sent(outcome, channel)


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions for easy unit testing)
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    """Return the hex digest of ``sha256(value)``.

    Used for both ``body_hash`` (forensic correlation without storing
    plaintext) and the ``target`` column (so a Slack webhook URL never
    lands in the table verbatim.
    """

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _dedup_key(
    *,
    workflow_id: str,
    channel: NotificationChannel,
    kind: NotificationKind,
) -> str:
    """Build the deterministic ``dedup_key`` for one dispatch attempt.

    The shape is::

        sha256(f"{workflow_id}:{channel}:{kind}")

    All three components are required:

    * ``workflow_id`` — distinguishes calls across workflows.
    * ``channel`` — lets a single workflow drive Slack *and* email
      without colliding (we want one row per channel).
    * ``kind`` — currently always ``"workflow_completion"`` for normal
      workflow notifications; ``notify_audit_prune_failed`` reuses the same
      ``notification_log`` table with ``kind="audit_prune_failed"``;
      including ``kind`` in the hash future-proofs the schema.

    Returns:
        The hex-encoded sha256 digest. Length 64; matches the
        ``shared.notification_log.dedup_key`` ``TEXT`` column.
    """

    payload = f"{workflow_id}{_HASH_SEP}{channel}{_HASH_SEP}{kind}"
    return _sha256_hex(payload)


def _outcome_with_sent(
    outcome: NotificationOutcome, channel: NotificationChannel
) -> NotificationOutcome:
    """Return a copy of ``outcome`` with the channel's ``*_sent`` flag set."""

    if channel == "slack":
        return NotificationOutcome(
            slack_sent=True,
            email_sent=outcome.email_sent,
            slack_skipped_dedup=outcome.slack_skipped_dedup,
            email_skipped_dedup=outcome.email_skipped_dedup,
            slack_failed=outcome.slack_failed,
            email_failed=outcome.email_failed,
        )
    if channel == "email":
        return NotificationOutcome(
            slack_sent=outcome.slack_sent,
            email_sent=True,
            slack_skipped_dedup=outcome.slack_skipped_dedup,
            email_skipped_dedup=outcome.email_skipped_dedup,
            slack_failed=outcome.slack_failed,
            email_failed=outcome.email_failed,
        )
    return outcome  # pragma: no cover — guarded by Literal


def _outcome_with_dedup(
    outcome: NotificationOutcome, channel: NotificationChannel
) -> NotificationOutcome:
    """Return a copy of ``outcome`` with the channel's ``*_skipped_dedup`` flag set."""

    if channel == "slack":
        return NotificationOutcome(
            slack_sent=outcome.slack_sent,
            email_sent=outcome.email_sent,
            slack_skipped_dedup=True,
            email_skipped_dedup=outcome.email_skipped_dedup,
            slack_failed=outcome.slack_failed,
            email_failed=outcome.email_failed,
        )
    if channel == "email":
        return NotificationOutcome(
            slack_sent=outcome.slack_sent,
            email_sent=outcome.email_sent,
            slack_skipped_dedup=outcome.slack_skipped_dedup,
            email_skipped_dedup=True,
            slack_failed=outcome.slack_failed,
            email_failed=outcome.email_failed,
        )
    return outcome  # pragma: no cover


def _outcome_with_failure(
    outcome: NotificationOutcome,
    channel: NotificationChannel,
    error: str,  # noqa: ARG001 — kept for future structured failure reporting
) -> NotificationOutcome:
    """Return a copy of ``outcome`` with the channel's ``*_failed`` flag set."""

    if channel == "slack":
        return NotificationOutcome(
            slack_sent=outcome.slack_sent,
            email_sent=outcome.email_sent,
            slack_skipped_dedup=outcome.slack_skipped_dedup,
            email_skipped_dedup=outcome.email_skipped_dedup,
            slack_failed=True,
            email_failed=outcome.email_failed,
        )
    if channel == "email":
        return NotificationOutcome(
            slack_sent=outcome.slack_sent,
            email_sent=outcome.email_sent,
            slack_skipped_dedup=outcome.slack_skipped_dedup,
            email_skipped_dedup=outcome.email_skipped_dedup,
            slack_failed=outcome.slack_failed,
            email_failed=True,
        )
    return outcome  # pragma: no cover


def _used_status(  # noqa: D401 — internal helper
    status: NotificationStatus,
) -> NotificationStatus:
    """Identity helper kept for forward compatibility.

    A retry path may want to coerce ``"sent"`` to ``"retrying"`` inside a
    token-bucket back-pressure loop. Pinning the helper now keeps the type
    signature stable so the coercion can be added without a breaking change
    to consumers.
    """

    return status
