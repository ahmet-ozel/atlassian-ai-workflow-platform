"""External link extractor (W3 deeplink helper reuse).

**Covers 8.4 (rule 8 / Q9 - `external_links` field on
``GET /admin/workflows/{wf_id}``).**

The W3 deep-link concept (architecture notes §16.12 W3) standardises how the
admin dashboard renders cross-tool URLs: every workflow detail panel
links back to the originating Jira issue, the Bitbucket PR (if any)
and the Confluence page (if any). The TypeScript counterpart lives
in :file:`libs/web-shared/src/deeplink.ts`; this Python module is the
backend half of the same contract.

Rather than reach into a dept-scoped Vault credential to discover the
Atlassian base URL, this helper is **conservative**: it scans the
``audit_chain`` payloads we already collect for the workflow (see
:func:`workflows_drilldown._fetch_audit_chain`) and returns the first
verbatim URL value it finds for each link type. Audits emitted by
``automation-service`` and ``agent-runner-worker`` already carry these
URL strings:

* ``jira_issue_url`` / ``jira_issue_link`` / ``issue_url`` - set by
  ``jira_build_issue_link`` activity output (see
  :mod:`agent_runner.workflows.agent_runner_workflow`).
* ``pr_url`` / ``bitbucket_pr_url`` - set by ``bitbucket_create_pr``
  activity output and the cost-comment poster.
* ``page_url`` / ``confluence_page_url`` / ``confluence_url`` - set by
  ``confluence_create_page`` activity output.

When none of those keys are populated, the helper returns an empty
dict - the FE renders no link rather than a broken stub URL. This
matches the W3 deep-link contract: deep links are only surfaced when
the underlying resource exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = ["build_external_links"]


#: Audit payload keys we accept as a Jira issue URL. Order matters -
#: the first non-empty match wins, so ``jira_issue_url`` (the
#: canonical key) is checked before ``issue_url`` (a generic fallback
#: used by older audit emitters).
_JIRA_KEYS: Final[tuple[str, ...]] = (
    "jira_issue_url",
    "jira_issue_link",
    "issue_url",
)

#: Audit payload keys we accept as a Bitbucket PR URL.
_BITBUCKET_KEYS: Final[tuple[str, ...]] = (
    "bitbucket_pr_url",
    "pr_url",
)

#: Audit payload keys we accept as a Confluence page URL.
_CONFLUENCE_KEYS: Final[tuple[str, ...]] = (
    "confluence_page_url",
    "confluence_url",
    "page_url",
)


def _coerce_url(value: Any) -> str | None:
    """Return ``value`` if it is a non-empty HTTPS string, else ``None``.

    The audit payload is opaque JSON so we cannot trust the type.
    Reject anything that is not a plain ``https://`` string to keep
    the extracted link safe to drop into an ``<a href>`` attribute on
    the workflow detail page.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.startswith("https://"):
        return None
    return cleaned


def _first_url(
    payloads: Iterable[Mapping[str, Any]], keys: tuple[str, ...]
) -> str | None:
    """Return the first valid URL found under ``keys`` across ``payloads``.

    Iterates over each payload in chronological order (oldest first -
    callers preserve that order from the ``audit_chain`` query) and
    returns the first ``https://``-prefixed string match. Older audits
    win on the assumption that the URL surfaced earliest in the
    workflow's history is the canonical one (e.g. the Jira issue link
    is set by the very first activity).
    """

    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        for key in keys:
            url = _coerce_url(payload.get(key))
            if url is not None:
                return url
    return None


def build_external_links(
    audit_chain: Iterable[Mapping[str, Any]],
) -> dict[str, str]:
    """Return ``{jira_issue_url?, bitbucket_pr_url?, confluence_page_url?}``.

    Args:
        audit_chain: Iterable of audit events as the
            workflow drill-down endpoint would return them. Each
            element is a mapping with at least a ``payload`` key
            (the JSONB ``payload`` column of ``audit_events``);
            non-mapping items are skipped silently so the helper is
            resilient to a partial DB response.

    Returns:
        A dict carrying only the keys whose URL was discovered.
        Missing keys are intentionally absent so the JSON serialiser
        emits a compact response (``{}`` when no link is known).
    """

    payloads: list[Mapping[str, Any]] = []
    for entry in audit_chain:
        if not isinstance(entry, Mapping):
            continue
        payload = entry.get("payload")
        if isinstance(payload, Mapping):
            payloads.append(payload)

    result: dict[str, str] = {}

    jira_url = _first_url(payloads, _JIRA_KEYS)
    if jira_url is not None:
        result["jira_issue_url"] = jira_url

    bitbucket_url = _first_url(payloads, _BITBUCKET_KEYS)
    if bitbucket_url is not None:
        result["bitbucket_pr_url"] = bitbucket_url

    confluence_url = _first_url(payloads, _CONFLUENCE_KEYS)
    if confluence_url is not None:
        result["confluence_page_url"] = confluence_url

    return result
