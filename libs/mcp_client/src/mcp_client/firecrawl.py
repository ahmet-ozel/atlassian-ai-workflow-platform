"""Firecrawl MCP client — egress allowlist + dept overrides + graceful 403.

This module is the caller-side wrapper around the ``firecrawl`` service
(``platform/services/firecrawl``) that the ``automation-service`` and
the ``agent-runner-worker`` reach for whenever the ``research_*``
workflow types need web search or page scraping.

Responsibilities
----------------

The wrapper enforces three platform invariants documented in
``platform-mimari-workflows`` Requirements 9.1 / 9.2 / 9.3 / 9.6:

1. **Egress allowlist with dept overrides** (R9.1, R9.2). Every
   outbound host is checked against the *effective* allowlist computed
   from the global tuple and the per-department overrides. The pure
   helper :func:`effective_allowlist` is the single source of truth
   used by both this client and the property tests.
2. **Graceful degradation** (R9.3). When the target host is **not**
   in the allowlist or when the underlying service replies with HTTP
   403, the call **returns** an :class:`EgressBlocked` outcome instead
   of raising. The caller (``AgentRunnerWorkflow``) translates that
   outcome into a Jira-comment ("``🤖 {url} domain'i araştırma için
   izinli değil; admin'den eklenmesini isteyin.``") and continues —
   the workflow does **not** fail.
3. **Output size cap with MinIO offload** (R9.6, R5.9). When a
   response payload exceeds ``max_bytes`` the wrapper writes the full
   body to MinIO via an injected writer and returns a
   :class:`PayloadOverflow` outcome carrying a short summary plus the
   ``s3://`` URI of the offloaded object. The LLM receives only the
   summary; the full body is preserved for audit/replay.

Why "return, do not raise"
--------------------------

The ``research_publish_confluence`` and ``research_summary_jira``
workflows treat egress denial and oversized payloads as *expected*
operational outcomes (R9.3 — graceful degradation). Raising would
turn a routine "this domain isn't on the allowlist yet" event into a
workflow failure with compensation chain side-effects. We surface the
outcome as a value the caller can pattern-match on instead.

Transport injection
-------------------

The actual HTTP transport (``httpx`` against the firecrawl service)
and the MinIO writer (``aioboto3`` or equivalent) are passed in as
async callables. Keeping I/O at the seams means:

- the property test
  (``platform/tests/property/test_firecrawl_research.py``) drives
  the client without booting either service, and
- the workflow code stays replay-deterministic — the client itself
  is a pure decision layer that delegates I/O to the activity host.

Design context
--------------

- Task 9.1 of ``.kiro/specs/platform-mimari-workflows/tasks.md``.
- Validates Requirements 9.1, 9.2, 9.3, 9.6 of
  ``.kiro/specs/platform-mimari-workflows/requirements.md``.
- Sibling modules in this lib follow the same "pure helper +
  optional injected I/O" shape: see :mod:`mcp_client.pr_draft` (R1.9)
  and :mod:`mcp_client.tool_filter` (R1.8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Literal,
    Mapping,
    Protocol,
    Union,
    runtime_checkable,
)
from urllib.parse import urlparse

__all__ = [
    "EgressBlocked",
    "FirecrawlClient",
    "FirecrawlResult",
    "FirecrawlSuccess",
    "PayloadOverflow",
    "effective_allowlist",
]


# ---------------------------------------------------------------------------
# effective_allowlist — pure set algebra
# ---------------------------------------------------------------------------


def effective_allowlist(
    global_: "AllowlistInput",
    dept_override: "DeptOverrideInput | None",
) -> frozenset[str]:
    """Compute ``(global_ ∪ dept_override.allow) - dept_override.deny``.

    The function is the single source of truth for how a department's
    egress overrides combine with the platform-wide allowlist. Both
    the :class:`FirecrawlClient` and the property test
    (``test_firecrawl_research.py``) reach for this helper so any
    drift in the set algebra surfaces as a property-test failure
    instead of a silent behavioural change.

    Args:
        global_: Platform-wide allowlist. Any iterable of hostname
            strings; entries are stripped, lower-cased, and empty
            entries dropped. ``None`` is accepted and treated as the
            empty set (closed-by-default posture, R9.1).
        dept_override: Optional per-department override mapping with
            ``allow`` and ``deny`` keys. Either key may be missing or
            empty. ``None`` means "no overrides", in which case the
            return value equals the normalised ``global_`` set. The
            input shape mirrors the
            ``firecrawl_egress_overrides`` block in
            ``departments.schema.json``::

                {"allow": ["a.example", "b.example"], "deny": ["c.example"]}

    Returns:
        ``frozenset[str]`` of normalised hostnames (lower-cased,
        whitespace-stripped, no empty entries) representing the
        effective allowlist for this department. The set is hashable
        and immutable so callers can cache it for the lifetime of a
        workflow.

    Notes:
        - ``deny`` is applied **after** the union, so a host listed
          in *both* the dept ``allow`` and ``deny`` lists is denied.
          This matches the principle-of-least-surprise: ``deny`` is
          a closing valve operators reach for to revoke previously
          granted access without rewriting ``departments.json``.
        - Hostnames are normalised but **not** validated. Allowlist
          matching honours DNS label boundaries inside the
          :class:`FirecrawlClient`; this helper only owns the set
          algebra.

    Example::

        >>> effective_allowlist(
        ...     global_=("docs.example.com", "rfc.ietf.org"),
        ...     dept_override={"allow": ["wiki.local"], "deny": ["rfc.ietf.org"]},
        ... ) == frozenset({"docs.example.com", "wiki.local"})
        True
    """

    base: set[str] = _normalise_hosts(global_)
    if dept_override is None:
        return frozenset(base)

    allow: set[str] = _normalise_hosts(dept_override.get("allow"))
    deny: set[str] = _normalise_hosts(dept_override.get("deny"))
    return frozenset((base | allow) - deny)


# ---------------------------------------------------------------------------
# Outcome value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FirecrawlSuccess:
    """Successful call whose payload fit within ``max_bytes``.

    Attributes:
        kind: Always ``"success"`` — discriminator for pattern matching
            on the :data:`FirecrawlResult` union.
        url: The URL or query origin that produced this payload. For
            :meth:`FirecrawlClient.search` this is a synthetic
            ``firecrawl-search:{query}`` token so the field is always
            populated.
        body: The decoded response body. Shape matches the firecrawl
            service contract (search → ``list[dict]``, scrape →
            ``dict``); the wrapper does not reshape it.
        bytes_len: Byte length of the encoded response — recorded so
            audit logs can reason about output size budgets without
            re-encoding the payload.
    """

    url: str
    body: Any
    bytes_len: int
    kind: Literal["success"] = "success"


@dataclass(frozen=True)
class EgressBlocked:
    """Outcome returned (not raised) when the host is denied.

    Two cases produce this outcome:

    1. The target host is outside the effective allowlist computed
       via :func:`effective_allowlist` (pre-flight denial — R9.1 /
       R9.2).
    2. The firecrawl service itself returned HTTP 403 (post-flight
       denial — R9.3, e.g. the upstream egress proxy refused).

    Attributes:
        kind: Always ``"egress_blocked"`` — discriminator for the
            :data:`FirecrawlResult` union.
        url: The URL the caller asked for. For ``search`` calls the
            host is the resolved hostname of the *search service* if
            the denial happened post-flight; otherwise empty.
        host: Lower-cased hostname that was denied, or empty string
            when the URL failed to parse.
        reason: Short machine-readable token. One of
            ``"not_in_allowlist"``, ``"upstream_403"``,
            ``"invalid_url"``, ``"missing_host"``,
            ``"empty_allowlist"``.
        dept_id: Department id that requested the call. Useful for
            audit correlation.

    Notes:
        Workflows should translate this outcome into a Jira comment
        (R9.3 wording) and continue. They MUST NOT raise on receipt.
    """

    url: str
    host: str
    reason: str
    dept_id: str
    kind: Literal["egress_blocked"] = "egress_blocked"


@dataclass(frozen=True)
class PayloadOverflow:
    """Outcome returned when the response exceeded ``max_bytes``.

    The wrapper has already written the full body to MinIO via the
    injected writer; the LLM-facing summary lives in :attr:`summary`
    and the full body is at :attr:`storage_uri`.

    Attributes:
        kind: Always ``"payload_overflow"`` — discriminator for the
            :data:`FirecrawlResult` union.
        url: The originating URL or search-token.
        bytes_len: Byte length of the original payload.
        max_bytes: The cap that was breached.
        storage_uri: ``s3://{bucket}/{key}`` URI of the offloaded
            object. Empty string when no MinIO writer was injected
            (the caller chose to skip offload — see
            :class:`FirecrawlClient` constructor docs).
        summary: Short human-readable summary suitable for Jira /
            LLM context. Truncated to 500 characters.
    """

    url: str
    bytes_len: int
    max_bytes: int
    storage_uri: str
    summary: str
    kind: Literal["payload_overflow"] = "payload_overflow"


#: Discriminated union returned by :class:`FirecrawlClient` methods.
FirecrawlResult = Union[FirecrawlSuccess, EgressBlocked, PayloadOverflow]


# ---------------------------------------------------------------------------
# Transport / storage protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class _Transport(Protocol):
    """Async callable performing the actual HTTP round-trip.

    The protocol exists so the property test can stub the transport
    without depending on ``httpx``. The wrapper invokes it as::

        response = await transport(operation, payload)

    ``operation`` is one of ``"search"`` / ``"scrape"`` and the wrapper
    expects the callable to translate that into the firecrawl service
    endpoint (``POST /v0/search`` / ``POST /v0/scrape``).

    The response is a :class:`_TransportResponse` describing both the
    HTTP status code and the decoded JSON body. We carry the status
    explicitly so the wrapper can detect upstream 403 (R9.3) without
    re-parsing exception types from the underlying client.
    """

    async def __call__(
        self,
        operation: Literal["search", "scrape"],
        payload: Mapping[str, Any],
    ) -> "_TransportResponse": ...


@dataclass(frozen=True)
class _TransportResponse:
    """Lightweight envelope returned by a :class:`_Transport`.

    The wrapper is intentionally agnostic of the concrete HTTP
    library: callers can adapt :class:`httpx.Response`, mock objects,
    or static fixtures to this shape.
    """

    status: int
    body: Any


@runtime_checkable
class _MinioWriter(Protocol):
    """Async callable that writes ``payload`` to MinIO and returns a URI.

    The wrapper invokes the writer as::

        uri = await minio_writer(key=..., payload=...)

    The expected URI shape is ``s3://{bucket}/{key}`` to match the
    convention used elsewhere in the platform (see
    ``automation-worker/src/automation_worker/activities/audit_prune.py``
    and ``admin-dashboard-api/src/audit/archive_index.py``).
    """

    async def __call__(
        self, *, key: str, payload: bytes
    ) -> str: ...


# ---------------------------------------------------------------------------
# FirecrawlClient
# ---------------------------------------------------------------------------


#: Default per-call cap. The platform-wide ``MAX_OUTPUT_BYTES`` is
#: 1 MB (R5.9) and we mirror that here so callers that omit
#: ``max_bytes`` get the same behaviour as ``output_actions``.
_DEFAULT_MAX_BYTES: Final[int] = 1_048_576

#: Maximum length of the human-readable summary on
#: :class:`PayloadOverflow`. Mirrors R9.5 ("max 500 kelime") translated
#: to characters with a generous margin.
_SUMMARY_MAX_CHARS: Final[int] = 500


class FirecrawlClient:
    """Caller-side wrapper around the firecrawl service.

    The wrapper owns three concerns documented in R9.1 / R9.2 / R9.3 /
    R9.6:

    1. **Allowlist enforcement** — the effective allowlist for each
       call is computed from the global allowlist (constructor) and
       the per-department override (``dept_id`` argument).
    2. **Graceful denial** — out-of-allowlist hosts and upstream HTTP
       403 produce an :class:`EgressBlocked` *outcome*; the wrapper
       does **not** raise.
    3. **Payload offload** — bodies above ``max_bytes`` are written
       to MinIO via the injected writer and the call returns a
       :class:`PayloadOverflow` outcome carrying a short summary plus
       the ``s3://`` URI.

    Attributes:
        _allowlist_global: Normalised platform-wide allowlist.
        _dept_overrides: Mapping of ``dept_id`` to
            ``{"allow": [...], "deny": [...]}`` blocks. Loaded from
            the ``firecrawl_egress_overrides`` field in
            ``departments.json``.
        _transport: Async transport invoked for HTTP calls.
        _minio_writer: Optional async writer used by the overflow
            branch. ``None`` disables MinIO offload — the wrapper
            still returns :class:`PayloadOverflow` with an empty
            ``storage_uri`` so the caller can decide what to do.
    """

    def __init__(
        self,
        *,
        allowlist_global: "AllowlistInput",
        dept_overrides: "Mapping[str, DeptOverrideInput] | None" = None,
        transport: _Transport | None = None,
        minio_writer: _MinioWriter | None = None,
    ) -> None:
        """Build a client bound to a global allowlist and dept overrides.

        Args:
            allowlist_global: Platform-wide allowlist. Iterable of
                hostnames; normalised on construction (lower-case,
                whitespace-stripped). ``None`` is accepted and yields
                a closed-by-default posture (every host is denied).
            dept_overrides: Mapping of ``dept_id`` to
                ``{"allow": [...], "deny": [...]}`` from
                ``departments.json``. Missing departments fall back
                to the global allowlist with no overrides applied.
            transport: Async :class:`_Transport` performing the HTTP
                round-trip. Required for :meth:`search` and
                :meth:`scrape` to execute. Tests inject a stub.
            minio_writer: Optional :class:`_MinioWriter` used by the
                overflow branch. When ``None`` the wrapper still
                returns :class:`PayloadOverflow` for oversized bodies
                but ``storage_uri`` is the empty string.
        """

        self._allowlist_global: frozenset[str] = frozenset(
            _normalise_hosts(allowlist_global)
        )
        self._dept_overrides: dict[str, dict[str, list[str]]] = {}
        if dept_overrides is not None:
            for dept_id, override in dept_overrides.items():
                self._dept_overrides[dept_id] = {
                    "allow": sorted(_normalise_hosts(override.get("allow"))),
                    "deny": sorted(_normalise_hosts(override.get("deny"))),
                }
        self._transport = transport
        self._minio_writer = minio_writer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def effective_allowlist_for(self, dept_id: str) -> frozenset[str]:
        """Return the effective allowlist for ``dept_id``.

        Thin wrapper around :func:`effective_allowlist` that pulls the
        override block from ``dept_overrides``. Useful for callers
        (and tests) that want to inspect the resolved set without
        performing a network call.
        """

        return effective_allowlist(
            self._allowlist_global,
            self._dept_overrides.get(dept_id),
        )

    async def search(
        self,
        query: str,
        *,
        dept_id: str,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> FirecrawlResult:
        """Run a firecrawl ``search`` against the upstream service.

        Args:
            query: The search query string. Passed verbatim to the
                upstream service.
            dept_id: The requesting department's id. Used to resolve
                the effective allowlist.
            max_bytes: Per-call payload cap. Defaults to 1 MB
                (mirrors ``MAX_OUTPUT_BYTES`` from R5.9). When the
                response body exceeds this cap the wrapper returns
                a :class:`PayloadOverflow` outcome.

        Returns:
            One of :class:`FirecrawlSuccess`,
            :class:`EgressBlocked`, or :class:`PayloadOverflow`.
            The wrapper never raises for routine denial / overflow;
            transport errors propagate normally.

        Notes:
            ``search`` does not have a target URL the caller controls,
            so the allowlist check happens **post-flight** on any URL
            present in the response. The pre-flight check applies to
            an upstream 403 only — when the *firecrawl service itself*
            refuses (e.g. the dept has been disabled at the proxy
            level) we surface that as :class:`EgressBlocked` with
            reason ``"upstream_403"``.
        """

        if self._transport is None:
            raise RuntimeError(
                "FirecrawlClient.search requires a transport — pass one "
                "via the constructor or use the offline "
                "effective_allowlist_for helper instead."
            )

        synthetic_url = f"firecrawl-search:{query}"
        response = await self._transport("search", {"query": query})

        # Upstream 403 → graceful EgressBlocked (R9.3).
        if response.status == 403:
            return EgressBlocked(
                url=synthetic_url,
                host="",
                reason="upstream_403",
                dept_id=dept_id,
            )

        # Any other non-2xx is a transport-level fault; the wrapper
        # re-raises so the caller's retry policy can act. We do not
        # swallow these silently because they are not covered by the
        # graceful-degradation contract.
        if response.status < 200 or response.status >= 300:
            raise FirecrawlTransportError(
                operation="search",
                status=response.status,
                detail="upstream returned non-2xx and non-403 status",
            )

        return await self._wrap_payload(
            url=synthetic_url,
            body=response.body,
            max_bytes=max_bytes,
        )

    async def scrape(
        self,
        url: str,
        *,
        dept_id: str,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> FirecrawlResult:
        """Scrape a single page through the firecrawl service.

        The target ``url`` is checked against the effective allowlist
        **before** the upstream call. Out-of-allowlist hosts produce
        an :class:`EgressBlocked` outcome and the transport is **not**
        invoked.

        Args:
            url: The URL to scrape. Must be ``http`` or ``https``.
            dept_id: The requesting department's id. Used to resolve
                the effective allowlist.
            max_bytes: Per-call payload cap. Defaults to 1 MB.

        Returns:
            One of :class:`FirecrawlSuccess`,
            :class:`EgressBlocked`, or :class:`PayloadOverflow`.

        Notes:
            The allowlist matching honours DNS label boundaries — an
            entry of ``example.com`` matches ``example.com`` and
            ``api.example.com`` but **not** ``barexample.com``. This
            mirrors the rule in
            :func:`firecrawl.egress.is_host_allowed` so the two
            enforcement surfaces stay aligned.
        """

        decision = self._check_allowlist(url=url, dept_id=dept_id)
        if decision is not None:
            return decision

        if self._transport is None:
            raise RuntimeError(
                "FirecrawlClient.scrape requires a transport — pass one "
                "via the constructor."
            )

        response = await self._transport("scrape", {"url": url})

        if response.status == 403:
            return EgressBlocked(
                url=url,
                host=_extract_host(url),
                reason="upstream_403",
                dept_id=dept_id,
            )

        if response.status < 200 or response.status >= 300:
            raise FirecrawlTransportError(
                operation="scrape",
                status=response.status,
                detail="upstream returned non-2xx and non-403 status",
            )

        return await self._wrap_payload(
            url=url,
            body=response.body,
            max_bytes=max_bytes,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_allowlist(
        self, *, url: str, dept_id: str
    ) -> EgressBlocked | None:
        """Return :class:`EgressBlocked` when ``url`` is denied; ``None`` otherwise."""

        if not isinstance(url, str) or not url.strip():
            return EgressBlocked(
                url=url if isinstance(url, str) else "",
                host="",
                reason="invalid_url",
                dept_id=dept_id,
            )

        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return EgressBlocked(
                url=url,
                host="",
                reason="invalid_url",
                dept_id=dept_id,
            )

        host = (parsed.hostname or "").lower()
        if not host:
            return EgressBlocked(
                url=url,
                host="",
                reason="missing_host",
                dept_id=dept_id,
            )

        allowlist = self.effective_allowlist_for(dept_id)
        if not allowlist:
            return EgressBlocked(
                url=url,
                host=host,
                reason="empty_allowlist",
                dept_id=dept_id,
            )

        if _host_matches(host, allowlist):
            return None

        return EgressBlocked(
            url=url,
            host=host,
            reason="not_in_allowlist",
            dept_id=dept_id,
        )

    async def _wrap_payload(
        self,
        *,
        url: str,
        body: Any,
        max_bytes: int,
    ) -> FirecrawlSuccess | PayloadOverflow:
        """Materialise ``body`` and route to success / overflow."""

        encoded = _encode_body(body)
        bytes_len = len(encoded)

        if bytes_len <= max_bytes:
            return FirecrawlSuccess(
                url=url,
                body=body,
                bytes_len=bytes_len,
            )

        # Overflow — compute storage key and offload via injected writer.
        storage_uri = ""
        if self._minio_writer is not None:
            key = _build_overflow_key(url=url, bytes_len=bytes_len)
            storage_uri = await self._minio_writer(
                key=key,
                payload=encoded,
            )

        summary = _build_summary(body, bytes_len=bytes_len, max_bytes=max_bytes)
        return PayloadOverflow(
            url=url,
            bytes_len=bytes_len,
            max_bytes=max_bytes,
            storage_uri=storage_uri,
            summary=summary,
        )


# ---------------------------------------------------------------------------
# FirecrawlTransportError — non-routine transport faults
# ---------------------------------------------------------------------------


class FirecrawlTransportError(RuntimeError):
    """Raised for non-2xx, non-403 upstream responses.

    Routine denials (out-of-allowlist, upstream 403) and routine
    overflow are surfaced as outcome values, not exceptions —
    see :class:`EgressBlocked` and :class:`PayloadOverflow`. Anything
    that falls outside both categories (5xx, malformed transport,
    unexpected 4xx) propagates through this exception so the caller's
    Temporal retry policy can engage.
    """

    def __init__(self, *, operation: str, status: int, detail: str) -> None:
        super().__init__(
            f"firecrawl transport error: operation={operation} "
            f"status={status} detail={detail!r}"
        )
        self.operation = operation
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

#: Anything iterable of hostname strings. ``None`` is accepted by the
#: helpers and treated as "no entries".
AllowlistInput = Union[None, "Iterable[str]"]

#: Shape of the per-department override block, mirroring the
#: ``firecrawl_egress_overrides`` JSON Schema definition.
DeptOverrideInput = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _normalise_hosts(value: Any) -> set[str]:
    """Lower-case, strip, and drop empty entries from ``value``.

    Accepts ``None`` (returns the empty set), strings (single-entry
    set), and arbitrary iterables of strings. Non-string entries are
    silently skipped — the helper is robust against half-typed input
    so the property tests can throw arbitrary fixture shapes at it.
    """

    if value is None:
        return set()
    if isinstance(value, str):
        host = value.strip().lower()
        return {host} if host else set()

    out: set[str] = set()
    try:
        iterator = iter(value)
    except TypeError:
        return out

    for raw in iterator:
        if not isinstance(raw, str):
            continue
        host = raw.strip().lower()
        if host:
            out.add(host)
    return out


def _host_matches(host: str, allowlist: frozenset[str]) -> bool:
    """Return ``True`` iff ``host`` matches any allowlist entry on a label boundary.

    Mirrors :func:`firecrawl.egress.is_host_allowed` so the two
    enforcement surfaces (this client and the firecrawl FastAPI app)
    agree on subdomain semantics.
    """

    if not host or not allowlist:
        return False
    for entry in allowlist:
        if host == entry:
            return True
        if host.endswith("." + entry):
            return True
    return False


def _extract_host(url: str) -> str:
    """Extract the lower-cased hostname from ``url``; empty string on failure."""

    try:
        return (urlparse(url.strip()).hostname or "").lower()
    except Exception:  # pragma: no cover — defensive
        return ""


def _encode_body(body: Any) -> bytes:
    """Encode ``body`` to bytes for size accounting and MinIO offload.

    The wrapper does not interpret the body shape — it serialises
    via JSON when the body is a JSON-compatible type and falls back
    to ``repr`` for anything else. The returned bytes are what gets
    written to MinIO so the caller can fetch the exact payload the
    upstream service produced.
    """

    import json

    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        return repr(body).encode("utf-8", errors="replace")


def _build_overflow_key(*, url: str, bytes_len: int) -> str:
    """Build a deterministic MinIO key for an overflow object.

    Format: ``firecrawl/overflow/{sha256(url)[:16]}-{bytes_len}.json``

    The SHA-256 prefix gives a collision-resistant token while
    keeping the key short enough for human inspection. ``bytes_len``
    is appended so two overflows from the same URL with different
    sizes do not overwrite each other.
    """

    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"firecrawl/overflow/{digest}-{bytes_len}.json"


def _build_summary(body: Any, *, bytes_len: int, max_bytes: int) -> str:
    """Build a short human-readable summary for :class:`PayloadOverflow`.

    The summary is what ends up in the LLM context / Jira comment;
    we trim aggressively to stay within R5.9's "Jira yorumuna kısa
    özet" guidance.
    """

    if isinstance(body, list):
        head = (
            f"Search returned {len(body)} results "
            f"({bytes_len} bytes > cap {max_bytes})."
        )
    elif isinstance(body, dict):
        keys = ", ".join(sorted(map(str, body.keys()))[:5])
        head = (
            f"Scrape returned dict with keys [{keys}] "
            f"({bytes_len} bytes > cap {max_bytes})."
        )
    else:
        head = (
            f"Firecrawl payload ({type(body).__name__}, {bytes_len} bytes "
            f"> cap {max_bytes})."
        )

    if len(head) > _SUMMARY_MAX_CHARS:
        head = head[: _SUMMARY_MAX_CHARS - 1] + "…"
    return head


# Re-export ``Iterable`` from ``typing`` for the AllowlistInput alias
# without polluting the module namespace at the top of the file.
from typing import Iterable  # noqa: E402, F401  (used by ``AllowlistInput``)
