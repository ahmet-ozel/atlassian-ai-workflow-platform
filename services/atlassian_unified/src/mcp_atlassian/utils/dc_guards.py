"""Cross-cutting DC guard helpers for MCP Atlassian tools.

This module centralizes the pre-HTTP checks that every new DC-only tool runs
before issuing an outbound request: read-only enforcement, project/space
filter enforcement, DC version gating, owner-scoped delete resolution, and
reversible-receipt construction.

The structured error primitives (``StructuredError`` and the ``ERROR_CODES``
allowlist), ``check_read_only``, ``check_project_filter``, the
``DCVersionProbe`` mixin, the ``parse_dc_version`` /
``compare_dc_versions`` helpers, ``check_dc_version``, ``require_owner``,
and ``build_receipt`` are all implemented here. This completes the
cross-cutting guard module defined by the ``atlassian-dc-tool-parity``
spec; product-specific mixins and tools in subsequent tasks compose the
functions exported from this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mcp_atlassian.utils.io import is_read_only_mode

# Allowlist of structured error codes emitted by DC guards and tools.
#
# Every StructuredError.error_code MUST be a member of this frozenset.
# Matches the 13-entry allowlist defined in the ``atlassian-dc-tool-parity``
# design document, extended by the ``bitbucket-cloud-dc-parity`` design
# with ``not_supported_on_cloud`` and ``not_supported_on_dc`` for the
# mode-support guard (Requirements 15.1, 15.2, 15.3).
ERROR_CODES: frozenset[str] = frozenset(
    {
        "read_only_mode",
        "filtered_out",
        "dc_version_too_old",
        "dc_version_unknown",
        "plugin_unavailable",
        "not_owner",
        "not_filter_owner",
        "not_comment_author",
        "invalid_visibility",
        "invalid_target",
        "invalid_order_by",
        "long_task_not_found",
        "cherry_pick_conflict",
        "not_supported_on_cloud",
        "not_supported_on_dc",
    }
)


@dataclass(frozen=True)
class StructuredError:
    """Structured error returned by DC guards and tool pre-checks.

    Instances are frozen so guard callers can safely pass them around and
    serialize them into tool responses without risking mutation.

    Attributes:
        error_code: One of the codes in ``ERROR_CODES``. Validated at
            construction time to prevent ad-hoc error codes leaking into
            tool responses.
        message: Human-readable message describing the condition.
        details: Optional structured context (e.g.
            ``{"required_version": "9.4", "detected_version": "9.2"}``).
            Defaults to an empty dict. Values must be JSON-serializable so
            the error can be returned directly by tool functions.
    """

    error_code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error_code not in ERROR_CODES:
            raise ValueError(
                f"Unknown error_code {self.error_code!r}; "
                f"must be one of {sorted(ERROR_CODES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict form of the error.

        The shape matches what tool functions splat into their
        ``{"success": False, ...}`` response payloads.
        """
        return {
            "error_code": self.error_code,
            "message": self.message,
            "details": dict(self.details),
        }


def check_read_only(tool_tags: set[str]) -> StructuredError | None:
    """Enforce global read-only mode as a pre-HTTP precheck.

    This is the first guard every DC tool runs before issuing any outbound
    request. It is a belt-and-suspenders check: the MCP list-tools filter
    already hides ``write``-tagged tools when ``READ_ONLY_MODE=true``, but a
    client that invokes a tool by name directly must still be blocked before
    any HTTP side effect.

    The guard returns ``None`` (allowed) in every case except when both of the
    following hold:

    - ``READ_ONLY_MODE`` is set to a truthy value (``true``, ``1``, ``yes``,
      ``y``, ``on``; case-insensitive) in the process environment.
    - ``"write"`` is a member of ``tool_tags``.

    When both hold, the guard returns a ``StructuredError`` with
    ``error_code="read_only_mode"`` so the calling tool can splat the dict
    form into its ``{"success": False, ...}`` response without issuing any
    outbound HTTP request.

    Args:
        tool_tags: The full tag set of the invoking tool (for example
            ``{"bitbucket", "write", "toolset:bitbucket_webhooks"}``). Only
            the presence of ``"write"`` is inspected; other tags are ignored.

    Returns:
        ``None`` when the tool may proceed, or a ``StructuredError`` with
        ``error_code="read_only_mode"`` when the invocation must be blocked.
    """
    if "write" not in tool_tags:
        return None
    if not is_read_only_mode():
        return None
    return StructuredError(
        error_code="read_only_mode",
        message=(
            "Server is running in read-only mode; write tools are disabled. "
            "Unset READ_ONLY_MODE or set it to false to enable write tools."
        ),
        details={"read_only_mode": True},
    )


