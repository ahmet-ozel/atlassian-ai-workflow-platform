"""Module for Confluence CQL advanced search operations.

This mixin implements Requirement 35 of the ``atlassian-dc-tool-parity`` spec:
an advanced CQL search endpoint that supports explicit sort order and space
filter awareness.

The mixin exposes two methods and one module-level sortable-field allowlist:

* :data:`SORTABLE_FIELDS` — the documented CQL-sortable field set used by
  :meth:`CQLAdvancedMixin.cql_search` to validate the ``order_by`` argument
  *before* issuing any outbound HTTP request. Exposed both at module level
  (stable import path) and as a class attribute (convenient access from
  tool code that already has a fetcher handle).
* :meth:`CQLAdvancedMixin.cql_search` — issues a paginated CQL search and,
  when ``order_by`` is supplied, appends an ``order by <field> <dir>``
  clause to the query string. An unknown ``order_by`` field raises
  :class:`ValueError` with a ``invalid_order_by:`` prefix so the server
  layer can map the condition to the structured ``invalid_order_by``
  error without any outbound call.
* :meth:`CQLAdvancedMixin.rewrite_cql_for_space_filter` — parses the CQL
  string for existing ``space = KEY`` and ``space in (...)`` clauses,
  intersects them with the operator-configured allow-list, and either
  returns a filter-restricted query or raises :class:`ValueError` with a
  ``filtered_out:`` prefix when the referenced spaces are disjoint from
  the allow-list. When no space clause is present it prepends
  ``space in (...)`` so every outbound search is bounded by the
  ``CONFLUENCE_SPACES_FILTER`` allow-list.

Validation errors are surfaced as :class:`ValueError` with a short,
machine-recognizable prefix (``invalid_order_by:`` /
``filtered_out:``). The calling server tool is responsible for catching
the exception and mapping it onto a :class:`~mcp_atlassian.utils.dc_guards.StructuredError`
with the matching ``error_code`` before returning to the agent. Keeping
the mixin free of :class:`StructuredError` imports avoids a circular
dependency with ``utils/dc_guards.py`` and matches the mixin pattern
elsewhere in the package (the mixin raises, the server layer maps).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


# Module-level allowlist of CQL fields the ``confluence_cql_search`` tool
# accepts as ``order_by``. These are the fields documented by Confluence
# DC as safe to sort a CQL result set by. Keeping the set small and
# explicit prevents the agent from smuggling arbitrary expressions into
# the order-by clause (which CQL would otherwise interpret as a field
# reference and either error out or silently produce undefined results).
SORTABLE_FIELDS: frozenset[str] = frozenset(
    {"title", "created", "lastmodified", "space", "id", "type"}
)


# Regex fragments used by :meth:`CQLAdvancedMixin.rewrite_cql_for_space_filter`
# to locate existing space restrictions in a user-supplied CQL string. The
# patterns are intentionally simple (per the design's "simplest
# implementation" guidance): they catch the two canonical shapes Confluence
# uses for space filtering (``space = KEY`` and ``space in (A, B, C)``)
# and nothing fancier. A more elaborate parser is out of scope here — the
# server layer can still fall back to the structured ``filtered_out``
# error if the heuristic misses an edge case.
_SPACE_EQ_RE = re.compile(r"space\s*=\s*\"?([^\"\s,\)]+)\"?", re.IGNORECASE)
_SPACE_IN_RE = re.compile(r"space\s+in\s*\(([^)]*)\)", re.IGNORECASE)


def _normalize_order_dir(order_dir: str) -> str:
    """Return a normalized ``asc`` / ``desc`` literal or raise.

    CQL accepts ``ASC`` and ``DESC`` case-insensitively in the ``order by``
    clause but we normalize to lowercase so the emitted query is stable
    and easy to assert against in tests.

    Raises :class:`ValueError` with an ``invalid_order_by:`` prefix on any
    other value. The same prefix is used for the ``order_by`` field check
    so the server layer only needs to recognize one sentinel when mapping
    mixin exceptions to the ``invalid_order_by`` structured error code.
    """
    normalized = (order_dir or "").strip().lower()
    if normalized not in ("asc", "desc"):
        raise ValueError(
            f"invalid_order_by: order_dir {order_dir!r} must be 'asc' or 'desc'."
        )
    return normalized


class CQLAdvancedMixin(ConfluenceClient):
    """Mixin for Confluence advanced CQL search with sort and filter support."""

    # Re-exposed as a class attribute so call sites can reach the allowlist
    # without importing the module-level name. The two bindings point at
    # the same frozenset, so equality checks succeed regardless of which
    # handle the caller used.
    SORTABLE_FIELDS: frozenset[str] = SORTABLE_FIELDS

    def cql_search(
        self,
        cql: str,
        *,
        order_by: str | None = None,
        order_dir: str = "asc",
        limit: int = 25,
        start: int = 0,
    ) -> dict[str, Any]:
        """Run a CQL search with optional explicit sort and pagination.

        When ``order_by`` is supplied, it must be a member of
        :data:`SORTABLE_FIELDS`; otherwise this raises :class:`ValueError`
        with an ``invalid_order_by:`` prefix *before* any HTTP request so
        the server layer can map the condition to the structured
        ``invalid_order_by`` error with zero outbound traffic
        (Requirement 35.2).

        When ``order_by`` is ``None`` the original ``cql`` string is sent
        verbatim — the caller remains free to include their own
        ``order by`` clause inside ``cql`` if they prefer (the API accepts
        at most one). ``order_dir`` is validated separately and defaults
        to ``"asc"``; ``"desc"`` is also accepted (case-insensitive).

        Args:
            cql: The user-supplied CQL query. Passed through unchanged
                except for an optional appended ``order by`` clause.
            order_by: Optional sort field. Must be one of
                :data:`SORTABLE_FIELDS` when non-``None``.
            order_dir: Sort direction; ``"asc"`` (default) or ``"desc"``.
                Matched case-insensitively.
            limit: Maximum number of results to return. Defaults to ``25``
                (matches the Confluence CQL default page size).
            start: Zero-based offset into the result set for pagination.

        Returns:
            The raw CQL search payload as returned by Confluence. The
            caller is responsible for any model conversion — the mixin
            keeps the response as a plain dict so tool code can forward
            the ``results`` / ``_links`` / ``totalSize`` keys without an
            extra round-trip through a model class.

        Raises:
            ValueError: With prefix ``invalid_order_by:`` when
                ``order_by`` is not in :data:`SORTABLE_FIELDS` or
                ``order_dir`` is not ``asc``/``desc``.
        """
        effective_cql = cql
        if order_by is not None:
            if order_by not in SORTABLE_FIELDS:
                raise ValueError(
                    f"invalid_order_by: {order_by!r} is not a CQL-sortable "
                    f"field. Allowed fields: {sorted(SORTABLE_FIELDS)}."
                )
            direction = _normalize_order_dir(order_dir)
            # Append the order-by clause. CQL accepts a single ``order by``
            # per query so we assume the incoming ``cql`` does not already
            # contain one when the caller opted into explicit sorting.
            effective_cql = f"{cql} order by {order_by} {direction}"

        params = {
            "cql": effective_cql,
            "limit": limit,
            "start": start,
        }
        logger.debug(
            "Issuing CQL search: cql=%r, limit=%d, start=%d",
            effective_cql,
            limit,
            start,
        )
        response = self.confluence.get(
            "rest/api/content/search", params=params
        )
        # ``self.confluence.get`` returns ``None`` on an empty body; normalize
        # to an empty dict so callers can rely on a dict contract.
        return response or {}

    def rewrite_cql_for_space_filter(
        self,
        cql: str,
        allowed_spaces: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    ) -> str:
        """Intersect a CQL string with a space allow-list.

        Implements the space-filter half of Requirement 35.3 and the shared
        filter semantics documented in Requirement 43.3. The method
        inspects ``cql`` for existing space restrictions using two simple
        regex shapes:

        * ``space = KEY`` (bare or double-quoted identifier), matched by
          :data:`_SPACE_EQ_RE`.
        * ``space in (A, B, C)`` (comma-separated identifiers, bare or
          double-quoted), matched by :data:`_SPACE_IN_RE`.

        Behavior:

        * When ``allowed_spaces`` is empty, the CQL is returned unchanged
          (no filter configured → no rewrite needed).
        * When the CQL references at least one space and *every* referenced
          space is in ``allowed_spaces``, the CQL is returned unchanged —
          the existing restriction is already more specific than, or equal
          to, the allow-list.
        * When the CQL references spaces and *any* referenced space is
          outside ``allowed_spaces``, :class:`ValueError` is raised with a
          ``filtered_out:`` prefix so the server layer can map the
          condition onto the structured ``filtered_out`` error.
        * When the CQL contains no space clause, ``space in (...)`` using
          the allow-list is prepended with a logical ``AND`` so the
          outbound query is bounded by the allow-list without losing the
          caller's original predicates.

        Args:
            cql: The user-supplied CQL query string.
            allowed_spaces: The operator-configured allow-list (typically
                parsed from ``CONFLUENCE_SPACES_FILTER``). Accepts any
                iterable of space keys; duplicates and casing are
                normalized internally.

        Returns:
            A CQL string guaranteed to be restricted to spaces inside
            ``allowed_spaces``.

        Raises:
            ValueError: With prefix ``filtered_out:`` when the CQL already
                references space keys and at least one of them is outside
                ``allowed_spaces``.
        """
        # Normalize the allow-list once so every downstream comparison is
        # case-insensitive and dedupes natural-language duplicates (e.g. a
        # trailing whitespace in the env var).
        allowed_normalized = {
            s.strip().upper() for s in allowed_spaces if s and s.strip()
        }
        if not allowed_normalized:
            # No allow-list configured — the caller's CQL passes through
            # unchanged. The server layer's project-filter guard already
            # short-circuits this case, but keeping the behavior defensive
            # here makes the mixin safe to call outside a guarded tool.
            return cql

        referenced: set[str] = set()

        # Gather `space = KEY` matches.
        for match in _SPACE_EQ_RE.finditer(cql):
            referenced.add(match.group(1).strip().upper())

        # Gather `space in (A, B, C)` matches.
        for match in _SPACE_IN_RE.finditer(cql):
            inner = match.group(1)
            for token in inner.split(","):
                key = token.strip().strip('"').strip()
                if key:
                    referenced.add(key.upper())

        if referenced:
            disallowed = referenced - allowed_normalized
            if disallowed:
                raise ValueError(
                    f"filtered_out: CQL references space(s) "
                    f"{sorted(disallowed)} outside the configured "
                    f"allow-list {sorted(allowed_normalized)}."
                )
            # Every referenced space is already inside the allow-list, so
            # the restriction is acceptable as-is.
            return cql

        # No space clause in the CQL — prepend one using the allow-list.
        # Sort for deterministic output (eases testing and log diffing).
        space_list = ", ".join(sorted(allowed_normalized))
        prefix = f"space in ({space_list})"
        if cql and cql.strip():
            return f"{prefix} AND ({cql})"
        return prefix
