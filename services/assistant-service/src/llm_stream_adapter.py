"""Adapters for sync LLM providers used by the assistant chat stream."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any, AsyncIterator, Mapping, Sequence

from messages import Message
from llm_orchestrator.orchestrator import ProviderChunk


class StreamingProviderAdapter:
    """Expose ``stream(...)`` for providers that only implement ``complete``."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def downtime(self) -> int:
        fn = getattr(self._provider, "downtime", None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001
                return 0
        return 0

    async def stream(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
    ) -> AsyncIterator[ProviderChunk]:
        heuristic_call = _heuristic_tool_call(history, tools)
        if heuristic_call is not None:
            yield ProviderChunk(
                kind="tool_call",
                call=heuristic_call,
                token_count=1,
            )
            return

        prompt = _messages_to_prompt(system, history, tools)
        text = await asyncio.to_thread(self._provider.complete, prompt)
        tool_call = _extract_tool_call(text, tools)
        if tool_call is not None:
            yield ProviderChunk(
                kind="tool_call",
                call=tool_call,
                token_count=_token_estimate(text),
            )
            return
        yield ProviderChunk(kind="token", text=str(text), token_count=_token_estimate(text))
        yield ProviderChunk(kind="final", is_final=True)


def _messages_to_prompt(
    system: str,
    history: Sequence[Message],
    tools: Sequence[Any],
) -> str:
    parts = [f"SYSTEM:\n{system.strip()}", ""]
    tool_names = [_tool_name(tool) for tool in tools]
    tool_names = [name for name in tool_names if name]
    if tool_names:
        parts.append(
            "AVAILABLE_TOOLS:\n"
            + "\n".join(f"- {name}" for name in tool_names)
            + "\n\n"
            "When current information from a tool is needed, respond only "
            "with JSON in this form: "
            '{"tool_call":{"name":"tool_name","arguments":{}}}. '
            "For latest Jira issues in a project, prefer "
            'jira_search_issues with {"jql":"project = KEY ORDER BY '
            'created DESC","fields":"key,summary,status,assignee,updated",'
            '"limit":1}.'
        )
        parts.append("")
    for message in history:
        role = getattr(message, "role", "user")
        text = getattr(message, "text", "")
        heading = "TOOL_RESULT" if role == "tool" else role.upper()
        parts.append(f"{heading}:\n{text}")
        if role == "tool":
            parts.append(
                "Use the TOOL_RESULT above to answer the user's latest "
                "question. Do not say you lack access when the tool result "
                "contains data."
            )
    parts.append("ASSISTANT:")
    return "\n\n".join(parts)


def _token_estimate(text: Any) -> int:
    value = str(text or "")
    return max(1, len(value.split()))


def _extract_tool_call(text: Any, tools: Sequence[Any]) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    allowed = {_tool_name(tool) for tool in tools if _tool_name(tool)}
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, Mapping):
            continue
        call = payload.get("tool_call") or payload.get("call")
        if not isinstance(call, Mapping):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        if not name or (allowed and name not in allowed):
            continue
        args = call.get("arguments") or call.get("args") or {}
        return {
            "tool_name": name,
            "arguments": dict(args) if isinstance(args, Mapping) else {},
            "intent": payload.get("intent"),
        }
    return None