def check_project_filter(
    product: Literal["bitbucket", "jira", "confluence"],
    key: str,
    filter_env: str | None,
) -> StructuredError | None:
    """Enforce the per-product project/space allow-list as a pre-HTTP precheck.

    Every DC tool that operates on a specific project (Bitbucket, Jira) or
    space (Confluence) invokes this guard after ``check_read_only`` and
    before any outbound HTTP request. When the operator has configured the
    corresponding filter environment variable (``BITBUCKET_PROJECTS_FILTER``,
    ``JIRA_PROJECTS_FILTER``, or ``CONFLUENCE_SPACES_FILTER``), only keys in
    that comma-separated allow-list may be acted upon. Comparison is
    case-insensitive: both the allow-list tokens and ``key`` are uppercased
    before membership is tested.

    The guard returns ``None`` (allowed) in every case except when both of
    the following hold:

    - ``filter_env`` is a non-empty string (after stripping whitespace).
    - The uppercased ``key`` is not present in the uppercased, comma-split
      allow-list.

    When both hold, the guard returns a ``StructuredError`` with
    ``error_code="filtered_out"`` so the calling tool can splat the dict
    form into its ``{"success": False, ...}`` response without issuing any
    outbound HTTP request.

    Empty tokens produced by stray commas (for example ``"FOO,,BAR"`` or
    trailing/leading commas) are discarded so operators can comma-delimit
    without producing a phantom ``""`` allow-list entry.

    Args:
        product: Which product the filter applies to. Used only in the
            human-readable ``message`` field of the returned error so the
            operator can tell which env var to adjust; the comparison logic
            itself does not vary by product.
        key: The project key (Bitbucket/Jira) or space key (Confluence) the
            tool is about to act upon. Compared case-insensitively.
        filter_env: The raw value of the corresponding env var, exactly as
            read from configuration (for example
            ``os.environ.get("BITBUCKET_PROJECTS_FILTER")`` or the matching
            attribute on the fetcher config). ``None`` or an empty /
            whitespace-only string means "no filter configured" and the
            guard allows every key through.

    Returns:
        ``None`` when the tool may proceed, or a ``StructuredError`` with
        ``error_code="filtered_out"`` when the key is not in the configured
        allow-list.
    """
    if filter_env is None:
        return None
    if not filter_env.strip():
        return None

    allowed: list[str] = [
        token.strip().upper() for token in filter_env.split(",") if token.strip()
    ]
    if not allowed:
        # All tokens were blank (for example ``","`` or ``", ,"``); treat as
        # "no filter configured" rather than denying every key.
        return None

    if key.upper() in allowed:
        return None

    return StructuredError(
        error_code="filtered_out",
        message=(
            f"{product.capitalize()} key {key!r} is not in the configured "
            f"allow-list; adjust the corresponding *_FILTER env var or use "
            f"an allowed key."
        ),
        details={
            "product": product,
            "key": key,
            "allowed": allowed,
        },
    )


# ---------------------------------------------------------------------------
# DC version probe + semver-lite comparison
# ---------------------------------------------------------------------------


