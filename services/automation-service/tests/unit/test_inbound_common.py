"""Unit tests for :mod:`automation_service.inbound.common`.

Validates: Requirement 5.10 (Slack/Email-to-task adapter B19, task 8.5).

These tests cover the deterministic core of the inbound adapter chain:

* :func:`build_inbound_workflow_id` — channel-discriminated, normalised,
  deterministic.
* :func:`auto_assign_workflow_input` — flips ``auto_assign`` and
  ``smart_defaults`` to ``True`` (the standard task-creator path).
* :func:`verify_slack_signature` — rejects tampered bodies, stale
  timestamps, malformed headers; accepts Slack's exact contract.
* :func:`extract_slack_command_text` — strips the ``<@USER>`` prefix
  and surrounding whitespace.
* :class:`InboundTaskRequest` validation — empty fields raise
  ``ValueError`` so adapters fail fast on bad inputs.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_jira_comment / test_budget_policy.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


from automation_service.inbound.common import (  # noqa: E402
    InboundTaskRequest,
    SLACK_TIMESTAMP_TOLERANCE_S,
    auto_assign_workflow_input,
    build_inbound_workflow_id,
    extract_slack_command_text,
    verify_slack_signature,
)


# ---------------------------------------------------------------------------
# Workflow id formatter
# ---------------------------------------------------------------------------


class TestBuildInboundWorkflowId:
    def test_slack_id_is_channel_prefixed(self) -> None:
        wid = build_inbound_workflow_id("slack", "1700000000.000123")
        assert wid.startswith("automation-inbound-slack-")
        assert wid == "automation-inbound-slack-1700000000-000123"

    def test_email_id_is_channel_prefixed(self) -> None:
        wid = build_inbound_workflow_id("email", "<abc@example.com>")
        assert wid.startswith("automation-inbound-email-")
        assert wid == "automation-inbound-email-abc-example-com"

    def test_same_inputs_yield_same_id(self) -> None:
        a = build_inbound_workflow_id("slack", "C123.456")
        b = build_inbound_workflow_id("slack", "C123.456")
        assert a == b

    def test_different_channels_disambiguate(self) -> None:
        a = build_inbound_workflow_id("slack", "x")
        b = build_inbound_workflow_id("email", "x")
        assert a != b

    def test_non_alphanumerics_collapse_to_dashes(self) -> None:
        wid = build_inbound_workflow_id("slack", "Foo / Bar @ baz!")
        assert wid == "automation-inbound-slack-foo-bar-baz"

    def test_all_special_external_id_falls_back_to_hash(self) -> None:
        wid = build_inbound_workflow_id("email", "@@@")
        assert wid.startswith("automation-inbound-email-")
        # Hash fallback uses the first 16 hex chars of SHA-256.
        suffix = wid.removeprefix("automation-inbound-email-")
        expected = hashlib.sha256(b"@@@").hexdigest()[:16]
        assert suffix == expected

    def test_empty_external_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_inbound_workflow_id("slack", "")

    def test_invalid_channel_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_inbound_workflow_id("teams", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Workflow input shape
# ---------------------------------------------------------------------------


class TestAutoAssignWorkflowInput:
    def _request(self, **overrides: object) -> InboundTaskRequest:
        kwargs = dict(
            channel="slack",
            external_id="abc",
            dept_id="payment",
            actor_handle="U07ABC",
            intent_text="open a ticket",
            title_hint=None,
        )
        kwargs.update(overrides)
        return InboundTaskRequest(**kwargs)  # type: ignore[arg-type]

    def test_includes_auto_assign_and_smart_defaults_true(self) -> None:
        req = self._request()
        out = auto_assign_workflow_input(req)
        assert out["auto_assign"] is True
        assert out["smart_defaults"] is True

    def test_trigger_is_inbound_channel_prefix(self) -> None:
        slack_out = auto_assign_workflow_input(self._request(channel="slack"))
        email_out = auto_assign_workflow_input(
            self._request(channel="email", actor_handle="alice@example.com")
        )
        assert slack_out["trigger"] == "inbound_slack"
        assert email_out["trigger"] == "inbound_email"

    def test_dept_id_propagated_under_department_id_key(self) -> None:
        out = auto_assign_workflow_input(self._request(dept_id="ops"))
        assert out["department_id"] == "ops"

    def test_title_hint_omitted_when_none(self) -> None:
        out = auto_assign_workflow_input(self._request(title_hint=None))
        assert "title_hint" not in out

    def test_title_hint_included_when_provided(self) -> None:
        out = auto_assign_workflow_input(
            self._request(title_hint="Subject: please help")
        )
        assert out["title_hint"] == "Subject: please help"


# ---------------------------------------------------------------------------
# InboundTaskRequest validation
# ---------------------------------------------------------------------------


class TestInboundTaskRequestValidation:
    def test_empty_external_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            InboundTaskRequest(
                channel="slack",
                external_id="",
                dept_id="ops",
                actor_handle="U1",
                intent_text="hello",
            )

    def test_empty_dept_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            InboundTaskRequest(
                channel="slack",
                external_id="abc",
                dept_id="",
                actor_handle="U1",
                intent_text="hello",
            )

    def test_empty_actor_handle_rejected(self) -> None:
        with pytest.raises(ValueError):
            InboundTaskRequest(
                channel="slack",
                external_id="abc",
                dept_id="ops",
                actor_handle="",
                intent_text="hello",
            )

    def test_invalid_channel_rejected(self) -> None:
        with pytest.raises(ValueError):
            InboundTaskRequest(
                channel="webex",  # type: ignore[arg-type]
                external_id="abc",
                dept_id="ops",
                actor_handle="U1",
                intent_text="hello",
            )

    def test_empty_intent_text_is_allowed(self) -> None:
        """Adapters decide whether empty intent should produce a workflow.

        The dataclass itself only enforces structural correctness; the
        Slack route emits ``inbound_empty_mention`` and short-circuits
        before the dataclass is constructed in that branch, so an
        empty-but-string ``intent_text`` is accepted here.
        """

        req = InboundTaskRequest(
            channel="slack",
            external_id="abc",
            dept_id="ops",
            actor_handle="U1",
            intent_text="",
        )
        assert req.intent_text == ""


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------


_SECRET = b"slack-signing-secret-dev-only"
_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _sign(secret: bytes, ts: str, body: bytes) -> str:
    base = f"v0:{ts}:".encode("utf-8") + body
    return "v0=" + hmac.new(secret, base, hashlib.sha256).hexdigest()


class TestVerifySlackSignature:
    def test_valid_signature_accepted(self) -> None:
        body = b'{"event":"hi"}'
        ts = str(int(_NOW.timestamp()))
        sig = _sign(_SECRET, ts, body)
        assert verify_slack_signature(
            secret=_SECRET,
            timestamp=ts,
            raw_body=body,
            signature=sig,
            now=_NOW,
        )

    def test_tampered_body_rejected(self) -> None:
        body = b'{"event":"hi"}'
        ts = str(int(_NOW.timestamp()))
        sig = _sign(_SECRET, ts, body)
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp=ts,
            raw_body=body + b"!",
            signature=sig,
            now=_NOW,
        )

    def test_stale_timestamp_rejected(self) -> None:
        body = b"payload"
        old_ts = str(int(_NOW.timestamp()) - SLACK_TIMESTAMP_TOLERANCE_S - 1)
        sig = _sign(_SECRET, old_ts, body)
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp=old_ts,
            raw_body=body,
            signature=sig,
            now=_NOW,
        )

    def test_future_timestamp_outside_tolerance_rejected(self) -> None:
        body = b"payload"
        future_ts = str(int(_NOW.timestamp()) + SLACK_TIMESTAMP_TOLERANCE_S + 1)
        sig = _sign(_SECRET, future_ts, body)
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp=future_ts,
            raw_body=body,
            signature=sig,
            now=_NOW,
        )

    def test_missing_v0_prefix_rejected(self) -> None:
        body = b"payload"
        ts = str(int(_NOW.timestamp()))
        # Strip the ``v0=`` prefix from a valid signature.
        valid = _sign(_SECRET, ts, body)
        bare_hex = valid.split("=", 1)[1]
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp=ts,
            raw_body=body,
            signature=bare_hex,
            now=_NOW,
        )

    def test_empty_secret_rejected(self) -> None:
        body = b"payload"
        ts = str(int(_NOW.timestamp()))
        sig = _sign(_SECRET, ts, body)
        assert not verify_slack_signature(
            secret=b"",
            timestamp=ts,
            raw_body=body,
            signature=sig,
            now=_NOW,
        )

    def test_non_integer_timestamp_rejected(self) -> None:
        body = b"payload"
        sig = "v0=" + "0" * 64
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp="not-a-number",
            raw_body=body,
            signature=sig,
            now=_NOW,
        )

    def test_wrong_secret_rejected(self) -> None:
        body = b"payload"
        ts = str(int(_NOW.timestamp()))
        sig = _sign(b"different-secret", ts, body)
        assert not verify_slack_signature(
            secret=_SECRET,
            timestamp=ts,
            raw_body=body,
            signature=sig,
            now=_NOW,
        )


# ---------------------------------------------------------------------------
# Slack mention extraction
# ---------------------------------------------------------------------------


class TestExtractSlackCommandText:
    def test_strips_user_mention_prefix(self) -> None:
        out = extract_slack_command_text(
            "<@U07ABCDEF> open a ticket for the API"
        )
        assert out == "open a ticket for the API"

    def test_strips_user_mention_with_display_name(self) -> None:
        out = extract_slack_command_text("<@U07ABCDEF|alice> fix login bug")
        assert out == "fix login bug"

    def test_no_mention_returns_trimmed_text(self) -> None:
        out = extract_slack_command_text("  just a plain message  ")
        assert out == "just a plain message"

    def test_only_mention_returns_empty(self) -> None:
        out = extract_slack_command_text("<@U07ABCDEF>")
        assert out == ""

    def test_idempotent_on_already_stripped(self) -> None:
        a = extract_slack_command_text("<@U1> hello")
        b = extract_slack_command_text(a)
        assert a == b == "hello"

    def test_non_string_input_raises(self) -> None:
        with pytest.raises(TypeError):
            extract_slack_command_text(None)  # type: ignore[arg-type]
