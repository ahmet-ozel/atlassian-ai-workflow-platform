"""Intent planner for read-only Streamlit Mail Chat requests."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

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
    r"\b(?:detay\w*|detail\w*|ozet\w*|özet\w*|read|hakkinda|bilgi|anlat|incele)\b|\boku\b",
    re.IGNORECASE,
)


_SENDER_FROM_PHRASE_RE = re.compile(
    r"\b([A-Za-z0-9_.@+\-\s]{2,80}?)['’]?(?:den|dan|ten|tan)\s+gelen",
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


def _extract_offset(text: str) -> int:
    folded = _fold_text(text)
    ordinal_words = {
        "ilk": 1,
        "birinci": 1,
        "ikinci": 2,
        "ucuncu": 3,
        "dorduncu": 4,
        "besinci": 5,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
    }
    for word, value in ordinal_words.items():
        if re.search(rf"\b{re.escape(word)}(?:yi|i|nu|u)?\b", folded):
            return value
    match = re.search(
        r"\b(\d{1,2})(?:\.|inci|nci|nd|rd|th)?\s*(?:son\s*)?(?:mail|email|mesaj|message)?",
        folded,
    )
    if match:
        return max(1, min(int(match.group(1)), _MAX_LIMIT))
    return 1


def _clean_query(value: str) -> str:
    value = re.split(
        r"\b(?:son|ilk|top|limit|ozetle|özetle|detay|oku|getir|listele|ara|search|bul)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(
        r"\b(?:olan|olanlari|olanları|olanlar|mailleri|mailler|mail|email)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(value.strip(" \t\r\n,.;'\"").split())


def _extract_sender(text: str) -> str:
    email_match = _EMAIL_RE.search(text)
    if email_match:
        return email_match.group(0)
    match = _FROM_RE.search(text)
    if match:
        return _clean_query(match.group(1))
    phrase_match = _SENDER_FROM_PHRASE_RE.search(_fold_text(text))
    return _clean_query(phrase_match.group(1)) if phrase_match else ""


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


def _asks_latest_single_detail(text: str) -> bool:
    folded = _fold_text(text)
    if any(marker in folded for marker in ("liste", "list", "ara", "search", "bul")):
        return False
    if re.search(r"\b(?:son|ilk|top|limit)\s*[:=]?\s*\d{1,2}\b", folded):
        return False
    if any(marker in folded for marker in ("mailler", "emails", "messages", "mesajlar")):
        return False
    asks_latest = any(
        marker in folded
        for marker in ("son", "en son", "latest", "last", "recent", "yeni")
    )
    asks_mail = any(
        marker in folded
        for marker in ("mail", "email", "e-mail", "eposta", "e-posta", "mesaj", "message")
    )
    asks_open = any(
        marker in folded
        for marker in ("goster", "show", "ac", "open", "goruntule", "bak")
    )
    return asks_latest and asks_mail and asks_open


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
            f"{provider}_search_messages",
        ),
        "unread": (
            f"{provider}_list_unread_messages",
            f"{provider}_search_messages",
            f"{provider}_list_messages",
        ),
        "search": (
            f"{provider}_search_messages",
        ),
        "detail": (
            f"{provider}_get_message",
        ),
        "latest_detail": (
            f"{provider}_get_latest_message",
            f"{provider}_get_message",
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

    if _DETAIL_RE.search(folded) or _asks_latest_single_detail(text):
        message_id = _extract_message_id(text)
        if message_id:
            return _expand(
                provider_order,
                "detail",
                {"message_id": message_id, "id": message_id, "include_body": True},
            )

        args: dict[str, Any] = {
            "offset": _extract_offset(text),
            "include_body": True,
        }
        if "okunmamis" in folded or "unread" in folded:
            args["unread"] = True
            args["query"] = "is:unread"
        if any(
            marker in folded
            for marker in (
                "gelen mail",
                "gelen email",
                "gelen mesaj",
                "gelen kutusu",
                "inbox",
            )
        ):
            args["inbox"] = True

        sender = _extract_sender(text)
        if sender:
            args["from"] = sender
            args["query"] = f"from:{sender}"

        subject = _extract_subject(text)
        if subject:
            args["subject"] = subject
            args["query"] = f"subject:{subject}"

        return _expand(provider_order, "latest_detail", args)

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
    credential_refs: Mapping[MailProvider, str] | None = None,
) -> tuple[MailProvider, str, Any]:
    """Plan and execute a read-only mail MCP request."""

    candidates = plan_mail_mcp_candidates(text, providers)
    if credential_refs is None:
        return mail_mcp_call_any(candidates)
    return mail_mcp_call_any(candidates, credential_refs=credential_refs)