def parse_dc_version(s: str | None) -> tuple[int, ...] | None:
    """Parse an Atlassian DC version string into a tuple of integer segments.

    DC reports its version in a handful of shapes: two-segment (``"9.4"``),
    three-segment (``"9.4.0"``), four-segment build-suffixed
    (``"9.4.0.1"``), and pre-release / snapshot tagged
    (``"5.4-SNAPSHOT"``, ``"8.8.0-beta1"``). This parser is deliberately
    permissive: it reads leading numeric dot-separated segments and stops at
    the first non-numeric boundary so that both plain and tagged versions
    produce a clean integer tuple suitable for element-wise comparison.

    Parsing rules:

    - ``None``, empty, or whitespace-only input returns ``None``.
    - Anything after the first non-numeric, non-dot character (for example
      ``"-"`` in ``"5.4-SNAPSHOT"``, ``" "`` in ``"9.4 (build 1)"``, or an
      embedded letter) is discarded before splitting.
    - The remaining string is split on ``"."`` and each segment is parsed
      with :func:`int`. A segment that is not a valid integer terminates
      parsing at that point (shorter tuple is returned rather than
      erroring); this gracefully handles patterns like ``"9.4.x"`` by
      producing ``(9, 4)``.
    - If no numeric segments can be recovered, returns ``None`` so the
      caller can treat the version as indeterminate.

    Examples::

        parse_dc_version("9.4")           -> (9, 4)
        parse_dc_version("9.4.0")         -> (9, 4, 0)
        parse_dc_version("9.4.0.1")       -> (9, 4, 0, 1)
        parse_dc_version("5.4-SNAPSHOT")  -> (5, 4)
        parse_dc_version("8.8.0-beta1")   -> (8, 8, 0)
        parse_dc_version("9.4.x")         -> (9, 4)
        parse_dc_version("")              -> None
        parse_dc_version(None)            -> None
        parse_dc_version("not-a-version") -> None

    Args:
        s: Raw version string as reported by the DC server (for example the
            ``version`` field of ``/rest/api/2/serverInfo`` or the
            ``displayName`` from ``/rest/api/latest/application-properties``),
            or ``None`` when the fetcher has not successfully probed yet.

    Returns:
        A tuple of integer segments (for example ``(9, 4, 0)``), or
        ``None`` when the input is missing, blank, or cannot yield any
        numeric segment.
    """
    if s is None:
        return None

    trimmed = s.strip()
    if not trimmed:
        return None

    # Keep only the leading prefix of digits and dots. This handles all of
    # "5.4-SNAPSHOT", "8.8.0-beta1", "9.4 (build 1)", "9.4.x" uniformly by
    # cutting at the first non-numeric, non-dot character.
    prefix_chars: list[str] = []
    for ch in trimmed:
        if ch.isdigit() or ch == ".":
            prefix_chars.append(ch)
        else:
            break
    prefix = "".join(prefix_chars)
    if not prefix:
        return None

    segments: list[int] = []
    for raw in prefix.split("."):
        if not raw:
            # Empty segment produced by leading/trailing/double dot; stop
            # rather than coerce to zero so the tuple reflects only the
            # segments the server actually reported.
            break
        try:
            segments.append(int(raw))
        except ValueError:
            # Defensive: the prefix filter above should guarantee integer
            # segments, but if a future parsing rule relaxes that, stop at
            # the first non-integer segment rather than raising.
            break

    if not segments:
        return None
    return tuple(segments)


