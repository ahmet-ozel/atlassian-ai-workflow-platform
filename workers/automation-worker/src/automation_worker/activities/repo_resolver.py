"""Repo Field Resolver activity for the automation-worker.

Implements the ``resolve_repo_field`` Temporal activity that resolves
the target repository for a task using a priority-based strategy:

1. If the structured "Repository" field is non-empty, use it directly
 (skip description parsing entirely).
2. If the field is empty, use LLM to parse the repo from the task
 description. If confidence < 0.8, post a Jira comment asking the
 user for the repo info.
3. Validate the resolved repo against the department's ``repo_mappings``
 list. If not found, reject the task and post a Jira comment with
 the allowed repo list.
4. If the user was asked for repo info and no response within 30
 minutes, move the task to "needs_info" status.

Design reference: design.md §12 (Repo Field Resolver)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from temporalio import activity

__all__ = (
    "resolve_repo_field",
    "RepoResolveInput",
    "RepoResolveResult",
    "set_llm_parser",
    "get_llm_parser",
    "set_jira_commenter",
    "get_jira_commenter",
    "set_jira_transitioner",
    "get_jira_transitioner",
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum confidence score required to accept an LLM-parsed repo.
CONFIDENCE_THRESHOLD: float = 0.8

#: Timeout in minutes before moving task to "needs_info" status.
USER_RESPONSE_TIMEOUT_MINUTES: int = 30

_REPO_LINE_RE = re.compile(
    r"(?im)^\s*(?:repo|repository|bitbucket_repo|target_repo)\s*:\s*(?P<repo>\S+)\s*$"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoResolveInput:
    """Input for the resolve_repo_field activity.

 Attributes:
 issue_key: The Jira issue key (e.g. "PROJ-123").
 dept_id: Department identifier for config resolution.
 workflow_id: Parent workflow identifier for tracing.
 structured_field_value: Value from the structured "Repository"
 custom field. None or empty string means not provided.
 description: The task description text for LLM parsing.
 repo_mappings: Department's allowed repository mappings list.
 Each dict has at minimum a "bitbucket_repo" key with the
 repo URL/identifier.
 labels: Optional Jira labels. A label in ``repo:<slug>`` or
 ``repository:<slug>`` form is accepted before free-text LLM
 parsing and still validated against ``repo_mappings``.
 """

    issue_key: str
    dept_id: str
    workflow_id: str
    structured_field_value: str | None
    description: str
    repo_mappings: list[dict[str, Any]]
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RepoResolveResult:
    """Result of the resolve_repo_field activity.

 Attributes:
 resolved: True if a valid repo was successfully resolved.
 repo_url: The resolved repository URL/identifier, or None.
 confidence: Confidence score (1.0 for structured field, LLM
 score for parsed, 0.0 if unresolved).
 needs_user_input: True if the user was asked for repo info
 (confidence < threshold or field empty with no LLM match).
 error: Error message if resolution failed (e.g. repo not in
 allowed list).
 """

    resolved: bool
    repo_url: str | None
    confidence: float
    needs_user_input: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Dependency Injection Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMRepoParserProtocol(Protocol):
    """Protocol for LLM-based repo parsing from description text.

 Production wires this to the LLM orchestrator. Tests inject a
 fake that returns predetermined parse results.
 """

    async def parse_repo_from_description(
        self,
        description: str,
        repo_mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Parse repository info from task description using LLM.

 Args:
 description: The task description text.
 repo_mappings: Available repos for context.

 Returns:
 Dict with keys:
 - "repo_url": str | None - parsed repo identifier
 - "confidence": float - confidence score (0.0 to 1.0)
 """
        ...


@runtime_checkable
class JiraCommenterProtocol(Protocol):
    """Protocol for posting comments to Jira issues."""

    async def add_comment(
        self,
        issue_key: str,
        body: str,
        *,
        dept_id: str,
    ) -> None:
        """Post a comment to the specified Jira issue.

 Args:
 issue_key: The Jira issue key.
 body: Comment body text.
 dept_id: Department ID for credential resolution.
 """
        ...


@runtime_checkable
class JiraTransitionerProtocol(Protocol):
    """Protocol for transitioning Jira issue status."""

    async def transition_issue(
        self,
        issue_key: str,
        target_status: str,
        *,
        dept_id: str,
    ) -> None:
        """Transition a Jira issue to the target status.

 Args:
 issue_key: The Jira issue key.
 target_status: The target status name.
 dept_id: Department ID for credential resolution.
 """
        ...


