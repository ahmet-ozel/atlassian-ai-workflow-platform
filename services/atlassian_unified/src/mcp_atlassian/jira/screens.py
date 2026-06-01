"""Module for Jira screen metadata read-only operations (DC).

This mixin implements Requirement 25 from the atlassian-dc-tool-parity
feature: read-only lookup of the fields visible on the create screen
for a project and issue type, and the fields visible on the edit screen
for a given issue. No write methods are exposed per Requirement 25.3.

Both endpoints live under ``/rest/api/2/`` on Jira Data Center:

- ``GET /rest/api/2/issue/createmeta`` returns a nested
  ``{"projects": [{"issuetypes": [{"fields": {...}}]}]}`` envelope when
  called with ``expand=projects.issuetypes.fields``. The mixin flattens
  the fields map into a list of field descriptors, each carrying the
  ``fieldId`` so callers can correlate entries to other Jira APIs.
- ``GET /rest/api/2/issue/{issueIdOrKey}/editmeta`` returns a
  ``{"fields": {...}}`` envelope. The mixin flattens that map using the
  same shape as the create-screen output so both tools return a
  consistent payload.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class ScreensMixin(JiraClient):
    """Mixin for Jira screen metadata read-only operations.

    Provides read access to the create- and edit-screen field lists via
    the Data Center REST endpoints ``/rest/api/2/issue/createmeta`` and
    ``/rest/api/2/issue/{key}/editmeta``.

    Intentionally read-only — there are no tools here that modify
    screens, field configurations, or screen schemes.
    """

    @staticmethod
    def _flatten_fields_map(
        fields_map: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Flatten a ``{field_id: field_meta}`` map into a list.

        The Jira createmeta and editmeta endpoints both return the field
        metadata keyed by field id. Callers want a flat ``list[dict]``
        where each entry carries its ``fieldId`` so downstream agents
        can iterate and match without re-keying.

        Non-dict entries are skipped defensively (the upstream shape is
        consistently a dict, but a malformed response should not crash
        the caller). The original ``fieldId`` from the payload is
        preserved when present; otherwise the map key is stamped in.

        Args:
            fields_map: Mapping of field id to field metadata as
                returned by Jira.

        Returns:
            List of field metadata dictionaries, each including a
            ``fieldId`` string entry.
        """
        flattened: list[dict[str, Any]] = []
        for field_id, field_meta in fields_map.items():
            if not isinstance(field_meta, dict):
                logger.debug(
                    f"Skipping non-dict field entry for key '{field_id}' "
                    f"(got {type(field_meta).__name__})"
                )
                continue
            entry = dict(field_meta)
            entry.setdefault("fieldId", field_id)
            flattened.append(entry)
        return flattened

    def get_issue_create_screen(
        self,
        project_key: str,
        issue_type_id: str | None = None,
        issue_type_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the fields visible on the create screen.

        Calls ``GET /rest/api/2/issue/createmeta`` with
        ``projectKeys={project_key}`` plus one of
        ``issuetypeIds={issue_type_id}`` or
        ``issuetypeNames={issue_type_name}``, and
        ``expand=projects.issuetypes.fields``. The raw envelope
        looks like::

            {
                "projects": [
                    {
                        "key": "PROJ",
                        "issuetypes": [
                            {
                                "id": "10001",
                                "fields": {
                                    "summary": {"required": true, ...},
                                    "description": {"required": false, ...}
                                }
                            }
                        ]
                    }
                ]
            }

        and is flattened into a single ``list[dict]`` across all
        matching projects and issue types (typically one pair, given the
        query filters).

        Args:
            project_key: The project key to query (forwarded as
                ``projectKeys``, Jira's expected plural parameter).
            issue_type_id: Optional issue type id to query (forwarded as
                ``issuetypeIds``). Takes precedence over
                ``issue_type_name`` when both are provided.
            issue_type_name: Optional issue type display name to query
                (forwarded as ``issuetypeNames``). Used only when
                ``issue_type_id`` is not provided.

        Returns:
            List of field metadata dictionaries with ``fieldId`` set.
            Empty list on error or when the response has an unexpected
            shape.

        Raises:
            ValueError: When neither ``issue_type_id`` nor
                ``issue_type_name`` is provided.
        """
        if not issue_type_id and not issue_type_name:
            raise ValueError(
                "Either issue_type_id or issue_type_name must be provided."
            )

        params: dict[str, Any] = {
            "projectKeys": project_key,
            "expand": "projects.issuetypes.fields",
        }
        if issue_type_id:
            params["issuetypeIds"] = issue_type_id
        else:
            params["issuetypeNames"] = issue_type_name

        try:
            response = self.jira.get("rest/api/2/issue/createmeta", params=params)
        except Exception as e:
            logger.error(
                f"Error getting create screen for project '{project_key}' "
                f"issue type id='{issue_type_id}' name='{issue_type_name}': "
                f"{str(e)}"
            )
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/issue/createmeta`: "
                f"{type(response).__name__}"
            )
            return []

        projects = response.get("projects", [])
        if not isinstance(projects, list):
            logger.error(
                f"Unexpected 'projects' field type in createmeta response: "
                f"{type(projects).__name__}"
            )
            return []

        flattened: list[dict[str, Any]] = []
        for project in projects:
            if not isinstance(project, dict):
                continue
            issue_types = project.get("issuetypes", [])
            if not isinstance(issue_types, list):
                continue
            for issue_type in issue_types:
                if not isinstance(issue_type, dict):
                    continue
                fields = issue_type.get("fields", {})
                if not isinstance(fields, dict):
                    continue
                flattened.extend(self._flatten_fields_map(fields))

        return flattened

    def get_issue_edit_screen(self, issue_key: str) -> list[dict[str, Any]]:
        """Return the fields visible on the edit screen for an issue.

        Calls ``GET /rest/api/2/issue/{issueIdOrKey}/editmeta``. The raw
        response envelope is::

            {
                "fields": {
                    "summary": {"required": true, ...},
                    "description": {"required": false, ...}
                }
            }

        and is flattened to a ``list[dict]`` using the same shape as
        :meth:`get_issue_create_screen` so downstream callers can treat
        both screens uniformly.

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            List of field metadata dictionaries with ``fieldId`` set.
            Empty list on error or when the response has an unexpected
            shape.
        """
        try:
            response = self.jira.get(f"rest/api/2/issue/{issue_key}/editmeta")
        except Exception as e:
            logger.error(
                f"Error getting edit screen for issue '{issue_key}': {str(e)}"
            )
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/issue/{issue_key}/editmeta`: "
                f"{type(response).__name__}"
            )
            return []

        fields = response.get("fields", {})
        if not isinstance(fields, dict):
            logger.error(
                f"Unexpected 'fields' field type in editmeta response: "
                f"{type(fields).__name__}"
            )
            return []

        return self._flatten_fields_map(fields)