def compare_dc_versions(detected: str | None, required: str) -> int | None:
    """Compare a detected DC version against a required minimum.

    Both inputs are parsed with :func:`parse_dc_version`, then padded with
    trailing zeros to equal length and compared element-wise. This matches
    the intuition ``"9.4.0" >= "9.4"`` (equal) and ``"9.2.1" >= "9.4"``
    false, without needing a full PEP 440 implementation.

    The comparison returns ``None`` (indeterminate) in two cases:

    - ``detected`` is ``None`` or cannot be parsed by
      :func:`parse_dc_version`. The calling guard is expected to fall
      through so the tool body can attempt the call and map any upstream
      404/501 to ``dc_version_unknown`` per Requirement 45.3.
    - ``required`` cannot be parsed. This should not happen in practice
      because call sites hard-code minima like ``"5.4"`` or ``"9.4"``, but
      we treat it as indeterminate rather than raising so a future typo in
      a tool registration cannot crash the server.

    Examples::

        compare_dc_versions("9.4.0", "9.4")    -> 0
        compare_dc_versions("9.4",   "9.4.0")  -> 0
        compare_dc_versions("9.4.1", "9.4")    -> 1
        compare_dc_versions("9.2.1", "9.4")    -> -1
        compare_dc_versions(None,    "9.4")    -> None
        compare_dc_versions("bogus", "9.4")    -> None

    Args:
        detected: The DC version string cached on the fetcher (typically
            ``fetcher._dc_version``), or ``None`` when the probe has not
            succeeded.
        required: The tool's documented minimum version, for example
            ``"5.4"`` or ``"9.4"``. Always a non-empty dotted-integer
            string at the call site.

    Returns:
        ``-1`` when ``detected < required``, ``0`` when equal after
        zero-padding, ``1`` when ``detected > required``, or ``None`` when
        the comparison is indeterminate (either operand unparseable).
    """
    d = parse_dc_version(detected)
    r = parse_dc_version(required)
    if d is None or r is None:
        return None

    # Pad shorter tuple with zeros so ("9", "4") and ("9", "4", "0") compare
    # equal. Tuple ordering in Python is element-wise, so once lengths
    # match, a direct comparison is the cheapest option.
    length = max(len(d), len(r))
    d_padded = d + (0,) * (length - len(d))
    r_padded = r + (0,) * (length - len(r))

    if d_padded < r_padded:
        return -1
    if d_padded > r_padded:
        return 1
    return 0


class DCVersionProbe:
    """Mixin that declares the cached DC version contract on fetcher classes.

    The three product clients (:class:`BitbucketClient`, :class:`JiraClient`,
    :class:`ConfluenceClient`) each inherit from this mixin and are
    responsible for populating ``self._dc_version`` on first HTTP call in
    their ``__init__`` (or the first method that needs it). The probe is
    lazy and cached for the instance lifetime: subsequent tool invocations
    reuse the cached value so the guard stays pure-Python after the first
    probe.

    Probe endpoints per product (implemented in the client classes in
    tasks 2.2-2.4 of the ``atlassian-dc-tool-parity`` spec):

    - Bitbucket  ``GET /rest/api/latest/application-properties``
    - Jira       ``GET /rest/api/2/serverInfo``
    - Confluence ``GET /rest/applinks/latest/manifest`` with fallback

    This mixin deliberately does not implement the HTTP call itself.
    Centralizing the probe here would force the guard module to depend on
    every product client's HTTP stack, which is the wrong direction.
    Instead, the mixin documents the contract (the attribute name, its
    meaning, and the ``None`` sentinel) so :func:`check_dc_version`
    (implemented in task 2.5) and every DC-gated tool can rely on a single
    field shape across products.

    Attributes:
        _dc_version: The DC version string as reported by the product's
            server-info endpoint (for example ``"9.4.0"`` or
            ``"5.4-SNAPSHOT"``), or ``None`` when the probe has not yet
            succeeded. ``None`` is the "indeterminate" sentinel and causes
            :func:`check_dc_version` to fall through to the upstream call
            so a 404/501 can be mapped to ``dc_version_unknown``.
    """

    _dc_version: str | None = None


