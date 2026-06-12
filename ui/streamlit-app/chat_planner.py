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
_JIRA_SUMMARY_MAX_CHARS = 255
_FIELD_NAMES = {
    "summary": {"baslik", "summary", "title", "ozet"},
    "description": {"aciklama", "description", "desc"},
}
_ALL_FIELD_NAMES = sorted({name for names in _FIELD_NAMES.values() for name in names})
_PROJECT_KEY_STOPWORDS = {
    "ACIKLAMA",
    "BASLIK",
    "CREATE",
    "EKSIK",
    "ISSUE",
    "JIRA",
    "OLUSTUR",
    "PROJECT",
    "PROJE",
    "SON",
    "TASK",
    "TITLE",
    "YENI",
}


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
    folded = _fold_text(text)
    labels = "|".join(re.escape(name) for name in sorted(names))
    all_labels = "|".join(re.escape(name) for name in _ALL_FIELD_NAMES)
    match = re.search(
        rf"(?:^|[\s,.;])(?:{labels})\s*[:=]\s*(.+?)"
        rf"(?=(?:[\s,.;]+(?:{all_labels})\s*[:=])|$)",
        folded,
        flags=re.DOTALL,
    )
    if match:
        return text[match.start(1) : match.end(1)].strip(" \t\r\n,.;")
    return ""


def _strip_followup_instruction(text: str) -> str:
    folded = _fold_text(text)
    markers = (
        "olusturdugun issue",
        "olusturulan issue",
        "issue key",
        "durumunu soyle",
        "created issue",
    )
    positions = [folded.find(marker) for marker in markers if folded.find(marker) >= 0]
    if positions:
        text = text[: min(positions)]
    return text.strip(" \t\r\n,.;")


def _trim_jira_summary(summary: str) -> str:
    summary = _strip_followup_instruction(" ".join(summary.split()))
    if len(summary) <= _JIRA_SUMMARY_MAX_CHARS:
        return summary
    return summary[: _JIRA_SUMMARY_MAX_CHARS - 3].rstrip(" ,.;") + "..."


def _derive_jira_summary(text: str) -> str:
    cleaned = re.sub(
        r"\b(?:jira|task|issue|olustur|create|project|proje(?:si|sinde|sindeki)?)\b",
        " ",
        _fold_text(text),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b[A-Z][A-Z0-9_]{1,10}\b", " ", cleaned)
    cleaned = re.sub(r"[^\w\s./-]", " ", cleaned)
    cleaned = " ".join(cleaned.split())
    return _trim_jira_summary(cleaned)


def _normalise_project_candidate(value: str) -> str:
    candidate = value.strip(" \t\r\n,.;:'\"()[]{}")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,10}", candidate):
        return ""
    project_key = candidate.upper()
    if project_key in _PROJECT_KEY_STOPWORDS:
        return ""
    return project_key


def _strip_field_values_for_project_search(text: str) -> str:
    folded = _fold_text(text)
    labels = "|".join(re.escape(name) for name in _ALL_FIELD_NAMES)
    spans: list[tuple[int, int]] = []
    for match in re.finditer(
        rf"(?:^|[\s,.;])(?:{labels})\s*[:=]\s*(.+?)"
        rf"(?=(?:[\s,.;]+(?:{labels})\s*[:=])|$)",
        folded,
        flags=re.DOTALL,
    ):
        spans.append((match.start(1), match.end(1)))
    if not spans:
        return text
    chunks: list[str] = []
    cursor = 0
    for start, end in spans:
        chunks.append(text[cursor:start])
        cursor = end
    chunks.append(text[cursor:])
    return " ".join("".join(chunks).split())


