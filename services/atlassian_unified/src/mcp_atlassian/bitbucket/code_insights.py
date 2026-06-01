"""Code Insights (reports & annotations) operations for Bitbucket DC and Cloud.

DC paths target ``/rest/insights/1.0/projects/{key}/repos/{slug}/commits/{sha}/reports/...``
(the DC Code Insights plugin used by quality scanners — SonarQube, Snyk,
Trivy, Checkmarx, ...). Cloud paths target
``/2.0/repositories/{workspace}/{repo_slug}/commit/{sha}/reports[/{report_id}]``
and ``.../reports/{report_id}/annotations`` (Requirements 12.1, 12.2,
12.3). The agent-facing method signatures, parameter names, and return
types do not change between modes; Cloud payloads are passed through
with minimal shape adjustment so downstream server-layer code keeps
consuming the same dict / bool / list shapes.

The Cloud Code Insights body shape is very close to DC: the shared
fields (``title``, ``details``, ``reporter``, ``result``, ``link``,
``data``) carry the same semantic meaning, and ``logoUrl`` maps to
``logo_url``. Report ``result`` values differ slightly (DC uses
``PASS`` / ``FAIL``; Cloud also accepts ``PASSED`` / ``FAILED`` /
``PENDING``) but both sides pass the values through verbatim; the
agent-visible parameter name is preserved (Requirement 5.4).
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.code_insights")


def _resolve_workspace(
    project_key: str | None,
    config_workspace: str | None,
) -> str:
    """Resolve the Cloud workspace for a Bitbucket tool call.

    Precedence rules from Requirements 2.4 / 2.5 / 2.6:

    1. A non-empty ``project_key`` argument wins — it is interpreted as the
       workspace slug in Cloud mode.
    2. Otherwise ``config_workspace`` (populated from ``BITBUCKET_WORKSPACE``
       or the URL path by :meth:`BitbucketConfig.from_env`) is used.
    3. When both are empty/``None``, the mixin raises ``ValueError`` with a
       ``filtered_out:`` prefix so the server layer can map it onto a
       :class:`StructuredError` with ``error_code="filtered_out"`` before
       any outbound HTTP call.
    """
    if project_key:
        return project_key
    if config_workspace:
        return config_workspace
    raise ValueError(
        "filtered_out: Bitbucket Cloud workspace is required. "
        "Pass a non-empty project_key or set BITBUCKET_WORKSPACE."
    )


class CodeInsightsMixin(BitbucketClient):
    """Mixin providing Code Insights report and annotation operations."""

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def list_code_insight_reports(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List all Code Insights reports for a commit.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            limit: Maximum results per page

        Returns:
            List of report objects (key, title, result, reporter, ...).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports"
            )
            return self._get_paged_results(url, limit=limit)

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports"
        )
        return self._get_paged_results(url, limit=limit)

    def get_code_insight_report(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
    ) -> dict[str, Any]:
        """Get a single Code Insights report by key.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            report_key: Stable identifier of the report
                (e.g. ``com.sonarsource.sonarqube``). On Cloud this maps
                directly to the ``{report_id}`` path segment.

        Returns:
            Report object.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}"
            )
            result = self.bitbucket.get(url)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response for report {report_key}: {result}"
                )
            return result

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response for report {report_key}: {result}"
            )
        return result

    def create_or_update_code_insight_report(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
        title: str,
        details: str | None = None,
        result: str | None = None,
        reporter: str | None = None,
        link: str | None = None,
        logo_url: str | None = None,
        data: list[dict[str, Any]] | None = None,
        report_type: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace a Code Insights report.

        Both DC and Cloud expose the create/update operation as an
        idempotent ``PUT`` keyed by ``report_key`` — calling it again
        with the same key overwrites the existing report. The body
        shapes are close enough that the shared fields (``title``,
        ``details``, ``result``, ``reporter``, ``link``, ``data``) are
        passed through verbatim; the only spelling difference is
        ``logoUrl`` (DC) vs ``logo_url`` (Cloud), which we translate at
        the Cloud branch so agents keep the same ``logo_url`` parameter
        name.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1 the report applies to
            report_key: Stable identifier (e.g. ``my-ci.security-scan``)
            title: Human-readable report title
            details: Optional long-form description
            result: Optional outcome — ``PASS``/``FAIL`` on DC, or
                ``PASSED``/``FAILED``/``PENDING`` on Cloud; passed
                through verbatim either way
            reporter: Optional name of the tool that produced the report
            link: Optional URL to the full report in the source system
            logo_url: Optional logo URL for the UI
            data: Optional list of key/value summary rows — each item is
                ``{"title": str, "type": "NUMBER|PERCENTAGE|TEXT|..",
                "value": Any}``

        Returns:
            Created/updated report object.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}"
            )
            # Cloud uses ``logo_url`` (snake_case); every other shared
            # field name is identical to DC so we pass it through as-is.
            # ``type: "report"`` is Cloud-only and optional, but Cloud
            # accepts the payload without it; we leave it off to keep
            # the DC and Cloud request bodies aligned on the shared
            # fields listed in the docstring.
            payload: dict[str, Any] = {"title": title}
            # Cloud requires ``details`` to be present; fall back to an
            # empty string when the caller omits it so the PUT does not
            # return HTTP 400 "required attributes are not set [details]".
            payload["details"] = details if details is not None else ""
            # Cloud also requires ``report_type``; default to "TEST" when
            # the caller omits it so the PUT does not return HTTP 400
            # "required attributes are not set [type]".
            payload["report_type"] = report_type if report_type is not None else "TEST"
            if result is not None:
                payload["result"] = result
            if reporter is not None:
                payload["reporter"] = reporter
            if link is not None:
                payload["link"] = link
            if logo_url is not None:
                payload["logo_url"] = logo_url
            if data is not None:
                payload["data"] = data

            # ``atlassian-python-api``'s ``put`` helper appends a trailing
            # slash to the URL, which causes Cloud to return HTTP 400.
            # Bypass it and use the underlying session directly.
            raw_response = self.bitbucket._session.put(
                f"{self.config.url}{url}",
                json=payload,
                verify=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
            raw_response.raise_for_status()
            try:
                response = raw_response.json()
            except ValueError:
                response = {}
            if not isinstance(response, dict):
                raise ValueError(
                    f"Unexpected response upserting report {report_key}: {response}"
                )
            return response

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}"
        )
        payload = {"title": title}
        if details is not None:
            payload["details"] = details
        if result is not None:
            payload["result"] = result
        if reporter is not None:
            payload["reporter"] = reporter
        if link is not None:
            payload["link"] = link
        if logo_url is not None:
            payload["logoUrl"] = logo_url
        if data is not None:
            payload["data"] = data

        response = self.bitbucket.put(url, data=payload)
        if not isinstance(response, dict):
            raise ValueError(
                f"Unexpected response upserting report {report_key}: {response}"
            )
        return response

    def delete_code_insight_report(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
    ) -> bool:
        """Delete a Code Insights report (and its annotations).

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            report_key: Report identifier

        Returns:
            True on successful deletion.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}"
            )
            self.bitbucket.delete(url)
            return True

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}"
        )
        self.bitbucket.delete(url)
        return True

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    def list_code_insight_annotations(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List annotations attached to a given report.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            report_key: Report identifier
            limit: Page size (default 100)

        Returns:
            List of annotation objects.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}/annotations"
            )
            return self._get_paged_results(url, limit=limit)

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}/annotations"
        )
        return self._get_paged_results(url, limit=limit)

    def bulk_create_code_insight_annotations(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
        annotations: list[dict[str, Any]],
    ) -> bool:
        """Create up to 1000 annotations for a report in one call.

        Each annotation should include at minimum ``externalId`` (stable
        identifier within the scanner), ``message`` and ``severity``
        (``LOW``/``MEDIUM``/``HIGH``). Optional fields: ``path``, ``line``,
        ``type`` (``VULNERABILITY``/``CODE_SMELL``/``BUG``), ``link``.

        DC accepts a wrapped body ``{"annotations": [...]}``; Cloud
        accepts a bare JSON array for bulk upload. The agent-facing
        ``annotations`` parameter stays identical (Requirement 5.4); the
        Cloud branch unwraps the list before POSTing.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            report_key: Parent report identifier
            annotations: List of annotation dicts

        Returns:
            True on success.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}/annotations"
            )
            # Cloud bulk-create takes the annotations array as the top-level
            # POST body rather than DC's ``{"annotations": [...]}`` wrapper.
            # ``atlassian-python-api``'s ``post`` helper wraps the payload in
            # a dict when it detects a list, so we bypass it and use the
            # underlying session directly to preserve the bare-array shape
            # that Cloud requires.
            import json as _json
            response = self.bitbucket._session.post(
                f"{self.config.url}{url}",
                data=_json.dumps(annotations),
                headers={"Content-Type": "application/json"},
                verify=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            return True

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}/annotations"
        )
        self.bitbucket.post(url, data={"annotations": annotations})
        return True

    def delete_code_insight_annotations(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        report_key: str,
        external_ids: list[str] | None = None,
    ) -> bool:
        """Delete annotations from a report.

        DC supports a single bulk-delete call that accepts either no
        ``externalId`` parameter (removes every annotation on the report)
        or a repeated ``externalId`` query argument (removes only those
        annotations). Cloud exposes a per-annotation DELETE keyed by
        ``external_id`` and does not have a bulk-delete endpoint; the
        Cloud branch iterates ``external_ids`` and calls the per-annotation
        DELETE for each, preserving the agent-visible parameter name and
        the boolean return type.

        When ``external_ids`` is ``None`` on Cloud we list the report's
        annotations first and then delete each one individually, so the
        "delete all" semantics of the DC call are preserved.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Full commit SHA-1
            report_key: Report identifier
            external_ids: Optional list of specific annotation external IDs
                to delete. If omitted, all annotations on the report are
                removed.

        Returns:
            True on successful deletion.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            base_url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/reports/{report_key}/annotations"
            )

            if external_ids:
                targets: list[str] = list(external_ids)
            else:
                # Cloud lacks a bulk "delete all annotations" endpoint; list
                # the annotations and derive each target's external_id so
                # the DC "delete all" semantics are preserved. Fall back to
                # the annotation's ``uuid`` when ``external_id`` is absent
                # (Cloud exposes both identifiers on the list payload).
                listed = self._get_paged_results(base_url, limit=100)
                targets = []
                for ann in listed:
                    if not isinstance(ann, dict):
                        continue
                    ident = ann.get("external_id") or ann.get("externalId") or ann.get(
                        "uuid"
                    )
                    if isinstance(ident, str) and ident:
                        targets.append(ident)

            for ident in targets:
                self.bitbucket.delete(f"{base_url}/{ident}")
            return True

        url = (
            f"/rest/insights/1.0/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/reports/{report_key}/annotations"
        )
        params: dict[str, Any] = {}
        if external_ids:
            params["externalId"] = external_ids
        self.bitbucket.delete(url, params=params if params else None)
        return True