def check_dc_version(fetcher: Any, required: str) -> StructuredError | None:
    """Enforce the DC minimum-version gate for a tool as a pre-HTTP precheck.

    This is the third guard every DC-gated tool runs, after
    :func:`check_read_only` and :func:`check_project_filter` and before any
    outbound HTTP request to the business endpoint. It compares the
    fetcher's cached DC version against a tool-specific minimum (for
    example ``"5.4"`` for webhooks, ``"7.10"`` for deployments, ``"8.8"``
    for PR comment reactions, ``"9.4"`` for Jira archive/restore). When
    the detected version is below the minimum, the guard returns a
    :class:`StructuredError` with ``error_code="dc_version_too_old"`` so
    the tool can splat the dict form into its
    ``{"success": False, ...}`` response without issuing any outbound
    HTTP traffic.

    The probe itself is owned by the product client
    (:class:`BitbucketClient`, :class:`JiraClient`,
    :class:`ConfluenceClient`) and is lazy: the first call to
    ``fetcher.get_dc_version()`` issues the probe request and caches the
    result on ``self._dc_version``; subsequent invocations reuse the
    cached value. This guard prefers ``fetcher.get_dc_version()`` when
    available so the probe fires on the first DC-gated tool call rather
    than requiring the client constructor to block on the probe. Clients
    that do not expose ``get_dc_version()`` fall back to reading
    ``self._dc_version`` directly.

    Indeterminate handling (Requirement 45.3): when the fetcher reports
    ``None`` (probe not yet run, probe failed, or cached failure), the
    guard returns ``None`` so the tool body can proceed to the business
    call and map a resulting upstream 404 / 501 to ``dc_version_unknown``.
    The same fall-through applies when the detected string is present
    but unparseable (for example a future server returns something
    :func:`parse_dc_version` cannot normalize): treating both cases
    identically keeps the failure mode consistent.

    Args:
        fetcher: The product client instance (``BitbucketClient``,
            ``JiraClient``, or ``ConfluenceClient``). Must either expose a
            callable ``get_dc_version()`` that returns ``str | None`` or a
            ``_dc_version`` attribute of the same type. ``Any`` is used in
            the signature so the guard does not force an import cycle
            between this module and the product client packages.
        required: The tool's documented minimum DC version as a
            dotted-integer string (for example ``"5.4"``, ``"7.10"``,
            ``"8.8"``, ``"9.4"``). Always a non-empty literal at the
            call site.

    Returns:
        ``None`` when the tool may proceed. This happens when (a) the
        detected version is greater than or equal to ``required`` or
        (b) the detected version is ``None`` / unparseable and the
        tool should fall through to the upstream call so the response
        can be mapped to ``dc_version_unknown``.

        A :class:`StructuredError` with
        ``error_code="dc_version_too_old"`` and
        ``details={"required_version": required, "detected_version": detected}``
        when the detected version is strictly below ``required``.
    """
    # Prefer the lazy accessor when the fetcher exposes one: this fires
    # the probe on the first DC-gated tool call rather than forcing every
    # client constructor to block on an extra HTTP request at startup.
    # Fall back to the cached attribute for any fetcher that has not (yet)
    # adopted the accessor pattern.
    getter = getattr(fetcher, "get_dc_version", None)
    if callable(getter):
        detected = getter()
    else:
        detected = getattr(fetcher, "_dc_version", None)

    cmp = compare_dc_versions(detected, required)
    if cmp is None:
        # Indeterminate — fetcher has no cached version, the cached value
        # is unparseable, or ``required`` is malformed. Fall through so
        # the tool body can attempt the upstream call and map 404/501 to
        # ``dc_version_unknown`` per Requirement 45.3.
        return None
    if cmp < 0:
        return StructuredError(
            error_code="dc_version_too_old",
            message=(
                f"Detected DC version {detected!r} is below the minimum "
                f"{required!r} required by this tool."
            ),
            details={
                "required_version": required,
                "detected_version": detected,
            },
        )
    return None


# ---------------------------------------------------------------------------
# Owner-scoped delete resolution
# ---------------------------------------------------------------------------


