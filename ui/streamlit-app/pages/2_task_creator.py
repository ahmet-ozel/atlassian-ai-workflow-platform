"""Streamlit Task Creator chat page.

Task Creator is intentionally a chat-only assistant. It does not open Jira
issues and it does not ask through a structured form. The user describes the
task they want to create; the assistant points out missing information and,
when enough context exists, drafts the Jira description text.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components import render_dept_switcher
from components.theme import apply_theme, page_hero


_PROMPT_NAME = "task_creation_assistant.md"
_PROMPT_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent.parent / "prompts" / _PROMPT_NAME,
    Path("/app/prompts") / _PROMPT_NAME,
]

EXECUTION_WORKFLOWS = {
    "code_change_with_test",
    "remote_ssh_test_only",
    "script_execute",
}
REPO_REQUIRED_WORKFLOWS = {
    "code_change_with_test",
    "code_change_commit_only",
    "pr_review",
    "remote_ssh_test_only",
    "script_execute",
}


def _read_system_prompt_template() -> str:
    for prompt_path in _PROMPT_CANDIDATES:
        if prompt_path.is_file():
            return prompt_path.read_text(encoding="utf-8")
    return ""


SYSTEM_PROMPT_TEMPLATE = _read_system_prompt_template()


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in keywords)


def _infer_workflow_type(text: str) -> str:
    lower = text.lower()
    code_change = _has_any(
        lower,
        (
            "kod yaz",
            "değiştir",
            "degistir",
            "düzelt",
            "duzelt",
            "fix",
            "bug",
            "implement",
            "commit",
        ),
    )
    test_like = _has_any(
        lower,
        ("test", "smoke", "endpoint", "api", "docker", "çalıştır", "calistir"),
    )
    if _has_any(lower, ("pr review", "pull request", "merge request", "review et")):
        return "pr_review"
    if "sadece commit" in lower or ("commit" in lower and "pr" not in lower):
        return "code_change_commit_only"
    if test_like and not code_change:
        return "remote_ssh_test_only"
    if code_change:
        return "code_change_with_test" if test_like else "code_change_commit_only"
    if _has_any(lower, ("araştır", "arastir", "research", "internet", "web search")):
        return "research_publish_confluence" if "confluence" in lower else "research_summary_jira"
    if _has_any(lower, ("confluence", "doküman", "dokuman", "sayfa")):
        return "confluence_doc_update" if _has_any(lower, ("güncelle", "guncelle", "update")) else "confluence_doc_create"
    return "multi_step"


def _has_exact_repo(text: str) -> bool:
    lower = text.lower()
    repo_patterns = (
        r"https?://[^\s]+",
        r"\b[\w.-]+/[\w.-]+\b",
        r"\brepo(?:sitory)?\s*[:=]\s*[\w./:-]+",
        r"\bproje\s*[:=]\s*[\w./:-]+",
    )
    return any(re.search(pattern, lower) for pattern in repo_patterns)


def _has_branch(text: str) -> bool:
    lower = text.lower()
    return bool(
        re.search(r"\b(branch|dal)\s*[:=]\s*[\w./-]+", lower)
        or re.search(r"\b(develop|development|main|master|release/[\w.-]+|feature/[\w.-]+|hotfix/[\w.-]+)\b", lower)
        or "default branch" in lower
    )


def _has_command_or_rule(text: str) -> bool:
    lower = text.lower()
    return bool(
        re.search(r"\b(npm|pnpm|yarn|pytest|mvn|gradle|docker|make|go test|dotnet test)\b", lower)
        or "komut" in lower
        or "repo standardına göre" in lower
        or "bot seçsin" in lower
    )


def _output_types(text: str, workflow_type: str) -> list[str]:
    lower = text.lower()
    outputs = ["jira_comment"]
    if "md" in lower or "attachment" in lower or "ekle" in lower or "yükle" in lower:
        outputs.append("jira_attachment_md")
    if "confluence" in lower:
        outputs.append("confluence_update_page" if "güncelle" in lower else "confluence_create_page")
    if workflow_type in {"code_change_with_test", "code_change_commit_only"}:
        outputs.append("bitbucket_commit")
    if "pr" in lower or "pull request" in lower:
        outputs.append("bitbucket_create_pr")
    if "done" in lower or "kapat" in lower or "bitir" in lower:
        outputs.append("jira_transition_done")
    return list(dict.fromkeys(outputs))


def _missing_info(text: str, workflow_type: str, outputs: list[str], bot_username: str) -> list[str]:
    lower = text.lower()
    missing: list[str] = []
    if workflow_type in REPO_REQUIRED_WORKFLOWS and not _has_exact_repo(text):
        missing.append("Hangi repo/proje olduğu net değil. Repo adı, Bitbucket URL'i ya da Jira description içinde botun anlayacağı repo alanı gerekli.")
    if workflow_type in REPO_REQUIRED_WORKFLOWS and not _has_branch(text):
        missing.append("Hangi branch kullanılacağı net değil. Hedef branch ya da default branch kullanılacağı yazılmalı.")
    if workflow_type in EXECUTION_WORKFLOWS and not _has_command_or_rule(text):
        missing.append("Çalıştırılacak test/script komutu net değil. Komutu yazın ya da botun repo standardına göre seçmesini istediğinizi belirtin.")
    if workflow_type in EXECUTION_WORKFLOWS and not _has_any(lower, ("ssh", "runner", "sunucu", "remote", "lokal", "local")):
        missing.append("Kod/test çalıştırma nerede yapılacak net değil. Tanımlı SSH runner mı kullanılacak, yoksa sadece repo üzerinden analiz mi yapılacak?")
    if workflow_type in EXECUTION_WORKFLOWS and not _has_any(lower, ("cleanup", "temizle", "silme", "silinsin", "kalsın", "kalsin")):
        missing.append("Docker/container/workspace cleanup tercihi yok. İş bitince temizlensin mi, kalsın mı?")
    if any(item.startswith("confluence") for item in outputs) and not _has_any(lower, ("space", "page", "sayfa", "confluence url", "parent")):
        missing.append("Confluence hedefi eksik. Space key, parent page ya da güncellenecek sayfa URL'i gerekli.")
    if not bot_username and not _has_any(lower, ("assignee", "atan", "bot kullanıc", "bot kullanic")):
        missing.append("Task'ın Jira'da hangi bot kullanıcısına atanacağı belirtilmeli.")
    if len(text.strip()) < 40:
        missing.append("Kapsam kısa kalıyor. Botun neyi inceleyeceği, ne üretmesi gerektiği ve kabul kriterleri netleşmeli.")
    return missing


def _should_draft(text: str, missing: list[str]) -> bool:
    lower = text.lower()
    wants_draft = _has_any(lower, ("taslak", "description", "prompt", "yaz", "hazırla", "hazirla"))
    return wants_draft and len(missing) <= 2


def _draft_description(text: str, workflow_type: str, outputs: list[str], bot_username: str) -> str:
    outputs_yaml = "\n".join(f"  - {item}" for item in outputs)
    needs_ssh = workflow_type in EXECUTION_WORKFLOWS
    assignee = bot_username or "<jira bot kullanıcısı>"
    return f"""---