# ---------------------------------------------------------------------------
# Dependency Registry
# ---------------------------------------------------------------------------

_llm_parser: LLMRepoParserProtocol | None = None
_jira_commenter: JiraCommenterProtocol | None = None
_jira_transitioner: JiraTransitionerProtocol | None = None


def set_llm_parser(parser: LLMRepoParserProtocol) -> None:
    """Register the LLM repo parser used by the activity.

 Called once at worker boot. Tests call this with an in-memory fake.
 """
    global _llm_parser  # noqa: PLW0603
    _llm_parser = parser


def get_llm_parser() -> LLMRepoParserProtocol:
    """Resolve the registered LLM parser or fail loudly."""
    if _llm_parser is None:
        raise RuntimeError(
            "repo_resolver activity: LLM parser not initialised; "
            "call set_llm_parser during worker startup."
        )
    return _llm_parser


def set_jira_commenter(commenter: JiraCommenterProtocol) -> None:
    """Register the Jira commenter used by the activity.

 Called once at worker boot. Tests call this with an in-memory fake.
 """
    global _jira_commenter  # noqa: PLW0603
    _jira_commenter = commenter


def get_jira_commenter() -> JiraCommenterProtocol:
    """Resolve the registered Jira commenter or fail loudly."""
    if _jira_commenter is None:
        raise RuntimeError(
            "repo_resolver activity: Jira commenter not initialised; "
            "call set_jira_commenter during worker startup."
        )
    return _jira_commenter


def set_jira_transitioner(transitioner: JiraTransitionerProtocol) -> None:
    """Register the Jira transitioner used by the activity.

 Called once at worker boot. Tests call this with an in-memory fake.
 """
    global _jira_transitioner  # noqa: PLW0603
    _jira_transitioner = transitioner


def get_jira_transitioner() -> JiraTransitionerProtocol:
    """Resolve the registered Jira transitioner or fail loudly."""
    if _jira_transitioner is None:
        raise RuntimeError(
            "repo_resolver activity: Jira transitioner not initialised; "
            "call set_jira_transitioner during worker startup."
        )
    return _jira_transitioner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_allowed_repos(repo_mappings: list[dict[str, Any]]) -> list[str]:
    """Extract the list of allowed bitbucket_repo values from mappings.

 Returns a deduplicated list of repo identifiers.
 """
    repos: list[str] = []
    seen: set[str] = set()
    for mapping in repo_mappings:
        repo = mapping.get("bitbucket_repo")
        if repo and repo not in seen:
            repos.append(repo)
            seen.add(repo)
    return repos


def _repo_from_description(description: str) -> str | None:
    """Extract repo from YAML front-matter or simple ``repo:`` lines."""
    try:
        from automation_worker.activities.description_parser import (
            parse_description_frontmatter,
        )

        parsed = parse_description_frontmatter(description)
    except Exception:  # noqa: BLE001
        parsed = None
    repo = getattr(parsed, "repo", None)
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    match = _REPO_LINE_RE.search(description or "")
    if match:
        return match.group("repo").strip()
    return None


def _repo_from_labels(labels: list[str]) -> str | None:
    """Extract repo from labels like ``repo:org/service``."""
    for label in labels:
        if not isinstance(label, str):
            continue
        name, sep, value = label.partition(":")
        if sep and name.strip().lower() in {
            "repo",
            "repository",
            "bitbucket_repo",
            "target_repo",
        }:
            repo = value.strip()
            if repo:
                return repo
    return None


def _is_repo_in_allowed_list(
    repo_url: str,
    repo_mappings: list[dict[str, Any]],
) -> bool:
    """Check if a repo URL/identifier exists in the allowed mappings.

 Comparison is case-insensitive to handle URL variations.
 """
    repo_lower = repo_url.strip().lower()
    for mapping in repo_mappings:
        allowed = mapping.get("bitbucket_repo", "")
        if allowed.strip().lower() == repo_lower:
            return True
    return False


def _build_repo_ask_comment() -> str:
    """Build a Jira comment asking the user for repo information.

 """
    return (
        "🔍 Görev açıklamasından repository bilgisi yeterli güvenle "
        "belirlenemedi.\n\n"
        "Lütfen görevin ilişkili olduğu repository'yi belirtiniz. "
        "Bunu yapılandırılmış \"Repository\" alanını doldurarak veya "
        "bu comment'e yanıt vererek yapabilirsiniz."
    )