def require_owner(fetcher: Any, object_owner_id: str) -> StructuredError | None:
    """Enforce that the authenticated user owns the target object.

    This guard is invoked by owner-scoped destructive tools (for example
    ``jira_delete_own_filter``) after the read-only, project-filter, and
    DC-version prechecks and immediately before any DELETE request. The
    caller is responsible for having already resolved ``object_owner_id``
    via a read endpoint (for example ``GET /rest/api/2/filter/{id}`` to
    pull the filter's ``owner.name``). This function does not issue any
    HTTP traffic of its own — it performs a pure identity comparison and
    fails closed so a mismatch produces a structured error rather than a
    silent destructive call.

    Atlassian Data Center identifies users by the ``name`` field (the
    legacy username) rather than the Cloud ``accountId``. The fetcher
    configuration in every product stores this value as
    ``fetcher.config.username``. We prefer that attribute, and fall back
    to a cached ``_current_user_name`` attribute if a future client caches
    the myself-lookup result there. When neither is resolvable, the guard
    returns ``not_owner`` so the delete is blocked rather than proceeding
    against an indeterminate identity ("fail closed").

    Comparison rule:

    - Both names are stripped of surrounding whitespace and lowercased
      before equality is tested. DC usernames are case-insensitive at the
      application level (two accounts cannot differ only in case), so a
      case-insensitive compare matches server semantics and avoids false
      mismatches when callers pass mixed-case values.
    - Any falsy authenticated name (``None`` or empty after stripping)
      is treated as unresolvable and returns ``not_owner``.
    - A falsy ``object_owner_id`` likewise returns ``not_owner`` rather
      than being treated as a match against a missing authenticated
      identity.

    Args:
        fetcher: The product client instance (``BitbucketClient``,
            ``JiraClient``, or ``ConfluenceClient``). Inspected for
            ``fetcher.config.username`` first and then for
            ``fetcher._current_user_name`` as a fallback. ``Any`` is used
            in the signature so this module does not force an import
            cycle with the product client packages.
        object_owner_id: The DC ``name`` of the object's current owner,
            resolved by the caller from the matching read endpoint.

    Returns:
        ``None`` when the authenticated user's name matches
        ``object_owner_id`` (case-insensitive, whitespace-stripped).

        A :class:`StructuredError` with ``error_code="not_owner"`` and
        ``details={"object_owner_id": object_owner_id, "authenticated_user": authenticated_user_name}``
        when the names do not match or the authenticated user cannot be
        resolved. ``authenticated_user`` is ``None`` in the unresolvable
        case so the caller can surface that condition distinctly in
        logs.
    """
    # Resolve the authenticated user's DC ``name``. Prefer the canonical
    # location on the fetcher's config over any ad-hoc cache so we stay
    # aligned with how the product clients authenticate in the first
    # place (basic auth / PAT configured with a username).
    authenticated_user_name: str | None = None
    config = getattr(fetcher, "config", None)
    if config is not None:
        cfg_username = getattr(config, "username", None)
        if isinstance(cfg_username, str) and cfg_username.strip():
            authenticated_user_name = cfg_username

    if authenticated_user_name is None:
        # Fallback: a future client may cache the ``myself`` response
        # under this attribute. Using ``getattr`` keeps this guard
        # tolerant of clients that have not adopted the cache yet.
        cached = getattr(fetcher, "_current_user_name", None)
        if isinstance(cached, str) and cached.strip():
            authenticated_user_name = cached

    # Atlassian Cloud deployments do not expose ``name`` / ``key`` on
    # user objects; owners surface as ``accountId`` instead. The DC
    # username compare above will never match a Cloud accountId, so we
    # additionally accept a match against the authenticated user's
    # Cloud account id. Any fetcher method named
    # ``get_current_user_account_id`` that returns a non-empty string is
    # treated as the source of truth. The call is wrapped in a
    # try/except so a transient failure here still lets the DC compare
    # proceed.
    authenticated_account_id: str | None = None
    get_acct = getattr(fetcher, "get_current_user_account_id", None)
    if callable(get_acct):
        try:
            acct = get_acct()
            if isinstance(acct, str) and acct.strip():
                authenticated_account_id = acct
        except Exception:  # noqa: BLE001 — best-effort lookup
            authenticated_account_id = None

    # Fail closed on unresolvable identity or missing owner id.
    if (
        not authenticated_user_name
        and not authenticated_account_id
    ) or not isinstance(object_owner_id, str):
        return StructuredError(
            error_code="not_owner",
            message=(
                "Authenticated user could not be resolved or the object's "
                "owner is missing; refusing to issue a destructive call."
            ),
            details={
                "object_owner_id": object_owner_id,
                "authenticated_user": authenticated_user_name,
            },
        )

    owner_normalized = object_owner_id.strip().lower()
    auth_normalized = (
        authenticated_user_name.strip().lower() if authenticated_user_name else ""
    )
    acct_normalized = (
        authenticated_account_id.strip().lower()
        if authenticated_account_id
        else ""
    )
    if not owner_normalized:
        return StructuredError(
            error_code="not_owner",
            message=(
                "Object owner id is empty; refusing to issue a destructive "
                "call without a resolved owner to match against."
            ),
            details={
                "object_owner_id": object_owner_id,
                "authenticated_user": authenticated_user_name,
            },
        )

    if owner_normalized == auth_normalized or (
        acct_normalized and owner_normalized == acct_normalized
    ):
        return None

    return StructuredError(
        error_code="not_owner",
        message=(
            f"Authenticated user {authenticated_user_name!r} is not the "
            f"owner of the target object (owner is {object_owner_id!r}); "
            f"destructive call blocked."
        ),
        details={
            "object_owner_id": object_owner_id,
            "authenticated_user": authenticated_user_name,
        },
    )