def _extract_jira_project_key(text: str) -> str:
    project_text = _strip_field_values_for_project_search(text)
    prefix_match = re.search(
        r"(?i:\b(?:project\s+key|proje\s+key|project|proje)\b\s*[:=]?\s*)"
        r"([A-Za-z][A-Za-z0-9_]{1,10})\b",
        project_text,
    )
    if prefix_match:
        project_key = _normalise_project_candidate(prefix_match.group(1))
        if project_key:
            return project_key

    suffix_patterns = (
        r"\b([A-Z][A-Z0-9_]{1,10})\b\s+"
        r"(?i:proje(?:si|sindeki|sinde|sine|sinin|deki|de|nin|leri|ler)?|project)\b",
        r"\b([A-Z][A-Z0-9_]{1,10})(?:'?(?:da|de|ta|te|daki|deki|dan|den))\b"
        r"(?=.*(?i:\b(?:jira|task|issue|olustur|create)\b))",
    )
    for pattern in suffix_patterns:
        match = re.search(pattern, project_text)
        if not match:
            continue
        project_key = _normalise_project_candidate(match.group(1))
        if project_key:
            return project_key
    return ""


def _extract_limit(text: str, default: int = 5, maximum: int = 25) -> int:
    folded = _fold_text(text)
    match = re.search(r"\b(?:ilk|son|top|limit)\s*[:=]?\s*(\d{1,2})\b", folded)
    if not match:
        return default
    return max(1, min(int(match.group(1)), maximum))


_SPACE_KEY_STOPWORDS = {
    "VE",
    "KEY",
    "ID",
    "SPACE",
    "PAGE",
    "SAYFA",
    "BILGISINI",
    "BILGI",
    "ICIN",
    "ILE",
    "ADI",
    "ISMI",
    "NAME",
    "KISA",
    "DETAY",
}


def _extract_confluence_space_key(text: str) -> str:
    # The ``space`` keyword may appear in any case ("Space"/"space"), but the
    # key token itself must be uppercase AS WRITTEN - real Confluence space
    # keys are uppercase (E2ETEST, KAN, JOH). Using ``re.IGNORECASE`` on the
    # whole pattern previously captured lowercase prose words such as
    # "space bilgisini" -> "BILGISINI" or "space key" -> "KEY", which then
    # produced a CQL filter on a non-existent space and returned zero pages.
    match = re.search(
        r"[Ss][Pp][Aa][Cc][Ee]\s*[:=]?\s*([A-Z][A-Z0-9_]{1,20})\b|"
        r"\b([A-Z][A-Z0-9_]{1,20})\s+[Ss][Pp][Aa][Cc][Ee]\b",
        text,
    )
    if not match:
        return ""
    candidate = next((item for item in match.groups() if item), "").upper()
    if candidate in _SPACE_KEY_STOPWORDS:
        return ""
    return candidate


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


class MissingCredentialError(ValueError):
    """Raised before MCP is called when the requested service has no credential."""


def _require_credential(service: str, credential_for: CredentialGetter) -> Any:
    credential = credential_for(service)
    if credential is not None:
        return credential
    labels = {
        "jira": "Jira",
        "confluence": "Confluence",
        "bitbucket": "Bitbucket",
    }
    label = labels.get(service, service)
    raise MissingCredentialError(
        f"{label} credential yok. Credentials sayfasinda {label} bilgisini "
        "girip dogrulayin, sonra chat istegini tekrar gonderin."
    )