workflow_type: {workflow_type}
assignee: {assignee}
repo: <repo adı / Bitbucket URL / Jira field>
branch: <default branch veya hedef branch>
needs_ssh: {str(needs_ssh).lower()}
needs_docker: {str(needs_ssh).lower()}
test_command: <komut ya da 'bot repo standardına göre seçsin'>
cleanup: <always | on_success | never>
confluence_target: <space key / parent page / page URL>
outputs:
{outputs_yaml}
---

## Amaç
{text.strip()}

## Kapsam
- Bot Jira description içindeki repo/proje bilgisini kullanarak gerekli MCP verilerini toplamalı.
- Kod veya test çalıştırma gerekiyorsa tanımlı runner SSH workspace'inde task'a özel klasör oluşturmalı.
- Eksik bilgi varsa Jira'ya comment yazıp cevap gelene kadar beklemeli; cevap gelince workflow devam etmeli.

## Kabul Kriterleri
- Yapılan işlemler Jira comment içinde özetlenmeli.
- Test çalıştıysa komut, exit code ve önemli çıktı raporlanmalı.
- İstenen çıktı hedefleri tamamlanmalı: {", ".join(outputs)}.
"""


def _task_creator_chat_reply(text: str, context: str, bot_username: str) -> str:
    workflow_type = _infer_workflow_type(context)
    outputs = _output_types(context, workflow_type)
    missing = _missing_info(context, workflow_type, outputs, bot_username)

    lines = [
        f"Anladım. Bu task büyük ihtimalle `{workflow_type}` akışı.",
        "",
    ]
    if missing:
        lines.append("Task açmadan önce şu bilgiler eksik veya net değil:")
        lines.extend(f"- {item}" for item in missing)
        lines.extend(
            [
                "",
                "Bu bilgileri eklersen senin adına Jira'da description alanına yazılacak temiz task metnini hazırlayabilirim.",
            ]
        )
    else:
        lines.extend(
            [
                "Gerekli bilgiler yeterli görünüyor. İstersen Jira description taslağını aşağıdaki gibi kullanabilirsin:",
                "",
                "```markdown",
                _draft_description(text, workflow_type, outputs, bot_username),
                "```",
            ]
        )

    if _should_draft(text, missing):
        lines.extend(
            [
                "",
                "Eksikler az olduğu için placeholder'lı bir taslak da hazırladım:",
                "",
                "```markdown",
                _draft_description(text, workflow_type, outputs, bot_username),
                "```",
            ]
        )

    return "\n".join(lines)


def _inject_prompt_vars(template: str, bot_username: str) -> str:
    replacements = {
        "bot_username_for_dept": bot_username,
        "user_display_name": st.session_state.get("user_name", "kullanıcı"),
        "current_date": date.today().isoformat(),
    }
    injected = template
    for key, value in replacements.items():
        injected = injected.replace("{" + key + "}", value)
    return injected


_inject_session_state()
st.set_page_config(page_title="Task Creator", page_icon="🆕", layout="centered")
render_user_navigation()
apply_theme()
page_hero(
    "Task Creator",
    "Task açmadan önce eksikleri soran ve Jira description metnini sohbet içinde hazırlayan asistan.",
    icon="🆕",
)

dept_id = render_dept_switcher()
session_user = st.session_state.get("user", {})
bot_username = (
    st.session_state.get("_bot_identity_card_account_id")
    or session_user.get("bot_username")
    or ""
)
st.session_state["_system_prompt_injected"] = _inject_prompt_vars(
    SYSTEM_PROMPT_TEMPLATE,
    str(bot_username),
)

redirect_payload: dict[str, Any] = st.session_state.pop("_pending_task_creator_redirect", None) or {}
prefill = redirect_payload.get("prefill") or {}
redirect_text = prefill.get("description") or redirect_payload.get("message") or ""

history: list[dict[str, str]] = st.session_state.setdefault(
    "task_creator_chat_history",
    [
        {
            "role": "assistant",
            "text": (
                "Task'ı nasıl açmak istediğini doğal dille yaz. "
                "Eksik bilgileri söyleyeyim; istersen sonra Jira description taslağını birlikte yazalım."
            ),
        }
    ],
)

if redirect_text and not st.session_state.get("_task_creator_redirect_consumed"):
    st.session_state["_task_creator_redirect_consumed"] = True
    history.append({"role": "user", "text": redirect_text})
    history.append(
        {
            "role": "assistant",
            "text": _task_creator_chat_reply(
                redirect_text,
                redirect_text,
                str(bot_username),
            ),
        }
    )

for entry in history:
    with st.chat_message(entry.get("role", "assistant")):
        st.markdown(entry.get("text", ""))

user_message = st.chat_input(
    "Oluşturmak istediğiniz task'ı yazın...",
    key="task_creator_chat_input",
)

if user_message:
    history.append({"role": "user", "text": user_message})
    with st.chat_message("user"):
        st.markdown(user_message)

    context = "\n".join(item.get("text", "") for item in history if item.get("role") == "user")
    reply = _task_creator_chat_reply(user_message, context, str(bot_username))
    history.append({"role": "assistant", "text": reply})
    with st.chat_message("assistant"):
        st.markdown(reply)