def _build_repo_rejected_comment(
    repo_value: str,
    allowed_repos: list[str],
) -> str:
    """Build a Jira comment for rejected repo (not in allowed list).

 """
    allowed_list = "\n".join(f"• {repo}" for repo in allowed_repos)
    return (
        f"❌ Belirtilen repository (`{repo_value}`) bu departman için "
        f"izin verilen repo listesinde bulunamadı.\n\n"
        f"İzin verilen repository'ler:\n{allowed_list}\n\n"
        f"Lütfen yukarıdaki listeden bir repository seçiniz."
    )


# ---------------------------------------------------------------------------
# Core Activity
# ---------------------------------------------------------------------------


@activity.defn(name="resolve_repo_field")
async def resolve_repo_field(input: RepoResolveInput) -> RepoResolveResult:
    """Resolve the target repository for a task.

 Priority logic:
 1. Structured field non-empty → use directly, skip LLM parsing.
 2. Structured field empty → LLM parse from description.
 - confidence >= 0.8 → accept parsed repo.
 - confidence < 0.8 → ask user via Jira comment.
 3. Validate resolved repo against repo_mappings.
 - Not in list → reject task, post allowed list comment.
 4. If user asked and no response within 30 min → "needs_info".

 """
    activity.logger.info(
        "repo_resolver: resolving repo for issue %s (workflow=%s, dept=%s)",
        input.issue_key,
        input.workflow_id,
        input.dept_id,
    )

    allowed_repos = _extract_allowed_repos(input.repo_mappings)
    jira_commenter = get_jira_commenter()

    # ------------------------------------------------------------------
    # Step 1: Check structured "Repository" field 
    # ------------------------------------------------------------------
    if input.structured_field_value and input.structured_field_value.strip():
        repo_value = input.structured_field_value.strip()
        activity.logger.info(
            "repo_resolver: structured field provided: %s (issue=%s)",
            repo_value,
            input.issue_key,
        )

        # Validate against allowed list 
        if not _is_repo_in_allowed_list(repo_value, input.repo_mappings):
            activity.logger.warning(
                "repo_resolver: repo %s not in allowed list for dept %s "
                "(issue=%s)",
                repo_value,
                input.dept_id,
                input.issue_key,
            )
            # Post rejection comment with allowed repos
            comment = _build_repo_rejected_comment(repo_value, allowed_repos)
            try:
                await jira_commenter.add_comment(
                    input.issue_key,
                    comment,
                    dept_id=input.dept_id,
                )
            except Exception as exc:  # noqa: BLE001
                activity.logger.warning(
                    "repo_resolver: failed to post rejection comment "
                    "to %s: %s",
                    input.issue_key,
                    exc,
                )

            return RepoResolveResult(
                resolved=False,
                repo_url=None,
                confidence=1.0,
                needs_user_input=True,
                error=(
                    f"Repository '{repo_value}' is not in the allowed "
                    f"repo_mappings for department '{input.dept_id}'."
                ),
            )

        # Structured field is valid - resolved successfully
        return RepoResolveResult(
            resolved=True,
            repo_url=repo_value,
            confidence=1.0,
            needs_user_input=False,
            error=None,
        )

    # ------------------------------------------------------------------
    # Step 2: deterministic label / description / single-repo fallback
    # ------------------------------------------------------------------
    described_repo = _repo_from_labels(input.labels) or _repo_from_description(
        input.description
    )
    if described_repo:
        activity.logger.info(
            "repo_resolver: description provided repo: %s (issue=%s)",
            described_repo,
            input.issue_key,
        )
        if _is_repo_in_allowed_list(described_repo, input.repo_mappings):
            return RepoResolveResult(
                resolved=True,
                repo_url=described_repo,
                confidence=1.0,
                needs_user_input=False,
                error=None,
            )
        comment = _build_repo_rejected_comment(described_repo, allowed_repos)
        try:
            await jira_commenter.add_comment(
                input.issue_key,
                comment,
                dept_id=input.dept_id,
            )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "repo_resolver: failed to post rejection comment to %s: %s",
                input.issue_key,
                exc,
            )
        return RepoResolveResult(
            resolved=False,
            repo_url=None,
            confidence=1.0,
            needs_user_input=True,
            error=(
                f"Repository '{described_repo}' is not in the allowed "
                f"repo_mappings for department '{input.dept_id}'."
            ),
        )

    if len(allowed_repos) == 1:
        return RepoResolveResult(
            resolved=True,
            repo_url=allowed_repos[0],
            confidence=0.9,
            needs_user_input=False,
            error=None,
        )

    # ------------------------------------------------------------------
    # Step 3: LLM parse from description 
    # ------------------------------------------------------------------
    activity.logger.info(
        "repo_resolver: structured field empty, attempting LLM parse "
        "(issue=%s)",
        input.issue_key,
    )

    llm_parser = get_llm_parser()

    try:
        parse_result = await llm_parser.parse_repo_from_description(
            input.description,
            input.repo_mappings,
        )
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "repo_resolver: LLM parse failed for issue %s: %s",
            input.issue_key,
            exc,
        )
        # LLM failure - ask user for repo info
        try:
            await jira_commenter.add_comment(
                input.issue_key,
                _build_repo_ask_comment(),
                dept_id=input.dept_id,
            )
        except Exception as comment_exc:  # noqa: BLE001
            activity.logger.warning(
                "repo_resolver: failed to post ask comment to %s: %s",
                input.issue_key,
                comment_exc,
            )

        return RepoResolveResult(
            resolved=False,
            repo_url=None,
            confidence=0.0,
            needs_user_input=True,
            error=f"LLM parsing failed: {exc}",
        )

    parsed_repo: str | None = parse_result.get("repo_url")
    confidence: float = float(parse_result.get("confidence", 0.0))

    activity.logger.info(
        "repo_resolver: LLM parse result - repo=%s, confidence=%.2f "
        "(issue=%s)",
        parsed_repo,
        confidence,
        input.issue_key,
    )

    # ------------------------------------------------------------------
    # Step 2a: Confidence check 
    # ------------------------------------------------------------------
    if not parsed_repo or confidence < CONFIDENCE_THRESHOLD:
        activity.logger.info(
            "repo_resolver: confidence %.2f < %.2f threshold, asking user "
            "(issue=%s)",
            confidence,
            CONFIDENCE_THRESHOLD,
            input.issue_key,
        )

        # Post comment asking user for repo info
        try:
            await jira_commenter.add_comment(
                input.issue_key,
                _build_repo_ask_comment(),
                dept_id=input.dept_id,
            )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "repo_resolver: failed to post ask comment to %s: %s",
                input.issue_key,
                exc,
            )

        # Transition to "needs_info" status 
        # The workflow will handle the 30-minute timeout; we signal
        # that user input is needed so the workflow can set up the
        # timer and transition accordingly.
        jira_transitioner = get_jira_transitioner()
        try:
            await jira_transitioner.transition_issue(
                input.issue_key,
                "needs_info",
                dept_id=input.dept_id,
            )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "repo_resolver: failed to transition %s to needs_info: %s",
                input.issue_key,
                exc,
            )

        return RepoResolveResult(
            resolved=False,
            repo_url=parsed_repo,
            confidence=confidence,
            needs_user_input=True,
            error=None,
        )

    # ------------------------------------------------------------------
    # Step 3: Validate parsed repo against allowed list (Req 9.4)
    # ------------------------------------------------------------------
    if not _is_repo_in_allowed_list(parsed_repo, input.repo_mappings):
        activity.logger.warning(
            "repo_resolver: LLM-parsed repo %s not in allowed list "
            "for dept %s (issue=%s)",
            parsed_repo,
            input.dept_id,
            input.issue_key,
        )

        # Post rejection comment with allowed repos
        comment = _build_repo_rejected_comment(parsed_repo, allowed_repos)
        try:
            await jira_commenter.add_comment(
                input.issue_key,
                comment,
                dept_id=input.dept_id,
            )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "repo_resolver: failed to post rejection comment "
                "to %s: %s",
                input.issue_key,
                exc,
            )

        return RepoResolveResult(
            resolved=False,
            repo_url=None,
            confidence=confidence,
            needs_user_input=True,
            error=(
                f"Repository '{parsed_repo}' is not in the allowed "
                f"repo_mappings for department '{input.dept_id}'."
            ),
        )

    # ------------------------------------------------------------------
    # Success: repo resolved and validated
    # ------------------------------------------------------------------
    activity.logger.info(
        "repo_resolver: successfully resolved repo %s for issue %s",
        parsed_repo,
        input.issue_key,
    )

    return RepoResolveResult(
        resolved=True,
        repo_url=parsed_repo,
        confidence=confidence,
        needs_user_input=False,
        error=None,
    )