def _heuristic_tool_call(
    history: Sequence[Message],
    tools: Sequence[Any],
) -> dict[str, Any] | None:
    """Return a read-only tool call for common current-data questions."""

    if not tools or _last_role(history) == "tool":
        return None
    last_user_raw = _last_user_text(history)
    last_user = last_user_raw.lower()
    last_user_norm = _fold_text(last_user_raw)
    if not last_user:
        return None
    allowed = {_tool_name(tool) for tool in tools if _tool_name(tool)}

    mail_call = _mail_tool_call(last_user_raw, last_user_norm, allowed)
    if mail_call is not None:
        return mail_call

    asks_for_jira = any(
        marker in last_user
        for marker in ("jira", "issue", "issues", "task", "tasks")
    ) or any(
        marker in last_user_norm
        for marker in ("jira", "issue", "issues", "task", "tasks", "gorev")
    )
    asks_for_assigned = any(
        marker in last_user
        for marker in ("assigned to me", "my ", "bana", "atan", "assignee")
    ) or any(
        marker in last_user_norm
        for marker in ("assigned to me", "my ", "bana", "atan", "assignee")
    )
    asks_for_open = any(
        marker in last_user
        for marker in ("open", "unresolved", "acik", "aktif")
    ) or any(
        marker in last_user_norm
        for marker in ("open", "unresolved", "acik", "aktif")
    )
    asks_to_list = any(
        marker in last_user
        for marker in ("list", "liste", "show", "get", "cek", "fetch")
    ) or any(
        marker in last_user_norm
        for marker in ("list", "liste", "show", "get", "cek", "fetch")
    )
    if asks_for_jira and asks_for_assigned and asks_for_open and asks_to_list:
        if "jira_search" in allowed:
            return {
                "tool_name": "jira_search",
                "arguments": {
                    "jql": (
                        "assignee = currentUser() AND resolution = Unresolved "
                        "ORDER BY updated DESC"
                    ),
                    "fields": "key,summary,status,assignee,updated",
                    "limit": 10,
                },
            }
        if "jira_search_issues" in allowed:
            return {
                "tool_name": "jira_search_issues",
                "arguments": {
                    "jql": (
                        "assignee = currentUser() AND resolution = Unresolved "
                        "ORDER BY updated DESC"
                    )
                },
            }
    project_key = _extract_jira_project_key(last_user_raw)
    asks_latest = any(
        marker in last_user
        for marker in ("latest", "last", "recent", "son", "en son", "guncel", "güncel")
    )
    asks_project_lookup = asks_for_jira and bool(project_key) and (
        asks_to_list
        or asks_latest
        or any(marker in last_user for marker in ("ara", "getir", "cek", "çek"))
    )
    if asks_project_lookup and project_key:
        limit = _extract_requested_limit(last_user)
        fields = "key,summary,status,assignee,updated"
        if "jira_search_issues" in allowed:
            return {
                "tool_name": "jira_search_issues",
                "arguments": {
                    "jql": f"project = {project_key} ORDER BY created DESC",
                    "fields": fields,
                    "limit": limit,
                },
            }
        if "jira_search" in allowed:
            return {
                "tool_name": "jira_search",
                "arguments": {
                    "jql": f"project = {project_key} ORDER BY created DESC",
                    "fields": fields,
                    "limit": limit,
                },
            }
        if "jira_get_project_issues" in allowed:
            return {
                "tool_name": "jira_get_project_issues",
                "arguments": {"project_key": project_key, "limit": limit},
            }
    asks_for_confluence = "confluence" in last_user_norm
    asks_to_search = any(
        marker in last_user_norm
        for marker in ("ara", "search", "bul", "find")
    )
    if asks_for_confluence and asks_to_search and "confluence_search" in allowed:
        return {
            "tool_name": "confluence_search",
            "arguments": {
                "query": _extract_search_query(last_user_raw) or last_user_raw,
                "limit": _extract_requested_limit(last_user_norm),
            },
        }
    asks_for_bitbucket = "bitbucket" in last_user_norm
    asks_for_repo = any(
        marker in last_user_norm
        for marker in ("repo", "repository", "repositories")
    )
    if asks_for_bitbucket and asks_for_repo and asks_to_list:
        limit = _extract_requested_limit(last_user_norm)
        if "bitbucket_list_repos" in allowed:
            return {
                "tool_name": "bitbucket_list_repos",
                "arguments": {"limit": limit},
            }
        if "bitbucket_search_repos" in allowed:
            return {
                "tool_name": "bitbucket_search_repos",
                "arguments": {
                    "query": _extract_search_query(last_user_raw) or "",
                    "limit": limit,
                },
            }
    return None