# ---------------------------------------------------------------------------
# Reversible-receipt construction
# ---------------------------------------------------------------------------


def build_receipt(
    object_id: str,
    inverse_tool: str | None,
    inverse_args: dict[str, Any] | None,
    note: str | None,
    recipient_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a reversible-receipt dict for a successful Write_Tool call.

    Broadcast-capable and owner-scoped destructive Write_Tools (for example
    ``bitbucket_create_webhook``, ``bitbucket_cherry_pick_commit``,
    ``jira_notify_issue``, ``jira_archive_issue``,
    ``confluence_set_content_restrictions``, ``confluence_archive_page``)
    embed the dict returned here under the ``"receipt"`` key of their
    ``{"success": True, ...}`` response payload. Callers that can be
    undone supply the ``inverse_tool`` and ``inverse_args`` needed to roll
    back the effect; callers that cannot be undone (email sends, cherry
    picks that mutate history) instead supply a human-readable ``note``
    explaining the non-retractable nature.

    This function is deliberately pure: it performs no I/O, has no
    dependency on the product fetchers, and does not mutate the arguments
    passed in. The returned dict is a fresh object with exactly the five
    keys defined in the design — ``object_id``, ``inverse_tool``,
    ``inverse_args``, ``note``, ``recipient_scope`` — in that order. Keys
    are always present (even when their value is ``None``) so downstream
    consumers can rely on a stable shape across tools and across both
    retractable and non-retractable cases.

    All values are required to be JSON-serializable because the receipt is
    serialized by the tool function into its JSON response. Enforcement is
    structural rather than runtime: callers pass primitive / dict / list
    values built from their tool arguments and the DC response body, both
    of which are already JSON-shaped. The ``dict[str, Any]`` typing for
    ``inverse_args`` and ``recipient_scope`` reflects that leaves may be
    any JSON scalar (str/int/float/bool/None) or nested list/dict of the
    same.

    Args:
        object_id: Stable identifier of the created or mutated object (for
            example the webhook id, the target-branch commit hash, the
            archived issue key, or the page id). Stringified by the
            caller so the receipt shape is uniform across products that
            use integer vs. string ids upstream.
        inverse_tool: Name of the Write_Tool that undoes the effect (for
            example ``"bitbucket_delete_webhook"`` or
            ``"confluence_restore_archived_page"``), or ``None`` when the
            effect is not retractable. When ``None``, ``inverse_args``
            MUST also be ``None`` and ``note`` SHOULD explain why.
        inverse_args: Keyword arguments the agent can pass to
            ``inverse_tool`` to roll back the effect, or ``None`` when
            not retractable. The dict must be JSON-serializable.
        note: Human-readable note accompanying the receipt. Required for
            non-retractable effects (for example
            ``"Email sends are not retractable"`` on
            ``jira_notify_issue``) and ``None`` for plain retractable
            cases where the inverse-tool invocation speaks for itself.
        recipient_scope: Optional structured summary of the broadcast
            scope (for example ``{"url": url, "events": events}`` for a
            webhook creation or ``{"recipient_count": 12}`` for a notify
            call). ``None`` when the tool is not broadcast-capable; the
            key is still emitted in the returned dict with a ``None``
            value so consumers can rely on a stable shape.

    Returns:
        A fresh dict with exactly the keys
        ``{"object_id", "inverse_tool", "inverse_args", "note", "recipient_scope"}``
        and JSON-serializable values.
    """
    return {
        "object_id": object_id,
        "inverse_tool": inverse_tool,
        "inverse_args": inverse_args,
        "note": note,
        "recipient_scope": recipient_scope,
    }


# ---------------------------------------------------------------------------
# Cloud/DC mode-support guard
# ---------------------------------------------------------------------------


def check_mode_supported(
    is_cloud: bool,
    required_mode: Literal["cloud", "dc"],
    tool_name: str,
) -> StructuredError | None:
    """Enforce that a tool's required Bitbucket mode matches the effective mode.

    This guard is a pure precheck that runs before any outbound HTTP call.
    It is invoked by DC-only tools (for example ``bitbucket_render_markup``,
    ``bitbucket_fork_repository``, ``bitbucket_cherry_pick_commit``) when
    they have no Cloud-side counterpart, and is reserved for future
    Cloud-only tools via the symmetric ``not_supported_on_dc`` code. The
    guard issues zero outbound HTTP and returns a value rather than
    raising, so the calling tool can splat the dict form into its
    ``{"success": False, ...}`` response without any side effects
    (Requirements 14.1-14.10, 15.4, 15.5).

    The effective mode is derived directly from the boolean ``is_cloud``
    argument supplied by the caller (typically ``bb.is_cloud`` for a
    request-scoped :class:`BitbucketClient`): ``True`` means ``"cloud"``,
    ``False`` means ``"dc"``. When the effective mode equals the
    requested ``required_mode``, the guard returns ``None`` (allowed).
    When they differ, the guard emits a :class:`StructuredError` whose
    ``error_code`` is determined by the effective mode — ``"not_supported_on_cloud"``
    when the tool is invoked in CloudMode (but requires DC), and
    ``"not_supported_on_dc"`` when the tool is invoked in DCMode (but
    requires Cloud).

    The ``details`` payload is fixed to three keys: ``"tool"`` (the
    ``tool_name`` argument as provided), ``"effective_mode"`` (``"cloud"``
    or ``"dc"`` — the mode the server is currently operating in), and
    ``"required_mode"`` (the mode the tool needs — always the literal
    passed by the caller). This shape matches the design document's
    error-code table and lets agents programmatically distinguish a
    mode-mismatch from other structured errors without string parsing
    the human-readable message.

    Args:
        is_cloud: The effective-mode boolean for the current request
            (typically ``bb.is_cloud`` on a :class:`BitbucketClient`).
            ``True`` selects CloudMode; ``False`` selects DCMode.
        required_mode: The mode the tool requires; one of ``"cloud"`` or
            ``"dc"``. Typed as a ``Literal`` so static checkers catch
            typos at the call site. Every current DC-only tool passes
            ``"dc"``; the ``"cloud"`` branch is reserved for future
            Cloud-only tools.
        tool_name: The fully-qualified tool name (for example
            ``"bitbucket_render_markup"``). Used verbatim in both the
            human-readable ``message`` and the structured
            ``details["tool"]`` field so the operator and the agent can
            correlate the error with the exact tool that produced it.

    Returns:
        ``None`` when the effective mode matches ``required_mode`` and
        the tool may proceed to its business logic.

        A :class:`StructuredError` with
        ``error_code="not_supported_on_cloud"`` (CloudMode + DC-only
        tool) or ``error_code="not_supported_on_dc"`` (DCMode +
        Cloud-only tool), and
        ``details={"tool": tool_name, "effective_mode": effective, "required_mode": required_mode}``
        when the modes do not match.
    """
    effective: Literal["cloud", "dc"] = "cloud" if is_cloud else "dc"
    if effective == required_mode:
        return None

    # Effective mode differs from required: pick the code that names the
    # mode the server IS running in, since that is the condition the
    # operator needs to change to unblock the call.
    code = (
        "not_supported_on_cloud" if effective == "cloud" else "not_supported_on_dc"
    )
    return StructuredError(
        error_code=code,
        message=(
            f"Tool {tool_name!r} is not supported on {effective} Bitbucket; "
            f"it requires {required_mode} mode."
        ),
        details={
            "tool": tool_name,
            "effective_mode": effective,
            "required_mode": required_mode,
        },
    )
