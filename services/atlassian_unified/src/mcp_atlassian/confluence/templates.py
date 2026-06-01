"""Module for Confluence blueprint and template operations.

Implements Requirement 32 (list templates/blueprints and create a page from
a template) against the Confluence Data Center REST API. The mixin gives
authors the minimum surface they need to discover the templates available
to a space and to bootstrap a new page from one of those templates
without hand-authoring storage-format XHTML.

Endpoint reference:
    * ``GET  /rest/api/template/page?spaceKey={space_key}``
      — list **user-authored** page templates, optionally scoped to a
      space. When ``spaceKey`` is omitted the endpoint returns the
      global templates defined on the instance.
    * ``GET  /rest/api/template/blueprint?spaceKey={space_key}``
      — list **blueprint** templates (both the built-in blueprints
      shipped with Confluence and any app-contributed blueprints),
      optionally scoped to a space. Blueprints are the templates users
      pick from Confluence's "Create" dialog.
    * ``GET  /rest/api/template/{template_id}``
      — fetch a single template's full payload, including its
      ``body.storage.value`` content. Used by
      :meth:`create_page_from_template` to read the template body so the
      new page is seeded with the template's storage-format XHTML.
    * ``POST /rest/api/content?expand=body.storage``
      — create a Confluence page in the target space with the template
      body as its initial content. ``?expand=body.storage`` asks DC to
      echo the newly persisted storage body back in the response so
      callers can verify what landed on the page without a second GET.

The mixin deliberately keeps the two methods narrow: discovery returns a
plain list of DC dicts (so the server layer can JSON-encode them
directly), and page creation returns the raw DC content response so the
server-tool layer owns any shaping or receipt construction. DC version
gating is not required here — the template endpoints have been stable
since Confluence 5.x — so no ``check_dc_version`` call is expected at
the call site.

``context`` on :meth:`create_page_from_template` is intentionally a
free-form ``dict[str, Any]`` of template substitution variables. DC
blueprints accept a ``context`` field on the create-content request
body that is fed to blueprint wizards, but plain page templates do not
perform server-side substitution. This mixin forwards the value
unchanged when supplied so operators can use it with blueprint-backed
workflows; callers targeting plain page templates will want to leave it
``None``.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class TemplatesMixin(ConfluenceClient):
    """Mixin exposing list-templates and create-page-from-template.

    Both methods are keyword-only so the call sites in
    ``servers/confluence.py`` stay self-documenting. The mixin does not
    apply any space or read-only filtering; those checks belong to the
    server-tool layer (``check_project_filter`` /
    ``check_read_only``) and are expected to run before this mixin is
    called.
    """

    def list_templates(
        self,
        *,
        space_key: str | None = None,
        blueprint: bool = False,
    ) -> list[dict[str, Any]]:
        """List page templates or blueprints available to the caller.

        Routes to one of two DC endpoints based on ``blueprint``:

        * ``blueprint=False`` (default) →
          ``GET /rest/api/template/page`` — user-authored page
          templates.
        * ``blueprint=True`` →
          ``GET /rest/api/template/blueprint`` — blueprint templates
          (built-in plus app-contributed).

        When ``space_key`` is provided it is forwarded as the
        ``spaceKey`` query parameter so the listing is scoped to that
        space. When ``space_key`` is ``None`` the parameter is omitted
        entirely and DC returns the global (instance-wide) template
        set.

        Args:
            space_key: Optional Confluence space key to scope the
                listing (for example ``"ENG"``). ``None`` returns
                global templates.
            blueprint: When ``True`` list blueprint templates; when
                ``False`` (the default) list user-authored page
                templates.

        Returns:
            The ``results`` list from the DC response. Each entry is a
            dict matching DC's template envelope (typically including
            ``templateId``, ``name``, ``description``, ``templateType``,
            and — for blueprints — the backing module key). Returns
            an empty list when DC returns no results or an unexpected
            payload shape.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example
                404 when ``space_key`` does not exist, or 403 when the
                caller lacks access to the space).
        """
        path = (
            "rest/api/template/blueprint"
            if blueprint
            else "rest/api/template/page"
        )
        params: dict[str, Any] = {}
        if space_key is not None:
            params["spaceKey"] = space_key

        logger.debug(
            "Listing Confluence %s templates space_key=%s",
            "blueprint" if blueprint else "page",
            space_key,
        )
        response = self.confluence.get(path, params=params or None)

        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list):
                return results
            # Some DC versions return a bare list or a single-entry dict
            # without a ``results`` envelope; coerce those to a list so
            # the caller always sees a uniform shape.
            return [response] if response else []
        if isinstance(response, list):
            return response

        logger.debug(
            "list_templates: unexpected response type %s; returning empty list",
            type(response).__name__,
        )
        return []

    def create_page_from_template(
        self,
        *,
        space_key: str,
        title: str,
        template_id: str,
        parent_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new Confluence page seeded from a template.

        Two DC calls are issued:

        1. ``GET /rest/api/template/{template_id}`` — fetch the
           template so its ``body.storage.value`` can be used as the
           seed body for the new page. This keeps the mixin behavior
           predictable across plain page templates and blueprints: the
           resulting page is always created with concrete
           storage-format XHTML rather than relying on DC's
           template-expansion side effects.
        2. ``POST /rest/api/content?expand=body.storage`` — create the
           page. ``?expand=body.storage`` asks DC to echo the persisted
           storage body back in the response so the caller can see what
           landed on the page without a second GET.

        The request body follows DC's standard content-create shape:

        .. code-block:: python

            {
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "ancestors": [{"id": parent_id}],  # omitted when parent_id is None
                "body": {
                    "storage": {
                        "value": <template body storage value>,
                        "representation": "storage",
                    },
                },
                "metadata": {
                    "properties": {"editor": {"value": "v2"}},
                },
                "context": context,  # only when caller supplied a non-None dict
            }

        ``metadata.properties.editor.value = "v2"`` pins the new page to
        the modern Fabric editor, matching what Confluence's own UI
        does when creating a page from a template, so the page opens
        without the legacy-editor downgrade prompt.

        Args:
            space_key: Key of the space to create the page in.
            title: Title for the new page.
            template_id: Identifier of the template whose body will
                seed the new page. Accepts both page-template ids and
                blueprint template ids; the template lookup uses the
                same ``/rest/api/template/{id}`` endpoint for both.
            parent_id: Optional content id of the parent page. When
                omitted the page is created at the space root.
            context: Optional blueprint-context variables to forward
                verbatim in the create body. Leave ``None`` for plain
                page templates (DC ignores ``context`` for those).

        Returns:
            The DC content response dict for the newly created page,
            including the expanded ``body.storage`` envelope so the
            caller can verify the stored content.

        Raises:
            HTTPError: Propagated from the underlying client when
                either the template GET or the content POST returns a
                non-2xx response (for example 404 when ``template_id``
                or ``space_key`` does not exist, or 403 when the caller
                lacks create-content permission on the space).
        """
        logger.debug(
            "Fetching template body template_id=%s for new page in space_key=%s",
            template_id,
            space_key,
        )
        template = self.confluence.get(f"rest/api/template/{template_id}")
        if not isinstance(template, dict):
            template = {}

        # Pull the template's storage body. DC's template payload nests
        # the storage value under ``body.storage.value``; when a
        # template has no body (rare, but seen on empty blueprints)
        # fall back to an empty string so the POST still succeeds and
        # produces a blank page.
        storage_value = (
            ((template.get("body") or {}).get("storage") or {}).get("value")
            or ""
        )

        body: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": storage_value,
                    "representation": "storage",
                },
            },
            "metadata": {
                "properties": {"editor": {"value": "v2"}},
            },
        }
        if parent_id is not None:
            body["ancestors"] = [{"id": str(parent_id)}]
        if context is not None:
            # Forwarded verbatim so blueprint-backed templates can
            # perform their server-side variable substitution. Plain
            # page templates ignore this field.
            body["context"] = context

        logger.debug(
            "Creating Confluence page from template_id=%s in space_key=%s title=%s",
            template_id,
            space_key,
            title,
        )
        response = self.confluence.post(
            "rest/api/content",
            data=body,
            params={"expand": "body.storage"},
        )
        if not isinstance(response, dict):
            response = {}
        return response