def _mail_tool_call(
    raw_text: str,
    folded_text: str,
    allowed: set[str],
) -> dict[str, Any] | None:
    providers = tuple(
        provider
        for provider in ("gmail", "outlook")
        if any(name.startswith(f"{provider}_") for name in allowed)
    )
    if not providers:
        return None

    provider = _preferred_mail_provider(folded_text, providers)
    prefix = f"{provider}_"
    limit = _extract_requested_limit(folded_text)
    offset = _extract_mail_offset(folded_text)

    message_id = _extract_mail_message_id(raw_text)
    get_tool = f"{prefix}get_message"
    if message_id and get_tool in allowed:
        return {
            "tool_name": get_tool,
            "arguments": {"message_id": message_id, "include_body": True},
        }

    priority_call = _priority_mail_tool_call(
        raw_text=raw_text,
        folded_text=folded_text,
        allowed=allowed,
        provider=provider,
        prefix=prefix,
        limit=limit,
        offset=offset,
    )
    if priority_call is not None:
        return priority_call

    latest_tool = f"{prefix}get_latest_message"
    asks_unread = any(
        marker in folded_text
        for marker in ("unread", "okunmamis", "okunmamislar", "okunmadi")
    )
    asks_inbox = _asks_mail_inbox(folded_text)
    asks_detail = _asks_mail_detail(folded_text)
    unread_tool = f"{prefix}list_unread_messages"
    sender = _extract_mail_sender(raw_text)
    subject = _extract_mail_subject(raw_text, folded_text)
    asks_latest = any(
        marker in folded_text
        for marker in ("son", "en son", "latest", "last", "recent", "yeni", "guncel")
    )
    asks_bulk_list = any(
        marker in folded_text
        for marker in ("liste", "list", "mailler", "emails", "messages", "mesajlar")
    ) or bool(re.search(r"\b\d{1,2}\s+(?:mail|email|e-mail|message|mesaj)", folded_text))
    asks_open_by_position = offset > 1 and any(
        marker in folded_text
        for marker in ("ac", "aç", "open", "goster", "göster", "detay", "detail")
    )
    if latest_tool in allowed and (
        asks_detail
        or (asks_latest and not asks_bulk_list)
        or asks_open_by_position
        or ((sender or subject) and (asks_detail or asks_latest))
        or (asks_unread and any(marker in folded_text for marker in ("son", "latest", "last")))
    ):
        arguments: dict[str, Any] = {
            "offset": offset,
            "include_body": True,
        }
        if asks_unread:
            arguments["unread"] = True
        if asks_inbox:
            arguments["inbox"] = True
        if sender:
            arguments["from"] = sender
        if subject:
            arguments["subject"] = subject
        if not sender and not subject and _asks_mail_search(folded_text):
            query = _extract_search_query(raw_text)
            if query:
                arguments["query"] = query
        return {"tool_name": latest_tool, "arguments": arguments}

    if asks_unread and unread_tool in allowed:
        return {"tool_name": unread_tool, "arguments": {"limit": limit}}

    search_tool = f"{prefix}search_messages"
    if (sender or subject or _asks_mail_search(folded_text)) and search_tool in allowed:
        arguments: dict[str, Any] = {"limit": limit}
        if sender:
            arguments["from"] = sender
        if subject:
            arguments["subject"] = subject
        if not sender and not subject:
            query = _extract_search_query(raw_text)
            if query:
                arguments["query"] = query
        return {"tool_name": search_tool, "arguments": arguments}

    asks_mail = any(
        marker in folded_text
        for marker in (
            "mail",
            "email",
            "e-mail",
            "inbox",
            "gelen kutusu",
            "mesaj",
            "message",
        )
    )
    asks_list = any(
        marker in folded_text
        for marker in ("son", "latest", "last", "recent", "liste", "list", "getir", "show")
    )
    list_tool = f"{prefix}list_messages"
    if asks_mail and asks_list and list_tool in allowed:
        return {"tool_name": list_tool, "arguments": {"limit": limit}}
    return None


def _priority_mail_tool_call(
    *,
    raw_text: str,
    folded_text: str,
    allowed: set[str],
    provider: str,
    prefix: str,
    limit: int,
    offset: int,
) -> dict[str, Any] | None:
    latest_tool = f"{prefix}get_latest_message"
    latest_draft_tool = f"{prefix}get_latest_draft"
    list_drafts_tool = f"{prefix}list_drafts"
    search_tool = f"{prefix}search_messages"

    asks_detail = _asks_mail_detail(folded_text)
    asks_latest = any(
        marker in folded_text
        for marker in ("son", "en son", "latest", "last", "recent", "yeni", "guncel")
    )
    asks_list = any(marker in folded_text for marker in ("liste", "list", "tum", "tüm"))
    asks_existing_drafts = _asks_existing_mail_drafts(folded_text)
    asks_reply_draft = _asks_reply_draft(folded_text)
    query_filter = _mail_query_filter(folded_text, provider)

    if asks_existing_drafts:
        if latest_draft_tool in allowed and (asks_detail or asks_latest or not asks_list):
            return {
                "tool_name": latest_draft_tool,
                "arguments": {"offset": offset, "include_body": True},
            }
        if list_drafts_tool in allowed:
            return {"tool_name": list_drafts_tool, "arguments": {"limit": limit}}

    if asks_reply_draft and latest_tool in allowed:
        arguments: dict[str, Any] = {
            "offset": offset,
            "include_body": True,
            "analysis_intent": "reply_draft",
        }
        if _asks_mail_inbox(folded_text):
            arguments["inbox"] = True
        if query_filter:
            arguments["query"] = query_filter
        return {"tool_name": latest_tool, "arguments": arguments}

    if query_filter and asks_latest and latest_tool in allowed and not asks_list:
        arguments = {"offset": offset, "include_body": True, "query": query_filter}
        if _asks_mail_inbox(folded_text):
            arguments["inbox"] = True
        return {"tool_name": latest_tool, "arguments": arguments}

    if query_filter and search_tool in allowed:
        query = _extract_search_query(raw_text)
        full_query = " ".join(part for part in (query_filter, query) if part)
        return {
            "tool_name": search_tool,
            "arguments": {"limit": limit, "query": full_query},
        }
    return None


