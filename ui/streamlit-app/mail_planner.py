"""Intent planner for read-only Streamlit Mail Chat requests."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence

from mail_mcp import MailProvider, MailToolCandidate, mail_mcp_call_any


_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_WRITE_INTENT_RE = re.compile(
    r"\b(?:send|gonder|gönder|sil|delete|arsiv|archive|reply|yanitla|"
    r"cevapla|forward|ilet|taslak|draft|move|tasi|taşı|mark|isaretle)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_MESSAGE_ID_RE = re.compile(
    r"\b(?:id|message id|mail id|email id|mesaj id)\s*[:#=]?\s*([A-Za-z0-9_.@:-]{4,})",
    re.IGNORECASE,
)
_SUBJECT_RE = re.compile(
    r"\b(?:subject|konu(?:su)?|baslik|başlık)\s*[:=]?\s+(.+)",
    re.IGNORECASE,
)
_FROM_RE = re.compile(
    r"\b(?:from|sender|gonderen|gönderen|kimden)\s*[:=]?\s+(.+)",
    re.IGNORECASE,
)
_DETAIL_RE = re.compile(
    r"\b(?:detay\w*|detail\w*|ozet\w*|özet\w*|read)\b|\boku\b",
    re.IGNORECASE,
)


def _fold_text(text: str) -> str:
    replacements = str.maketrans(
        {
            "\u0130": "i",
            "\u0131": "i",
            "\u015e": "s",
            "\u015f": "s",
            "\u011e": "g",
            "\u011f": "g",
            "\u00c7": "c",
            "\u00e7": "c",
            "\u00d6": "o",
            "\u00f6": "o",
            "\u00dc": "u",
            "\u00fc": "u",
        }
    )
    normalized = unicodedata.normalize("NFKD", text.translate(replacements))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def _provider_order(
    text: str,
    providers: Sequence[MailProvider] | None,
) -> list[MailProvider]:
    available = list(providers or ("gmail", "outlook"))
    folded = _fold_text(text)
    if "outlook" in folded or "office" in folded:
        preferred: MailProvider = "outlook"
    elif "gmail" in folded or "google" in folded:
        preferred = "gmail"
    else:
        return available
    return [preferred, *[provider for provider in available if provider != preferred]]


def _extract_limit(text: str, default: int = _DEFAULT_LIMIT) -> int:
    folded = _fold_text(text)
    match = re.search(r"\b(?:son|ilk|top|limit)\s*[:=]?\s*(\d{1,2})\b", folded)
    if not match:
        return default
    return max(1, min(int(match.group(1)), _MAX_LIMIT))


def _clean_query(value: str) -> str:
    value = re.split(
        r"\b(?:son|ilk|top|limit|ozetle|özetle|detay|oku|getir|listele|ara|search|bul)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return " ".join(value.strip(" \t\r\n,.;'\"").split())


def _extract_sender(text: str) -> str:
    email_match = _EMAIL_RE.search(text)
    if email_match:
        return email_match.group(0)
    match = _FROM_RE.search(text)
    return _clean_query(match.group(1)) if match else ""


def _extract_subject(text: str) -> str:
    match = _SUBJECT_RE.search(text)
    if match:
        return _clean_query(match.group(1))
    folded = _fold_text(text)
    if "konu" in folded or "subject" in folded:
        cleaned = re.sub(
            r"\b(?:gmail|outlook|mail|email|eposta|e-posta|konu|subject|ara|search|bul)\b",
            " ",
            folded,
        )
        return " ".join(cleaned.split())
    return ""


def _extract_message_id(text: str) -> str:
    match = _MESSAGE_ID_RE.search(text)
    if match:
        return match.group(1).strip(" \t\r\n,.;'\"")
    return ""


def _search_query(text: str) -> str:
    folded = _fold_text(text)
    cleaned = re.sub(
        r"\b(?:gmail|outlook|mail|email|eposta|e-posta|ara|search|bul|"
        r"listele|getir|son|ilk|okunmamis|okunmamış|unread|detay|ozet|özet)\b",
        " ",
        folded,
    )
    cleaned = re.sub(r"\b\d{1,2}\b", " ", cleaned)
    return " ".join(cleaned.split())


def _candidate_names(provider: MailProvider, intent: str) -> tuple[str, ...]:
    names: dict[str, tuple[str, ...]] = {
        "list": (
            f"{provider}_list_messages",
            f"{provider}_list_emails",
            f"{provider}_search_messages",
            "list_messages",
            "search_messages",
        ),
        "unread": (
            f"{provider}_list_unread_messages",
            f"{provider}_search_messages",
            f"{provider}_list_messages",
            "list_unread_messages",
            "search_messages",
        ),
        "search": (
            f"{provider}_search_messages",
            f"{provider}_search_emails",
            "search_messages",
            "search_emails",
        ),
        "detail": (
            f"{provider}_get_message",
            f"{provider}_get_email",
            f"{provider}_read_message",
            "get_message",
            "read_message",
        ),
    }
    return names[intent]


def _expand(
    providers: Sequence[MailProvider],
    intent: str,
    args: dict[str, Any],
) -> list[MailToolCandidate]:
    return [
        (provider, tool_name, dict(args))
        for provider in providers
        for tool_name in _candidate_names(provider, intent)
    ]


def plan_mail_mcp_candidates(
    text: str,
    providers: Sequence[MailProvider] | None = None,
) -> list[MailToolCandidate]:
    """Map a user mail request to read-only MCP tool candidates."""

    if _WRITE_INTENT_RE.search(text):
        raise ValueError(
            "Mail Chat MVP read-only calisir; gonderme, silme, arsivleme "
            "veya tasima islemleri desteklenmiyor."
        )

    provider_order = _provider_order(text, providers)
    if not provider_order:
        raise ValueError("Kullanilabilir Gmail/Outlook MCP provider yok.")

    folded = _fold_text(text)
    limit = _extract_limit(text)

    if _DETAIL_RE.search(folded):
        message_id = _extract_message_id(text)
        if not message_id:
            raise ValueError(
                "Mail detayini/ozetini getirmek icin message id gerekli. "
                "Ornek: mail id: abc123 detayini getir."
            )
        return _expand(
            provider_order,
            "detail",
            {"message_id": message_id, "id": message_id, "include_body": True},
        )

    sender = _extract_sender(text)
    if sender:
        return _expand(
            provider_order,
            "search",
            {"query": f"from:{sender}", "from": sender, "limit": limit},
        )

    subject = _extract_subject(text)
    if subject:
        return _expand(
            provider_order,
            "search",
            {"query": f"subject:{subject}", "subject": subject, "limit": limit},
        )

    if "okunmamis" in folded or "unread" in folded:
        return _expand(
            provider_order,
            "unread",
            {"query": "is:unread", "unread": True, "limit": limit},
        )

    query = _search_query(text)
    if query and any(word in folded for word in ("ara", "search", "bul")):
        return _expand(provider_order, "search", {"query": query, "limit": limit})

    return _expand(provider_order, "list", {"limit": limit})


def plan_and_call_mail_mcp(
    text: str,
    providers: Sequence[MailProvider] | None = None,
) -> tuple[MailProvider, str, Any]:
    """Plan and execute a read-only mail MCP request."""

    return mail_mcp_call_any(plan_mail_mcp_candidates(text, providers))
