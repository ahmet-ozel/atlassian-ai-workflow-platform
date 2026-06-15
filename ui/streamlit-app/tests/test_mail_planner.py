from __future__ import annotations

import pytest

import mail_planner
from mail_planner import plan_and_call_mail_mcp, plan_mail_mcp_candidates


def test_latest_mail_maps_to_list_candidates() -> None:
    candidates = plan_mail_mcp_candidates("son 5 maili listele", ["gmail"])

    assert candidates[0] == ("gmail", "gmail_list_messages", {"limit": 5})


def test_unread_mail_maps_to_unread_search() -> None:
    candidates = plan_mail_mcp_candidates("okunmamis mailleri getir", ["outlook"])

    assert candidates[0][0] == "outlook"
    assert candidates[0][1] == "outlook_list_unread_messages"
    assert candidates[0][2]["unread"] is True
    assert candidates[0][2]["query"] == "is:unread"


def test_sender_search_prefers_email_address() -> None:
    candidates = plan_mail_mcp_candidates(
        "alice@example.com adresinden gelen mailleri ara",
        ["gmail"],
    )

    assert candidates[0][1] == "gmail_search_messages"
    assert candidates[0][2]["from"] == "alice@example.com"
    assert candidates[0][2]["query"] == "from:alice@example.com"


def test_subject_search_extracts_subject() -> None:
    candidates = plan_mail_mcp_candidates("konu: fatura onayi ara", ["outlook"])

    assert candidates[0][1] == "outlook_search_messages"
    assert candidates[0][2]["subject"] == "fatura onayi"
    assert candidates[0][2]["query"] == "subject:fatura onayi"


def test_detail_requires_message_id() -> None:
    with pytest.raises(ValueError, match="message id"):
        plan_mail_mcp_candidates("mail detayini getir", ["gmail"])


def test_detail_maps_to_get_message_candidates() -> None:
    candidates = plan_mail_mcp_candidates("mail id: abc12345 detayini getir", ["gmail"])

    assert candidates[0][1] == "gmail_get_message"
    assert candidates[0][2]["message_id"] == "abc12345"
    assert candidates[0][2]["include_body"] is True


def test_write_intent_is_rejected() -> None:
    with pytest.raises(ValueError, match="read-only"):
        plan_mail_mcp_candidates("bu maili sil", ["gmail"])


def test_provider_mention_changes_order() -> None:
    candidates = plan_mail_mcp_candidates("outlook son mailleri getir")

    assert candidates[0][0] == "outlook"


def test_plan_and_call_uses_mail_mcp_call_any(monkeypatch) -> None:
    captured = {}

    def fake_call_any(candidates):
        captured["candidates"] = candidates
        return "gmail", "gmail_list_messages", {"ok": True}

    monkeypatch.setattr(mail_planner, "mail_mcp_call_any", fake_call_any)

    result = plan_and_call_mail_mcp("son mailleri getir", ["gmail"])

    assert result == ("gmail", "gmail_list_messages", {"ok": True})
    assert captured["candidates"][0][1] == "gmail_list_messages"