def _preferred_mail_provider(text: str, providers: Sequence[str]) -> str:
    if "outlook" in text and "outlook" in providers:
        return "outlook"
    if "gmail" in text and "gmail" in providers:
        return "gmail"
    return providers[0]


def _extract_mail_message_id(text: str) -> str | None:
    patterns = (
        r"\b(?:message_id|message id|mail id|id)\s*[:=]\s*([A-Za-z0-9._%+/=-]{4,200})",
        r"\b(?:detay|detail|ozet|summary)\s+([A-Za-z0-9._%+/=-]{8,200})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _asks_mail_detail(text: str) -> bool:
    detail_markers = (
        "detay",
        "detayli",
        "detail",
        "details",
        "ozet",
        "özet",
        "summary",
        "summarize",
        "hakkinda",
        "hakknida",
        "bilgi",
        "anlat",
        "incele",
        "icerik",
        "içerik",
        "body",
        "tamamini",
        "tamamını",
        "ac",
        "aç",
        "goster",
        "göster",
        "show",
        "open",
        "oku",
        "read",
    )
    mail_markers = (
        "mail",
        "email",
        "e-mail",
        "message",
        "mesaj",
        "inbox",
        "gelen kutusu",
    )
    return any(marker in text for marker in detail_markers) and (
        any(marker in text for marker in mail_markers)
        or any(marker in text for marker in ("son", "latest", "last", "recent"))
    )


def _asks_mail_inbox(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "gelen mail",
            "gelen email",
            "gelen e-mail",
            "gelen mesaj",
            "gelen kutusu",
            "inbox",
            "received mail",
            "received email",
            "incoming mail",
        )
    )


def _asks_existing_mail_drafts(text: str) -> bool:
    if not any(marker in text for marker in ("taslak", "taslag", "draft")):
        return False
    return not _asks_reply_draft(text)


def _asks_reply_draft(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "cevap taslagi",
            "cevap taslag",
            "cevap taslağı",
            "yanit taslagi",
            "yanit taslag",
            "yanıt taslağı",
            "reply draft",
            "response draft",
            "cevap yaz",
            "yanit yaz",
            "yanıt yaz",
            "cevap hazirla",
            "cevap hazırla",
        )
    )


def _mail_query_filter(text: str, provider: str) -> str:
    parts: list[str] = []
    if any(marker in text for marker in ("bugun", "bugün", "today")):
        parts.append("newer_than:1d" if provider == "gmail" else "today")
    elif any(marker in text for marker in ("dun", "dün", "yesterday")):
        parts.append("newer_than:2d older_than:1d" if provider == "gmail" else "yesterday")
    elif any(marker in text for marker in ("bu hafta", "haftalik", "haftalık", "this week")):
        parts.append("newer_than:7d" if provider == "gmail" else "this week")
    elif any(marker in text for marker in ("bu ay", "this month")):
        parts.append("newer_than:30d" if provider == "gmail" else "this month")

    if any(marker in text for marker in ("ekli", "ek var", "attachment", "attached", "dosya")):
        parts.append("has:attachment" if provider == "gmail" else "attachment")
    if any(marker in text for marker in ("onemli", "önemli", "important", "kritik")):
        parts.append("is:important" if provider == "gmail" else "important")
    if any(marker in text for marker in ("guvenlik", "güvenlik", "security", "kod", "pin", "otp", "sifre", "şifre")):
        parts.append("security OR code OR pin OR otp")
    if (
        any(marker in text for marker in ("fatura", "invoice", "receipt", "makbuz"))
        and not any(marker in text for marker in ("konu", "subject", "baslik"))
    ):
        parts.append("invoice OR receipt OR fatura")
    if any(marker in text for marker in ("is ilani", "iş ilanı", "kariyer", "career", "job")):
        parts.append("job OR career OR kariyer")
    return " ".join(parts)


