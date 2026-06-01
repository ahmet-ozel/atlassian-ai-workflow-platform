"""Module for Confluence page move and page-hierarchy copy operations.

Implements Requirement 31 (move a page to a new parent and copy a page
subtree to a new parent) against the Confluence Data Center REST API.
Both endpoints are long-task backed: on non-trivial trees DC returns a
``longTaskId`` in the response body so the caller can poll progress via
the long-task endpoint wired up by :mod:`.long_tasks` (Requirement 38).

Endpoint reference:
    * ``PUT  /rest/api/content/{page_id}/move/{position}/{target_parent_id}``
      — move ``page_id`` under ``target_parent_id`` using the DC
      page-move endpoint. ``position`` is a path segment (``"append"``,
      ``"above"``, or ``"below"``) controlling the ordering of
      ``page_id`` relative to ``target_parent_id``'s existing
      children. The endpoint takes no request body. DC replies either
      synchronously (small moves within the same space) or with a
      long-task descriptor of the shape ``{"longTaskId": "..."}`` that
      the caller polls with ``confluence_get_long_task``.
    * ``POST /rest/api/content/{page_id}/pagehierarchy/copy``
      — copy ``page_id`` and all of its descendants under
      ``target_parent_id``. The request body selects which parts of
      each page are copied (permissions, attachments, labels) and
      optionally applies a ``titleOptions.prefix`` to every copied
      page. DC returns a long-task descriptor for any non-trivial
      tree.
    * ``GET  /rest/api/content/{target_parent_id}?expand=ancestors``
      — used for the pre-flight ancestor-of-self check on the copy
      path. The response's ``ancestors`` list carries the target's
      full ancestor chain (root-first); walking it and comparing each
      entry's id against ``page_id`` lets us reject cycles *before*
      issuing the POST, per Requirement 31.3.

The mixin is intentionally narrow: each method issues exactly the DC
REST calls needed and returns the raw response dict so the server-tool
layer can extract the ``longTaskId`` (Requirement 31.4) and wire it
into its structured tool response without a second round-trip. The
server-tool layer owns version gating, receipt construction, and
error-envelope mapping; this mixin contributes only the single
precondition call-out required by Requirement 31.3.

Ancestor-of-self rejection (Requirement 31.3): the copy path explicitly
refuses to move a subtree underneath itself. Two cases are covered:

* ``target_parent_id == page_id`` — a page cannot be its own parent.
* ``page_id`` appears in ``target_parent_id``'s ancestor chain — the
  target is a descendant of the source, so copying would create a
  cycle and DC would either fail non-deterministically or produce
  duplicated branches depending on version. We surface the violation
  as a :class:`ValueError` with the ``invalid_target:`` prefix so the
  server-tool layer can map it to the structured ``invalid_target``
  error envelope listed in the feature's error-code allowlist.

The pre-flight GET is cheap (single page fetch with an ``expand``)
and happens *before* any write-side call, which is what Requirement
31.3 and design Property 12 require.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class PageMoveCopyMixin(ConfluenceClient):
    """Mixin exposing page-move and page-hierarchy-copy operations.

    Both methods are keyword-only on the write-side arguments so the
    call sites in ``servers/confluence.py`` stay self-documenting and
    cannot accidentally swap ``page_id`` with ``target_parent_id``.
    The mixin assumes its inputs have already been authorized by the
    cross-cutting guards (``check_read_only``, ``check_project_filter``)
    and that DC version gating has run at the server layer — both
    endpoints have been stable across supported DC releases, so no
    ``check_dc_version`` call is expected for these tools.
    """

    def move_page(
        self,
        page_id: str,
        *,
        target_parent_id: str,
        position: str = "append",
    ) -> dict[str, Any]:
        """Move a Confluence page to a new parent.

        Wraps
        ``PUT /rest/api/content/{page_id}/move/{position}/{target_parent_id}``.
        The endpoint takes no request body; the move is fully
        specified by the path segments. DC replies either
        synchronously (small same-space moves complete in-line) or
        with a long-task descriptor like ``{"longTaskId": "..."}``
        that the caller polls via ``confluence_get_long_task``
        (Requirement 38).

        The raw DC response is returned unchanged so the server-tool
        layer can inspect the ``longTaskId`` field (Requirement 31.4)
        and surface it to the agent without a second round-trip. When
        DC returns an empty body the method normalizes to an empty
        dict so the caller can rely on a consistent dict contract.

        Args:
            page_id: Confluence content id of the page to move.
            target_parent_id: Content id of the page that should
                become the new parent. The move operates *relative*
                to this page — ``position`` selects whether
                ``page_id`` lands as a child (``"append"``) or as a
                sibling (``"above"`` / ``"below"``).
            position: One of ``"append"``, ``"above"``, or ``"below"``.
                ``"append"`` (the default) places ``page_id`` as the
                last child of ``target_parent_id``; ``"above"`` and
                ``"below"`` place it immediately before or after
                ``target_parent_id`` among its parent's children.

        Returns:
            The raw DC response dictionary. For asynchronous moves the
            dict contains a ``"longTaskId"`` entry that the server-tool
            layer forwards to the agent. For synchronous moves the
            dict carries DC's confirmation payload (typically an empty
            object). When DC returns a non-dict body the method
            normalizes to an empty dict.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example
                404 when ``page_id`` or ``target_parent_id`` does not
                exist, 403 when the caller lacks move permission on
                either page, or 409 when the move would violate a
                space constraint). The server-tool layer maps these
                to the standard structured-error envelope.
        """
        logger.debug(
            "Moving Confluence page page_id=%s target_parent_id=%s position=%s",
            page_id,
            target_parent_id,
            position,
        )
        path = (
            f"rest/api/content/{page_id}/move/{position}/{target_parent_id}"
        )
        response = self.confluence.put(path)
        if not isinstance(response, dict):
            return {}
        return response

    def copy_page_tree(
        self,
        page_id: str,
        *,
        target_parent_id: str,
        title_prefix: str | None = None,
        copy_permissions: bool = False,
        copy_attachments: bool = True,
        copy_labels: bool = False,
    ) -> dict[str, Any]:
        """Copy a Confluence page and its descendants to a new parent.

        Wraps ``POST /rest/api/content/{page_id}/pagehierarchy/copy``.
        The DC endpoint always operates on the full subtree rooted at
        ``page_id``; there is no partial-depth variant. The request
        body selects which per-page attributes are copied alongside
        the storage content.

        Pre-flight ancestor-of-self check (Requirement 31.3): before
        issuing the POST, the method fetches
        ``GET /rest/api/content/{target_parent_id}?expand=ancestors``
        and walks the resulting ``ancestors`` list. If ``page_id``
        appears anywhere in that chain — or if
        ``target_parent_id`` equals ``page_id`` — the copy would
        create a cycle, so the method raises :class:`ValueError` with
        the ``invalid_target:`` prefix and *no* write-side call is
        issued. The server-tool layer catches the ``invalid_target``
        prefix and maps it to the structured error envelope listed
        in the feature's error-code allowlist.

        The POST body follows the DC contract:

        .. code-block:: python

            {
                "destinationPageId": target_parent_id,
                "titleOptions": {"prefix": title_prefix}  # or {} when None
                "copyPermissions": copy_permissions,
                "copyAttachments": copy_attachments,
                "copyLabels": copy_labels,
            }

        DC returns a long-task descriptor like ``{"longTaskId": "..."}``
        for any non-trivial tree, which the caller polls via
        ``confluence_get_long_task`` (Requirement 31.4).

        Args:
            page_id: Confluence content id of the root page whose
                subtree should be copied.
            target_parent_id: Content id of the page that the copied
                subtree should be attached under. Must not equal
                ``page_id`` and must not be a descendant of
                ``page_id``.
            title_prefix: Optional string prepended to every copied
                page's title. DC only accepts the ``prefix`` variant
                of ``titleOptions``; when ``None`` the field is sent
                as an empty object and every copy keeps its source
                title verbatim (DC auto-numbers collisions).
            copy_permissions: When ``True`` the per-page restrictions
                from each source page are copied to its target. Off
                by default to match the conservative Requirement 31
                posture (copying restrictions can accidentally
                broaden or narrow access in ways the caller did not
                intend).
            copy_attachments: When ``True`` (the default) every
                source page's attachments are copied alongside the
                storage body.
            copy_labels: When ``True`` the source pages' labels are
                copied to the targets. Off by default so the copied
                tree starts with a clean label set.

        Returns:
            The raw DC response dictionary. For asynchronous copies
            the dict contains a ``"longTaskId"`` entry that the
            server-tool layer forwards to the agent for polling. When
            DC returns a non-dict body the method normalizes to an
            empty dict.

        Raises:
            ValueError: When ``target_parent_id == page_id`` or
                ``page_id`` appears in ``target_parent_id``'s
                ancestor chain. The message is prefixed with
                ``"invalid_target:"`` so the server-tool layer can
                map it to the structured ``invalid_target`` error
                envelope. Raised *before* any write-side call is
                issued (Requirement 31.3).
            HTTPError: Propagated from the underlying client when
                either the pre-flight ancestor GET or the copy POST
                returns a non-2xx response (for example 404 when
                ``page_id`` or ``target_parent_id`` does not exist,
                or 403 when the caller lacks copy permission).
        """
        source_id = str(page_id)
        target_id = str(target_parent_id)

        # Short-circuit the trivial self-as-parent case without a
        # round-trip. The ancestor GET below would also catch this
        # (``target_parent_id == page_id`` implies the ids match
        # directly), but handling it up front keeps the rejection
        # path predictable and lets the error message be specific.
        if target_id == source_id:
            raise ValueError(
                "invalid_target: target is ancestor-of-self "
                f"(page_id={source_id!r} equals target_parent_id)"
            )

        logger.debug(
            "Checking ancestor chain for copy_page_tree "
            "source_page_id=%s target_parent_id=%s",
            source_id,
            target_id,
        )
        target = self.confluence.get(
            f"rest/api/content/{target_id}",
            params={"expand": "ancestors"},
        )
        if not isinstance(target, dict):
            target = {}

        ancestors = target.get("ancestors")
        if not isinstance(ancestors, list):
            ancestors = []

        for ancestor in ancestors:
            if not isinstance(ancestor, dict):
                continue
            ancestor_id = ancestor.get("id")
            if ancestor_id is None:
                continue
            if str(ancestor_id) == source_id:
                raise ValueError(
                    "invalid_target: target is ancestor-of-self "
                    f"(page_id={source_id!r} is an ancestor of "
                    f"target_parent_id={target_id!r})"
                )

        # Build the title-options envelope. DC expects an object; when
        # no prefix is requested we send an empty object rather than
        # omitting the field so the request shape stays stable across
        # optional/required variants of the endpoint.
        title_options: dict[str, Any] = (
            {"prefix": title_prefix} if title_prefix else {}
        )

        body: dict[str, Any] = {
            "destinationPageId": target_id,
            "titleOptions": title_options,
            "copyPermissions": bool(copy_permissions),
            "copyAttachments": bool(copy_attachments),
            "copyLabels": bool(copy_labels),
        }

        logger.debug(
            "Copying Confluence page tree page_id=%s target_parent_id=%s "
            "title_prefix=%r copy_permissions=%s copy_attachments=%s "
            "copy_labels=%s",
            source_id,
            target_id,
            title_prefix,
            copy_permissions,
            copy_attachments,
            copy_labels,
        )
        response = self.confluence.post(
            f"rest/api/content/{source_id}/pagehierarchy/copy",
            data=body,
        )
        if not isinstance(response, dict):
            return {}
        return response