def plan_and_call_mcp(text: str, credential_for: CredentialGetter) -> tuple[str, Any]:
    lowered = _fold_text(text)

    if _is_jira_create_request(lowered):
        _require_credential("jira", credential_for)
        project_key = _extract_jira_project_key(text)
        if not project_key:
            raise ValueError(
                "Jira task olusturmak icin project key eksik. "
                "Lutfen su bilgiyi verin: project ABC veya ABC projesinde."
            )

        summary = _extract_field(text, _FIELD_NAMES["summary"]) or _derive_jira_summary(text)
        summary = _trim_jira_summary(summary)
        if not summary:
            raise ValueError(
                "Jira task olusturmak icin baslik/ozet eksik. "
                "Ornek: project ABC, baslik: Kisa task basligi, aciklama: ..."
            )
        description = _strip_followup_instruction(
            _extract_field(text, _FIELD_NAMES["description"]) or text
        )
        return mcp_call_any(
            [
                (
                    "jira_create_issue",
                    {
                        "project_key": project_key,
                        "summary": summary,
                        "description": description,
                        "issue_type": "Task",
                    },
                ),
                (
                    "create_issue",
                    {
                        "project_key": project_key,
                        "summary": summary,
                        "description": description,
                        "issue_type": "Task",
                    },
                ),
            ],
            credential_for,
        )

    if "confluence" in lowered or "conf" in lowered or "sayfa" in lowered:
        _require_credential("confluence", credential_for)
        topic = _extract_topic(text, "")
        limit = _extract_limit(text)
        space_key = _extract_confluence_space_key(text)
        cql = "type=page order by lastmodified desc"
        candidates: list[tuple[str, dict[str, Any]]] = []
        if space_key:
            cql = f'space = "{space_key}" and type=page order by lastmodified desc'
            candidates.extend(
                [
                    ("confluence_cql_search", {"cql": cql, "limit": limit}),
                    ("cql_search", {"cql": cql, "limit": limit}),
                ]
            )
        elif topic and not any(word in lowered for word in ("son", "guncel", "guncellenen")):
            quoted = topic.replace('"', '\\"')
            cql = f'type=page and (title ~ "{quoted}" or text ~ "{quoted}") order by lastmodified desc'
            candidates = [
                ("confluence_search", {"query": topic, "limit": limit}),
                ("search", {"query": topic, "limit": limit}),
            ]
        candidates.extend(
            [
                ("confluence_search", {"query": cql, "limit": limit}),
                ("search", {"query": cql, "limit": limit}),
                ("confluence_cql_search", {"cql": cql, "limit": limit}),
                ("cql_search", {"cql": cql, "limit": limit}),
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
        _require_credential("bitbucket", credential_for)
        repo = _extract_bitbucket_repo(text)
        is_pr_request = "pull request" in lowered or " pr " in f" {lowered} "
        if is_pr_request and repo:
            project_key, repo_slug = repo
            workspace = project_key
            limit = _extract_limit(text, default=10)
            return mcp_call_any(
                [
                    (
                        "bitbucket_list_pull_requests",
                        {
                            "workspace": workspace,
                            "repo_slug": repo_slug,
                            "state": "OPEN",
                            "max_results": limit,
                        },
                    ),
                    (
                        "bitbucket_list_pull_requests",
                        {
                            "project_key": project_key,
                            "repo_slug": repo_slug,
                            "state": "OPEN",
                            "max_results": limit,
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
            workspace = project_key
            limit = _extract_limit(text, default=5)
            return mcp_call_any(
                [
                    (
                        "bitbucket_list_commits",
                        {"workspace": workspace, "repo_slug": repo_slug, "max_results": limit},
                    ),
                    (
                        "bitbucket_list_commits",
                        {"project_key": project_key, "repo_slug": repo_slug, "limit": limit},
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
        _require_credential("jira", credential_for)
        issue_key = issue_match.group(1).upper()
        return mcp_call_any(
            [("jira_get_issue", {"issue_key": issue_key}), ("get_issue", {"issue_key": issue_key})],
            credential_for,
        )

    _require_credential("jira", credential_for)
    project_key = _extract_jira_project_key(text)
    jql = f"project = {project_key.upper()}" if project_key else "assignee = currentUser()"
    if "acik" in lowered or "open" in lowered:
        jql += " AND statusCategory != Done"
    jql += " ORDER BY updated DESC"
    limit = _extract_limit(text, default=10)
    return mcp_call_any(
        [
            ("jira_search", {"jql": jql, "limit": limit}),
            ("jira_search_issues", {"jql": jql, "limit": limit}),
            ("search", {"jql": jql, "limit": limit}),
        ],
        credential_for,
    )
