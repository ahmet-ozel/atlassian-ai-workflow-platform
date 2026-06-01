"""Prompt planning helpers for Streamlit Atlassian chat."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urlparse

from chat_mcp import CredentialGetter, mcp_call_any


_SERVICE_RE = re.compile(r"\b(?:jira|confluence|conf|bitbucket)(?:'?(?:da|de|ta|te|dan|den|daki|deki))?\b")
_TOPIC_NOISE_RE = re.compile(
    r"\b(?:sayfa\w*|liste\w*|repo(?:sitory)?|repos?\w*|repolar\w*|"
    r"workspace|task|issue|ara(?:ma)?|search|filter|filtre\w*|goster|"
    r"bul|son|guncel(?:lenen)?|yap|kisa|ozet(?:le)?|ilgili|hakkinda|"
    r"baslig?\w*|olan|getir|detay\w*|icerik\w*|adlar\w*|isim\w*|"
    r"private|public|bilgi\w*|dondur|don|cevap\w*|ve|ile|icin|mi)\b"
)
_SIMPLE_REPO_TERM_RE = re.compile(r"[A-Za-z0-9_.-]{2,64}")


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


def _extract_topic(text: str, fallback: str = "") -> str:
    cleaned = _SERVICE_RE.sub(" ", _fold_text(text))
    cleaned = _TOPIC_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[^\w\s./-]", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned or fallback


def _extract_field(text: str, names: set[str]) -> str:
    for line in text.splitlines():
        folded_line = _fold_text(line)
        if not any(name in folded_line for name in names):
            continue
        parts = re.split(r"[:=]", line, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return ""


def _is_jira_create_request(lowered: str) -> bool:
    asks_create = "olustur" in lowered or re.search(r"\bcreate\b", lowered) is not None
    return asks_create and (
        "jira" in lowered or "task" in lowered or "issue" in lowered
    )


def _extract_bitbucket_repo(text: str) -> tuple[str, str] | None:
    repo_match = re.search(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b", text)
    return repo_match.groups() if repo_match else None


def _bitbucket_workspace_from_credential(credential_for: CredentialGetter) -> str:
    credential = credential_for("bitbucket")
    if credential is None:
        return ""
    workspace = getattr(credential, "workspace", "")
    if isinstance(workspace, str) and workspace.strip():
        return workspace.strip().strip("/")
    parsed = urlparse(credential.url)
    if "bitbucket.org" not in parsed.netloc.lower():
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "2.0" and parts[1] == "repositories":
        return parts[2]
    return parts[0] if parts else ""


def _bitbucket_repository_query(text: str, lowered: str) -> str | None:
    if not any(word in lowered for word in ("ara", "search", "filter", "filtre")):
        return None
    topic = _extract_topic(text, "")
    if not _SIMPLE_REPO_TERM_RE.fullmatch(topic):
        return None
    quoted = topic.replace('"', '\\"')
    return f'name ~ "{quoted}"'


def plan_and_call_mcp(text: str, credential_for: CredentialGetter) -> tuple[str, Any]:
    lowered = _fold_text(text)

    if _is_jira_create_request(lowered):
        project_match = re.search(
            r"\bproject\s*[:=]?\s*([A-Z][A-Z0-9]{1,10})\b",
            text,
            re.IGNORECASE,
        )
        project_key = project_match.group(1).upper() if project_match else ""
        if not project_key:
            raise ValueError(
                "Jira task olusturmak icin project key belirtmelisiniz. "
                "Ornek: project ABC, baslik: ..., aciklama: ..."
            )

        summary = _extract_field(text, {"baslik", "summary"}) or text[:80]
        return mcp_call_any(
            [
                (
                    "jira_create_issue",
                    {
                        "project_key": project_key,
                        "summary": summary,
                        "description": text,
                        "issue_type": "Task",
                    },
                ),
                (
                    "create_issue",
                    {
                        "project_key": project_key,
                        "summary": summary,
                        "description": text,
                        "issue_type": "Task",
                    },
                ),
            ],
            credential_for,
        )

    if "confluence" in lowered or "conf" in lowered or "sayfa" in lowered:
        topic = _extract_topic(text, "")
        cql = "type=page order by lastmodified desc"
        candidates: list[tuple[str, dict[str, Any]]] = []
        if topic and not any(word in lowered for word in ("son", "guncel", "guncellenen")):
            quoted = topic.replace('"', '\\"')
            cql = f'type=page and (title ~ "{quoted}" or text ~ "{quoted}") order by lastmodified desc'
            candidates = [
                ("confluence_search", {"query": topic, "limit": 5}),
                ("search", {"query": topic, "limit": 5}),
            ]
        candidates.extend(
            [
                ("confluence_search", {"query": cql, "limit": 5}),
                ("search", {"query": cql, "limit": 5}),
                ("confluence_cql_search", {"cql": cql, "limit": 5}),
                ("cql_search", {"cql": cql, "limit": 5}),
            ]
        )
        return mcp_call_any(candidates, credential_for)

    asks_bitbucket = (
        "bitbucket" in lowered
        or "repo" in lowered
        or "commit" in lowered
        or "pull request" in lowered
        or " pr " in f" {lowered} "
    )
    if asks_bitbucket:
        repo = _extract_bitbucket_repo(text)
        is_pr_request = "pull request" in lowered or " pr " in f" {lowered} "
        if is_pr_request and repo:
            project_key, repo_slug = repo
            workspace = _bitbucket_workspace_from_credential(credential_for) or project_key
            return mcp_call_any(
                [
                    (
                        "bitbucket_list_pull_requests",
                        {
                            "workspace": workspace,
                            "repo_slug": repo_slug,
                            "state": "OPEN",
                            "max_results": 10,
                        },
                    ),
                    (
                        "bitbucket_list_pull_requests",
                        {
                            "project_key": project_key,
                            "repo_slug": repo_slug,
                            "state": "OPEN",
                            "max_results": 10,
                        },
                    ),
                ],
                credential_for,
            )
        if is_pr_request:
            raise ValueError(
                "Bitbucket PR listelemek icin repo belirtin. "
                "Ornek: Bitbucket example_workspace/smoke-test acik pull request listesini getir."
            )
        if "commit" in lowered and repo:
            project_key, repo_slug = repo
            workspace = _bitbucket_workspace_from_credential(credential_for) or project_key
            return mcp_call_any(
                [
                    (
                        "bitbucket_list_commits",
                        {"workspace": workspace, "repo_slug": repo_slug, "max_results": 5},
                    ),
                    (
                        "bitbucket_list_commits",
                        {"project_key": project_key, "repo_slug": repo_slug, "limit": 5},
                    ),
                ],
                credential_for,
            )
        workspace = _bitbucket_workspace_from_credential(credential_for)
        if not workspace:
            raise ValueError(
                "Bitbucket repo listelemek icin Credentials sayfasindaki "
                "Bitbucket workspace alanini girin. Ornek: example_workspace"
            )
        return mcp_call_any(
            [
                (
                    "bitbucket_list_repositories",
                    {
                        "workspace": workspace,
                        "query": _bitbucket_repository_query(text, lowered),
                        "max_results": 10,
                    },
                ),
                ("bitbucket_list_repos", {"project_key": workspace, "limit": 10}),
            ],
            credential_for,
        )

    issue_match = re.search(r"\b([A-Z][A-Z0-9_]+-\d+)\b", text, re.IGNORECASE)
    if issue_match:
        issue_key = issue_match.group(1).upper()
        return mcp_call_any(
            [("jira_get_issue", {"issue_key": issue_key}), ("get_issue", {"issue_key": issue_key})],
            credential_for,
        )

    project_match = re.search(
        r"\bproject\s*[:=]?\s*([A-Z][A-Z0-9_]{1,10})\b|"
        r"\b([A-Z][A-Z0-9_]{1,10})\s+proje(?:si|sindeki|sinde|deki|de|nin|leri|ler)?\b",
        text,
        re.IGNORECASE,
    )
    project_key = next((item for item in project_match.groups() if item), "") if project_match else ""
    jql = f"project = {project_key.upper()}" if project_key else "assignee = currentUser()"
    if "acik" in lowered or "open" in lowered:
        jql += " AND statusCategory != Done"
    jql += " ORDER BY updated DESC"
    return mcp_call_any(
        [
            ("jira_search", {"jql": jql, "limit": 10}),
            ("jira_search_issues", {"jql": jql, "limit": 10}),
            ("search", {"jql": jql, "limit": 10}),
        ],
        credential_for,
    )