def _extract_mail_offset(text: str) -> int:
    ordinal_words = {
        "ilk": 1,
        "birinci": 1,
        "ikinci": 2,
        "ucuncu": 3,
        "üçüncü": 3,
        "dorduncu": 4,
        "dördüncü": 4,
        "besinci": 5,
        "beşinci": 5,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
    }
    for word, value in ordinal_words.items():
        if re.search(rf"\b{re.escape(word)}(?:yi|yı|nu|nü)?\b", text):
            return value
    match = re.search(r"\b(\d{1,2})(?:\.|inci|nci|nd|rd|th)?\s*(?:son\s*)?(?:mail|email|mesaj|message)?", text)
    if match:
        try:
            return max(1, min(int(match.group(1)), 25))
        except ValueError:
            return 1
    return 1


def _extract_mail_sender(text: str) -> str | None:
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    if email:
        return email.group(0)
    match = re.search(
        r"\b(?:from|sender|gonderen|gonderenden)\s*[:=]?\s*([^\n,;]{2,80})",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_mail_query(match.group(1))
    phrase = re.search(
        r"\b([A-Za-z0-9_.@+\-\s]{2,80}?)['’]?(?:den|dan|ten|tan)\s+gelen",
        _fold_text(text),
        flags=re.IGNORECASE,
    )
    if phrase:
        return _clean_mail_query(phrase.group(1))
    return None


def _extract_mail_subject(raw_text: str, folded_text: str) -> str | None:
    quoted = _extract_search_query(raw_text)
    if quoted and any(marker in folded_text for marker in ("subject", "konu", "baslik")):
        return quoted
    match = re.search(
        r"\b(?:subject|konu|baslik)\s*[:=]?\s*([^\n,;]{2,100})",
        raw_text,
        flags=re.IGNORECASE,
    )
    if match:
        return _clean_mail_query(match.group(1))
    return None


def _clean_mail_query(value: str) -> str:
    value = re.split(
        r"\b(?:son|ilk|top|limit|ozetle|detay|oku|getir|listele|ara|search|bul|find)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(
        r"\b(?:olan|olanlari|olanlar|mailleri|mailler|mail|email)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return " ".join(value.strip(" \t\r\n,.;'\"`").split())


def _asks_mail_search(text: str) -> bool:
    return any(marker in text for marker in ("ara", "search", "bul", "find"))


def _fold_text(text: str) -> str:
    """Lowercase text and strip accents for route-keyword matching."""

    turkish_folded = text.translate(str.maketrans({"ı": "i", "İ": "I"}))
    return (
        unicodedata.normalize("NFKD", turkish_folded)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _last_role(history: Sequence[Message]) -> str:
    if not history:
        return ""
    return str(getattr(history[-1], "role", "") or "")


def _last_user_text(history: Sequence[Message]) -> str:
    for message in reversed(history):
        if str(getattr(message, "role", "") or "") == "user":
            return str(getattr(message, "text", "") or "")
    return ""


def _tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, Mapping):
        return str(tool.get("name") or tool.get("tool_name") or "")
    return str(getattr(tool, "name", "") or getattr(tool, "tool_name", ""))


def _extract_jira_project_key(text: str) -> str | None:
    ignored = {"JIRA", "TASK", "ISSUE", "OPEN", "SON", "GETIR", "GETİR"}
    patterns = (
        r"\bproject\s*[=:]?\s*([A-Z][A-Z0-9_]{1,9})\b",
        r"\b([A-Z][A-Z0-9_]{1,9})\s+proje(?:si|sinde|sindeki|de|den|leri|ler)?\b",
        r"\b([A-Z][A-Z0-9_]{1,9})['’](?:da|de|daki|deki|dan|den)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        key = match.group(1).upper()
        if key not in ignored:
            return key
    return None


def _extract_requested_limit(text: str) -> int:
    for pattern in (
        r"(?:ilk|first)\s+(\d+)",
        r"(?:en son|son|latest|last|recent)\s+(\d+)",
        r"(\d+)\s+(?:task|tasks|issue|issues|gorev|görev|repo|repos|repository|repositories|mail|email|e-mail|message|messages|mesaj)",
    ):
        match = re.search(pattern, text)
        if match:
            return max(1, min(int(match.group(1)), 50))
    if any(marker in text for marker in ("son task", "latest task", "last task")):
        return 1
    return 10


def _extract_search_query(text: str) -> str | None:
    quoted = re.search(r"[\"`“”‘’]([^\"`“”‘’]{1,80})[\"`“”‘’]", text)
    if quoted:
        return quoted.group(1).strip()
    folded = _fold_text(text)
    match = re.search(r"\b([a-z0-9_.-]{2,80})\s+kelimesi(?:ni|ne|yle|nde)?\b", folded)
    if match:
        return match.group(1)
    return None
