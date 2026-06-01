"""Webhook operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{project_key}/repos/{repo_slug}/webhooks``
(Bitbucket DC 5.4+). Cloud paths target
``/2.0/repositories/{workspace}/{repo_slug}/hooks[/{uid}]``
(Requirement 16.4). Webhooks exist on both Cloud and DC, so the mixin
branches on :attr:`BitbucketClient.is_cloud` and translates the Cloud
payload shape back to the DC-shaped dict through
:func:`normalize_webhook`.

The mixin intentionally forwards webhook secrets to Bitbucket without
any redaction — secret hygiene (redacting ``secret`` / ``configuration.secret``
values in read responses and never echoing an input secret back to the
caller) is enforced by the server-layer ``redact_secrets()`` helper on
every response regardless of mode.
"""

import logging
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_webhook

logger = logging.getLogger("mcp-atlassian.bitbucket.webhooks")


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


class WebhooksMixin(BitbucketClient):
    """Mixin providing repository webhook CRUD for Bitbucket DC and Cloud."""

    def list_webhooks(
        self,
        project_key: str,
        repo_slug: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List webhooks configured on a repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            limit: Maximum number of results per page

        Returns:
            List of webhook objects. Callers in the server layer are
            expected to redact ``secret`` / ``configuration.secret``
            before returning the payload to an agent.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/hooks"
            return self._get_paged_results(
                url, limit=limit, normalizer=normalize_webhook
            )

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/webhooks"
        )
        return self._get_paged_results(url, limit=limit)

    def get_webhook(
        self,
        project_key: str,
        repo_slug: str,
        webhook_id: int,
    ) -> dict[str, Any]:
        """Fetch a single webhook by id.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            webhook_id: The numeric webhook id (DC) or UUID (Cloud)

        Returns:
            Webhook object. Server-layer callers MUST redact the
            ``secret`` / ``configuration.secret`` field before surfacing
            the response.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/hooks/{webhook_id}"
            )
            result = self.bitbucket.get(url)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response for webhook {webhook_id} in "
                    f"{workspace}/{repo_slug}: {result}"
                )
            normalized = normalize_webhook(result)
            assert normalized is not None
            return normalized

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/webhooks/{webhook_id}"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response for webhook {webhook_id} in "
                f"{project_key}/{repo_slug}: {result}"
            )
        return result

    def create_webhook(
        self,
        project_key: str,
        repo_slug: str,
        *,
        name: str,
        url: str,
        events: list[str],
        secret: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        """Create a repository webhook.

        DC request body follows Bitbucket DC's schema: ``configuration``
        is always sent as an object and populated with ``secret`` only
        when one is supplied. Cloud request body follows the Cloud 2.0
        shape: ``description``, ``url``, ``active``, ``events`` and an
        optional top-level ``secret``. In both modes the secret is
        forwarded verbatim so Bitbucket can HMAC outbound payloads; the
        server-layer ``redact_secrets()`` helper strips it from the
        response before it reaches the agent.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            name: Human-readable webhook name (mapped to ``description``
                on Cloud)
            url: Target URL that Bitbucket will POST events to
            events: List of event keys (e.g. ``["repo:refs_changed"]``)
            secret: Optional HMAC secret; forwarded to Bitbucket and not
                echoed back to the caller by the server layer
            active: Whether the webhook should be enabled on creation

        Returns:
            Created webhook object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            endpoint = f"/2.0/repositories/{workspace}/{repo_slug}/hooks"
            data: dict[str, Any] = {
                "description": name,
                "url": url,
                "active": active,
                "events": list(events),
            }
            if secret is not None:
                data["secret"] = secret

            result = self.bitbucket.post(endpoint, data=data)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response creating webhook in "
                    f"{workspace}/{repo_slug}: {result}"
                )
            normalized = normalize_webhook(result)
            assert normalized is not None
            return normalized

        endpoint = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/webhooks"
        )
        configuration: dict[str, Any] = {}
        if secret is not None:
            configuration["secret"] = secret

        data = {
            "name": name,
            "url": url,
            "events": list(events),
            "configuration": configuration,
            "active": active,
        }

        result = self.bitbucket.post(endpoint, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response creating webhook in "
                f"{project_key}/{repo_slug}: {result}"
            )
        return result

    def update_webhook(
        self,
        project_key: str,
        repo_slug: str,
        webhook_id: int,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update an existing webhook.

        Accepts any of the DC-shaped mutable webhook fields (``name``,
        ``url``, ``events``, ``configuration``, ``active``) as keyword
        arguments. On DC they are forwarded as the PUT body unchanged.
        On Cloud they are translated to the Cloud 2.0 body shape:

        - ``name`` → ``description``
        - ``url`` → ``url``
        - ``events`` → ``events``
        - ``active`` → ``active``
        - ``configuration.secret`` (if supplied) → top-level ``secret``
        - bare ``secret`` kwarg (if supplied) → top-level ``secret``

        Only keys present in ``fields`` are sent, matching the DC
        behavior. The secret is forwarded verbatim in the request body
        and never echoed back in the response — server-layer
        ``redact_secrets()`` enforces that on every response regardless
        of mode.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            webhook_id: The numeric webhook id (DC) or UUID (Cloud)
            **fields: Fields to update on the webhook.

        Returns:
            Updated webhook object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            endpoint = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/hooks/{webhook_id}"
            )
            body: dict[str, Any] = {}
            if "name" in fields:
                body["description"] = fields["name"]
            if "url" in fields:
                body["url"] = fields["url"]
            if "events" in fields:
                body["events"] = list(fields["events"])
            if "active" in fields:
                body["active"] = fields["active"]
            configuration = fields.get("configuration")
            if isinstance(configuration, dict) and "secret" in configuration:
                body["secret"] = configuration["secret"]
            # Allow a bare ``secret`` kwarg to win over configuration.secret
            # so callers can update the secret without rebuilding the
            # ``configuration`` dict.
            if "secret" in fields:
                body["secret"] = fields["secret"]

            result = self.bitbucket.put(endpoint, data=body)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response updating webhook {webhook_id} in "
                    f"{workspace}/{repo_slug}: {result}"
                )
            normalized = normalize_webhook(result)
            assert normalized is not None
            return normalized

        endpoint = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/webhooks/{webhook_id}"
        )
        result = self.bitbucket.put(endpoint, data=dict(fields))
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating webhook {webhook_id} in "
                f"{project_key}/{repo_slug}: {result}"
            )
        return result

    def delete_webhook(
        self,
        project_key: str,
        repo_slug: str,
        webhook_id: int,
    ) -> None:
        """Delete a webhook.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            webhook_id: The numeric webhook id (DC) or UUID (Cloud)
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/hooks/{webhook_id}"
            )
            self.bitbucket.delete(url)
            return

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/webhooks/{webhook_id}"
        )
        self.bitbucket.delete(url)
