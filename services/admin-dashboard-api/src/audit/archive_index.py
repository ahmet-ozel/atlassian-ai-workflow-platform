"""MinIO-backed archive index for audit logs.

platform-mimari-ops task 13.4 — surface archived audit-log objects to
the admin-dashboard ``/admin/audit/search`` endpoint when a query's
time range extends beyond Loki's hot retention window.

Validates: Requirements 6.3, 6.5, 6.9 (design §"LokiSearchProxy").

Layout
------

The ``audit-archive`` bucket is partitioned by UTC date::

    audit-archive/{YYYY}/{MM}/{DD}/audit-{N}.jsonl.gz

where ``{MM}`` and ``{DD}`` are zero-padded. The full S3 URI returned
to callers in :class:`ArchivedAuditHit.archive_uri` therefore looks
like ``s3://audit-archive/2024/03/05/audit-0.jsonl.gz``.

Design contract
---------------

This index is *read-only*: writes are performed by
``automation-worker.archive_audit_to_minio`` (task 13.2). The index's
:meth:`MinIOArchiveIndex.search` method:

1. Computes the set of date-prefixes covered by the query's
   :class:`TimeRange` (inclusive start, exclusive end).
2. Issues an S3 ``ListObjectsV2`` request per covered prefix, pages
   through every continuation token, and yields one
   :class:`ArchivedAuditHit` per object key.
3. Returns the hits as a tuple in **deterministic** ascending order
   (by ``archive_uri``) so the caller's audit panel renders a stable
   list and Hypothesis-driven property tests can assert exact
   equality.

The index does NOT open or parse the gzipped JSON-lines payloads;
content-level filtering (matching ``actor_id`` / ``dept_id`` /
``action`` against the rows inside an archive object) is the
restore-API's job (``POST /admin/audit/archive/restore``, task 11.x).
The index's job is to surface the relevant **archive objects**, with
a one-line ``summary`` derived from the key, and let the operator
either drill into the restore endpoint or hand the URI to a
download-link.

Implementation notes
--------------------

* The MinIO endpoint is reached via the S3-compatible HTTP API and
  AWS Signature V4. The signing helpers mirror the
  ``execution-runner-worker.activities.minio`` module so the two
  surfaces stay in lockstep; we keep them duplicated rather than
  pulled into a shared library because (a) the worker activity uses
  ``activity.logger`` and Temporal-specific retry semantics that the
  admin-dashboard-api does not need, and (b) extracting a shared lib
  is the *only* way the two would diverge — keeping them inline
  makes the duplication obvious to reviewers.
* All MinIO calls go through a caller-supplied :class:`httpx.AsyncClient`
  so unit tests can install an :class:`httpx.MockTransport` without
  monkey-patching globals.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from .types import ArchivedAuditHit, AuditQuery, TimeRange


__all__ = [
    "MinIOArchiveIndex",
    "MinIOArchiveError",
    "ArchiveIndexConfig",
    "DEFAULT_BUCKET",
]


#: Default bucket name (matches ``infra/minio/init.sh`` and
#: ``automation-worker.archive_audit_to_minio`` per design §"MinIO
#: arşiv yapısı"). Overridable via :class:`ArchiveIndexConfig`.
DEFAULT_BUCKET: str = "audit-archive"

#: AWS region used when signing S3 requests against MinIO. MinIO
#: ignores the region but the SigV4 algorithm requires it; the
#: industry-standard placeholder is ``us-east-1``.
_AWS_REGION: str = "us-east-1"
_AWS_SERVICE: str = "s3"

#: ``ListObjectsV2`` returns at most 1000 keys per page; we honour
#: that ceiling so a single archive day with many shards still
#: paginates correctly.
_LIST_PAGE_SIZE: int = 1000

#: S3 ``ListBucketResult`` XML namespace.
_S3_XMLNS: str = "http://s3.amazonaws.com/doc/2006-03-01/"


class MinIOArchiveError(RuntimeError):
    """Raised when the archive index cannot be queried.

    The error message is intentionally crisp — it surfaces in the
    admin UI when the archive-side search fails, so we want the
    operator to see *what* failed (HTTP status, prefix) without
    exposing credentials.
    """

    def __init__(self, *, prefix: str, status: int, body: str) -> None:
        self.prefix = prefix
        self.status = status
        # Truncate the body so a 500-page error response does not
        # blow up the UI; 400 chars is plenty for diagnostics.
        self.body = body[:400]
        super().__init__(
            f"MinIO ListObjectsV2 failed: prefix={prefix!r}, "
            f"status={status}, body={self.body!r}"
        )


@dataclass(frozen=True, slots=True)
class ArchiveIndexConfig:
    """Connection + bucket configuration for the archive index.

    All fields default to dev-mode values aligned with
    ``platform/infra/docker-compose.yml`` so a local run requires no
    explicit configuration. Production callers MUST override
    ``access_key`` / ``secret_key`` and SHOULD set ``use_ssl=True``.
    """

    endpoint: str = "minio:9000"
    access_key: str = ""
    secret_key: str = ""
    bucket: str = DEFAULT_BUCKET
    use_ssl: bool = False

    @property
    def scheme(self) -> str:
        return "https" if self.use_ssl else "http"

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.endpoint}"


# ---------------------------------------------------------------------------
# AWS Signature V4 helpers
# ---------------------------------------------------------------------------


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signing_key(secret: str, date_stamp: str) -> bytes:
    """Derive the AWS SigV4 signing key (chained HMACs)."""

    k_date = _sign(f"AWS4{secret}".encode("utf-8"), date_stamp)
    k_region = _sign(k_date, _AWS_REGION)
    k_service = _sign(k_region, _AWS_SERVICE)
    return _sign(k_service, "aws4_request")


def _build_authorization(
    *,
    method: str,
    canonical_uri: str,
    canonical_query: str,
    host: str,
    amz_date: str,
    payload_hash: str,
    access_key: str,
    secret_key: str,
) -> str:
    """Build a SigV4 ``Authorization`` header for an S3 GET/LIST request.

    The canonical headers list is fixed at ``host``,
    ``x-amz-content-sha256``, ``x-amz-date`` — the minimum required
    for ListObjectsV2 with no payload.
    """

    date_stamp = amz_date[:8]  # YYYYMMDD prefix of YYYYMMDDTHHMMSSZ
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = (
        f"{method}\n"
        f"{canonical_uri}\n"
        f"{canonical_query}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )

    credential_scope = f"{date_stamp}/{_AWS_REGION}/{_AWS_SERVICE}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    signing_key = _get_signing_key(secret_key, date_stamp)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return (
        f"AWS4-HMAC-SHA256 "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )


# ---------------------------------------------------------------------------
# Date-prefix derivation
# ---------------------------------------------------------------------------


def _date_prefixes(time_range: TimeRange) -> tuple[str, ...]:
    """Return the bucket prefixes covered by ``time_range``.

    Layout is daily (``{Y}/{M:02}/{D:02}/``) so every UTC day in
    ``[start.date, end.date)`` (half-open, mirroring
    :class:`TimeRange`) yields one prefix. The ``end`` boundary's
    own date is included **only** when ``end`` is past midnight on
    that day (i.e. ``end.time() != 00:00:00`` *or* ``end > start_of_end_day``);
    for an exclusive boundary at exactly ``YYYY-MM-DDT00:00:00Z`` the
    last day's archives are *not* relevant and the prefix is dropped.

    The returned tuple is sorted ascending so list operations are
    deterministic.
    """

    start_utc = time_range.start.astimezone(timezone.utc)
    end_utc = time_range.end.astimezone(timezone.utc)

    # Floor ``start`` to the start of its UTC day; that becomes the
    # first prefix.
    cursor = start_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    # Compute the (exclusive) upper-day cutoff. If ``end`` is exactly
    # midnight, the day it lands on is NOT covered (half-open); else
    # it IS covered.
    end_day_floor = end_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if end_utc == end_day_floor:
        upper_exclusive = end_day_floor
    else:
        upper_exclusive = end_day_floor + timedelta(days=1)

    prefixes: list[str] = []
    one_day = timedelta(days=1)
    while cursor < upper_exclusive:
        prefixes.append(f"{cursor.year:04d}/{cursor.month:02d}/{cursor.day:02d}/")
        cursor += one_day

    return tuple(prefixes)


def _summary_from_key(key: str) -> str:
    """Derive a human-readable one-liner from an archive object key.

    Operators see this in the archive search results table; the goal
    is recognisability ("audit shard 3 from 2024-03-05") rather than
    full content. Falls back to the raw key when the layout doesn't
    match (e.g. operator dropped a manual file in the bucket).
    """

    parts = key.split("/")
    if len(parts) >= 4:
        year, month, day, leaf = parts[0], parts[1], parts[2], parts[-1]
        return f"audit archive {year}-{month}-{day} ({leaf})"
    return f"audit archive {key}"


# ---------------------------------------------------------------------------
# MinIOArchiveIndex
# ---------------------------------------------------------------------------


class MinIOArchiveIndex:
    """Read-only index over the ``audit-archive`` MinIO bucket.

    Parameters
    ----------
    config:
        Connection + bucket configuration. Defaults to the dev-mode
        Compose values; production callers MUST override credentials.
    http_client:
        A live :class:`httpx.AsyncClient`. The index does NOT take
        ownership — callers manage the client's lifecycle (open it
        in the FastAPI ``lifespan`` and close it on shutdown). This
        also makes unit tests trivial: pass a client wired to an
        :class:`httpx.MockTransport`.

    The class is stateless beyond its config + client; method calls
    are safe to issue concurrently from multiple coroutines.
    """

    def __init__(
        self,
        *,
        config: ArchiveIndexConfig,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._http = http_client

    @property
    def bucket(self) -> str:
        return self._config.bucket

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: AuditQuery) -> tuple[ArchivedAuditHit, ...]:
        """List archive objects covering ``query.time_range``.

        Filters at the **prefix** level only — no object-content
        inspection. The ``actor_id``, ``dept_id`` and ``action``
        fields on :class:`AuditQuery` are *not* applied here; that
        responsibility belongs to the restore endpoint.

        Returns
        -------
        tuple[ArchivedAuditHit, ...]
            One hit per archive object found, sorted ascending by
            ``archive_uri`` for deterministic ordering. An empty
            tuple is returned when no archives cover the range.
        """

        prefixes = _date_prefixes(query.time_range)
        if not prefixes:
            return ()

        hits: list[ArchivedAuditHit] = []
        seen_keys: set[str] = set()
        for prefix in prefixes:
            full_prefix = self._object_prefix(prefix)
            async for key in self._list_objects(full_prefix):
                # Defensive: ListObjectsV2 may return duplicates in
                # extreme race conditions (e.g. paginating during a
                # multipart-upload commit). Dedupe by full key.
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                hits.append(
                    ArchivedAuditHit(
                        id=self._hit_id(key),
                        archived=True,
                        archive_uri=f"s3://{self._config.bucket}/{key}",
                        summary=_summary_from_key(key),
                    )
                )

        # Deterministic ordering — operators (and Hypothesis tests)
        # rely on this. ``archive_uri`` sort matches lexicographic
        # date ordering because the keys are zero-padded.
        hits.sort(key=lambda h: h.archive_uri)
        return tuple(hits)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _object_prefix(self, date_prefix: str) -> str:
        """Compose the bucket-relative prefix for ``ListObjectsV2``.

        ``date_prefix`` is an opaque date partition like
        ``"2024/03/05/"``; we don't add any sub-path on top of it
        because the bucket itself IS the ``audit-archive`` namespace.
        """

        return date_prefix

    def _hit_id(self, key: str) -> str:
        """Stable id derived from the archive key.

        Two archives with the same key (impossible under the bucket's
        write contract) would collide here, but operators looking up
        a hit by id only need uniqueness within a single search
        result set — the key itself satisfies that.
        """

        return key

    async def _list_objects(
        self, prefix: str
    ) -> AsyncIterator[str]:
        """Async-iterate over every key under ``prefix``.

        Pages through the ``ListObjectsV2`` continuation tokens until
        the response reports ``IsTruncated=false``.
        """

        continuation: str | None = None
        while True:
            keys, next_token = await self._list_objects_page(prefix, continuation)
            for key in keys:
                yield key
            if next_token is None:
                return
            continuation = next_token

    async def _list_objects_page(
        self,
        prefix: str,
        continuation: str | None,
    ) -> tuple[tuple[str, ...], str | None]:
        """Issue a single signed ``ListObjectsV2`` request.

        Returns the page's keys (in MinIO-returned order) and the
        next continuation token, or ``None`` when the response is
        not truncated.
        """

        # Build the canonical query string. SigV4 requires the params
        # to be sorted by name and percent-encoded with safe='~'.
        params: list[tuple[str, str]] = [
            ("list-type", "2"),
            ("max-keys", str(_LIST_PAGE_SIZE)),
            ("prefix", prefix),
        ]
        if continuation is not None:
            params.append(("continuation-token", continuation))
        params.sort(key=lambda kv: kv[0])
        canonical_query = "&".join(
            f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in params
        )

        host = self._config.endpoint
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Empty body for ListObjectsV2.
        payload_hash = hashlib.sha256(b"").hexdigest()
        canonical_uri = f"/{self._config.bucket}"

        if not self._config.access_key or not self._config.secret_key:
            raise MinIOArchiveError(
                prefix=prefix,
                status=0,
                body="MinIO credentials not configured",
            )

        authorization = _build_authorization(
            method="GET",
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            host=host,
            amz_date=amz_date,
            payload_hash=payload_hash,
            access_key=self._config.access_key,
            secret_key=self._config.secret_key,
        )

        url = f"{self._config.base_url}{canonical_uri}?{canonical_query}"
        headers = {
            "Authorization": authorization,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            # MinIO is forgiving about Host but we send it explicitly
            # so the canonical_request and the wire request agree.
            "Host": host,
        }

        response = await self._http.get(url, headers=headers)
        if response.status_code != 200:
            raise MinIOArchiveError(
                prefix=prefix,
                status=response.status_code,
                body=response.text,
            )

        return _parse_list_response(response.text)


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _parse_list_response(body: str) -> tuple[tuple[str, ...], str | None]:
    """Parse an S3 ``ListObjectsV2`` XML response.

    Returns the keys in document order plus the continuation token
    (or ``None`` when ``IsTruncated`` is absent / false).

    Raises
    ------
    MinIOArchiveError
        When the body is not valid XML or lacks the expected
        ``ListBucketResult`` root.
    """

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise MinIOArchiveError(
            prefix="<list>",
            status=200,
            body=f"unparseable XML: {exc}",
        ) from exc

    # Strip the namespace from the tag for forgiving comparison.
    if not _local_name(root.tag).endswith("ListBucketResult"):
        raise MinIOArchiveError(
            prefix="<list>",
            status=200,
            body=f"unexpected root element: {root.tag}",
        )

    keys: list[str] = []
    is_truncated = False
    next_token: str | None = None

    for child in root:
        local = _local_name(child.tag)
        if local == "Contents":
            for grand in child:
                if _local_name(grand.tag) == "Key" and grand.text:
                    keys.append(grand.text)
        elif local == "IsTruncated":
            is_truncated = (child.text or "").strip().lower() == "true"
        elif local == "NextContinuationToken":
            next_token = (child.text or None)

    if not is_truncated:
        next_token = None
    return tuple(keys), next_token


def _local_name(tag: str) -> str:
    """Return the local part of an XML tag (strip ``{ns}`` prefix)."""

    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
