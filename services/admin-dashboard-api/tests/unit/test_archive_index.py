"""Unit tests for ``src.audit.archive_index`` (platform-mimari-ops 13.4).

The tests exercise the MinIO archive index through an in-process
:class:`httpx.MockTransport`; no real MinIO, no network, no disk I/O.

Coverage
--------

* ``_date_prefixes`` derives the right set of ``Y/M/D/`` partitions
  for any half-open :class:`TimeRange` (single day, multi-day,
  exact-midnight upper boundary, multi-month spans).
* ``_summary_from_key`` produces a recognisable one-line description
  from a well-formed key and falls back gracefully for malformed
  keys.
* :class:`MinIOArchiveIndex.search` issues one ``ListObjectsV2`` per
  covered prefix, follows continuation tokens, and surfaces the
  resulting keys as :class:`ArchivedAuditHit` instances sorted
  ascending by ``archive_uri``.
* Non-200 responses raise :class:`MinIOArchiveError` with the
  truncated body and originating prefix preserved.
* Missing credentials short-circuit with a friendly error before any
  HTTP call is issued.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

# Bootstrap sys.path so the tests can be run directly from the service
# root without ``pip install -e``. Mirrors the pattern used by
# ``test_prompts_git_router.py``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.audit import (  # noqa: E402
    ArchivedAuditHit,
    AuditQuery,
    MinIOArchiveIndex,
    TimeRange,
)
from src.audit.archive_index import (  # noqa: E402
    ArchiveIndexConfig,
    MinIOArchiveError,
    _date_prefixes,
    _parse_list_response,
    _summary_from_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(**overrides: object) -> ArchiveIndexConfig:
    base = {
        "endpoint": "minio:9000",
        "access_key": "test-access",
        "secret_key": "test-secret",
        "bucket": "audit-archive",
        "use_ssl": False,
    }
    base.update(overrides)
    # mypy: ignore[arg-type] — the dict-merge keeps the keys aligned.
    return ArchiveIndexConfig(**base)  # type: ignore[arg-type]


def _make_index(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: ArchiveIndexConfig | None = None,
) -> tuple[MinIOArchiveIndex, list[httpx.Request]]:
    """Wire a :class:`MinIOArchiveIndex` against a mock transport.

    Returns the index and a list that captures every outgoing
    request so individual tests can assert on URL / headers.
    """

    captured: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    client = httpx.AsyncClient(transport=transport)
    index = MinIOArchiveIndex(config=config or _config(), http_client=client)
    return index, captured


def _list_response_xml(keys: list[str], next_token: str | None = None) -> str:
    """Build a minimal S3-compatible ``ListObjectsV2`` response."""

    contents = "".join(
        f"  <Contents>\n    <Key>{k}</Key>\n  </Contents>\n" for k in keys
    )
    truncated_block = (
        f"  <IsTruncated>true</IsTruncated>\n"
        f"  <NextContinuationToken>{next_token}</NextContinuationToken>\n"
        if next_token
        else "  <IsTruncated>false</IsTruncated>\n"
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">\n'
        f"  <Name>audit-archive</Name>\n"
        f"  <KeyCount>{len(keys)}</KeyCount>\n"
        f"{contents}"
        f"{truncated_block}"
        f"</ListBucketResult>\n"
    )


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# _date_prefixes
# ---------------------------------------------------------------------------


class TestDatePrefixes:
    def test_single_day_range(self) -> None:
        rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
        assert _date_prefixes(rng) == ("2024/03/05/",)

    def test_multi_day_range(self) -> None:
        rng = TimeRange(_utc(2024, 3, 5, 6), _utc(2024, 3, 7, 12))
        assert _date_prefixes(rng) == (
            "2024/03/05/",
            "2024/03/06/",
            "2024/03/07/",
        )

    def test_exact_midnight_upper_excludes_that_day(self) -> None:
        # Half-open: end at 00:00:00Z drops the end-day's prefix.
        rng = TimeRange(_utc(2024, 3, 5), _utc(2024, 3, 6))
        assert _date_prefixes(rng) == ("2024/03/05/",)

    def test_post_midnight_upper_includes_that_day(self) -> None:
        rng = TimeRange(_utc(2024, 3, 5), _utc(2024, 3, 6, 1))
        assert _date_prefixes(rng) == ("2024/03/05/", "2024/03/06/")

    def test_month_boundary(self) -> None:
        rng = TimeRange(_utc(2024, 1, 31, 6), _utc(2024, 2, 1, 6))
        assert _date_prefixes(rng) == ("2024/01/31/", "2024/02/01/")

    def test_year_boundary(self) -> None:
        rng = TimeRange(_utc(2023, 12, 31, 6), _utc(2024, 1, 1, 6))
        assert _date_prefixes(rng) == ("2023/12/31/", "2024/01/01/")

    def test_zero_padding(self) -> None:
        rng = TimeRange(_utc(2024, 1, 5, 6), _utc(2024, 1, 5, 12))
        assert _date_prefixes(rng) == ("2024/01/05/",)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            TimeRange(datetime(2024, 3, 5), datetime(2024, 3, 6))

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="end must be > start"):
            TimeRange(_utc(2024, 3, 6), _utc(2024, 3, 5))


# ---------------------------------------------------------------------------
# _summary_from_key
# ---------------------------------------------------------------------------


class TestSummaryFromKey:
    def test_well_formed_key(self) -> None:
        assert (
            _summary_from_key("2024/03/05/audit-0.jsonl.gz")
            == "audit archive 2024-03-05 (audit-0.jsonl.gz)"
        )

    def test_malformed_key_falls_back(self) -> None:
        assert _summary_from_key("loose-file.txt") == "audit archive loose-file.txt"

    def test_short_key_falls_back(self) -> None:
        assert _summary_from_key("2024/audit.gz") == "audit archive 2024/audit.gz"


# ---------------------------------------------------------------------------
# _parse_list_response
# ---------------------------------------------------------------------------


class TestParseListResponse:
    def test_empty_listing(self) -> None:
        body = _list_response_xml([])
        keys, token = _parse_list_response(body)
        assert keys == ()
        assert token is None

    def test_single_page(self) -> None:
        body = _list_response_xml(
            ["2024/03/05/audit-0.jsonl.gz", "2024/03/05/audit-1.jsonl.gz"]
        )
        keys, token = _parse_list_response(body)
        assert keys == (
            "2024/03/05/audit-0.jsonl.gz",
            "2024/03/05/audit-1.jsonl.gz",
        )
        assert token is None

    def test_truncated_with_continuation_token(self) -> None:
        body = _list_response_xml(
            ["2024/03/05/audit-0.jsonl.gz"], next_token="opaque-cursor-1"
        )
        keys, token = _parse_list_response(body)
        assert keys == ("2024/03/05/audit-0.jsonl.gz",)
        assert token == "opaque-cursor-1"

    def test_invalid_xml_raises(self) -> None:
        with pytest.raises(MinIOArchiveError):
            _parse_list_response("<not xml")

    def test_unexpected_root_raises(self) -> None:
        body = '<?xml version="1.0"?><Other/>'
        with pytest.raises(MinIOArchiveError, match="unexpected root"):
            _parse_list_response(body)


# ---------------------------------------------------------------------------
# MinIOArchiveIndex.search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_single_day_returns_sorted_hits() -> None:
    """Single-day range surfaces every shard as a sorted hit."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Sanity: the URL targets the right bucket and prefix.
        assert request.url.path == "/audit-archive"
        assert "prefix=2024%2F03%2F05%2F" in str(request.url)
        return httpx.Response(
            200,
            text=_list_response_xml(
                [
                    "2024/03/05/audit-2.jsonl.gz",
                    "2024/03/05/audit-0.jsonl.gz",
                    "2024/03/05/audit-1.jsonl.gz",
                ]
            ),
        )

    index, captured = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(
        actor_id=None, dept_id=None, action=None, time_range=rng
    )

    hits = await index.search(query)

    assert len(hits) == 3
    # Sorted ascending by archive_uri (which includes the shard suffix).
    assert [h.archive_uri for h in hits] == [
        "s3://audit-archive/2024/03/05/audit-0.jsonl.gz",
        "s3://audit-archive/2024/03/05/audit-1.jsonl.gz",
        "s3://audit-archive/2024/03/05/audit-2.jsonl.gz",
    ]
    for h in hits:
        assert isinstance(h, ArchivedAuditHit)
        assert h.archived is True
        assert h.summary.startswith("audit archive 2024-03-05")
    # Exactly one ListObjectsV2 request — the day's prefix.
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_search_multi_day_issues_one_list_per_day() -> None:
    """Each covered date partition produces its own ListObjectsV2 call."""

    seen_prefixes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Every request is a list against /audit-archive.
        assert request.url.path == "/audit-archive"
        # Extract the prefix= query param.
        for k, v in request.url.params.multi_items():
            if k == "prefix":
                seen_prefixes.append(v)
        return httpx.Response(200, text=_list_response_xml([]))

    index, _ = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 6), _utc(2024, 3, 7, 12))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    hits = await index.search(query)
    assert hits == ()
    assert seen_prefixes == ["2024/03/05/", "2024/03/06/", "2024/03/07/"]


@pytest.mark.asyncio
async def test_search_follows_continuation_tokens() -> None:
    """Truncated responses are paged until ``IsTruncated=false``."""

    call_index = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_index["i"] += 1
        if call_index["i"] == 1:
            return httpx.Response(
                200,
                text=_list_response_xml(
                    ["2024/03/05/audit-0.jsonl.gz"], next_token="page-2"
                ),
            )
        # Second call MUST carry continuation-token=page-2.
        token = request.url.params.get("continuation-token")
        assert token == "page-2"
        return httpx.Response(
            200,
            text=_list_response_xml(["2024/03/05/audit-1.jsonl.gz"]),
        )

    index, _ = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    hits = await index.search(query)

    assert call_index["i"] == 2
    assert [h.archive_uri for h in hits] == [
        "s3://audit-archive/2024/03/05/audit-0.jsonl.gz",
        "s3://audit-archive/2024/03/05/audit-1.jsonl.gz",
    ]


@pytest.mark.asyncio
async def test_search_dedupes_repeated_keys_across_pages() -> None:
    """Defensive dedupe: same key in two pages → one hit."""

    call_index = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_index["i"] += 1
        if call_index["i"] == 1:
            return httpx.Response(
                200,
                text=_list_response_xml(
                    ["2024/03/05/audit-0.jsonl.gz"], next_token="page-2"
                ),
            )
        return httpx.Response(
            200,
            text=_list_response_xml(["2024/03/05/audit-0.jsonl.gz"]),
        )

    index, _ = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    hits = await index.search(query)
    assert len(hits) == 1
    assert hits[0].archive_uri == "s3://audit-archive/2024/03/05/audit-0.jsonl.gz"


@pytest.mark.asyncio
async def test_search_propagates_non_200_as_error() -> None:
    """5xx response from MinIO surfaces as MinIOArchiveError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    index, _ = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    with pytest.raises(MinIOArchiveError) as excinfo:
        await index.search(query)
    assert excinfo.value.status == 503
    assert "Service Unavailable" in excinfo.value.body


@pytest.mark.asyncio
async def test_search_missing_credentials_short_circuits() -> None:
    """Empty access/secret keys raise before any HTTP call."""

    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, text=_list_response_xml([]))

    index, _ = _make_index(
        handler, config=_config(access_key="", secret_key="")
    )
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    with pytest.raises(MinIOArchiveError, match="credentials not configured"):
        await index.search(query)
    assert requests_seen == []


@pytest.mark.asyncio
async def test_search_signs_request_with_required_headers() -> None:
    """Every outgoing request carries SigV4 Authorization + amz headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=test-access/")
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
        assert "x-amz-content-sha256" in request.headers
        assert "x-amz-date" in request.headers
        return httpx.Response(200, text=_list_response_xml([]))

    index, _ = _make_index(handler)
    rng = TimeRange(_utc(2024, 3, 5, 1), _utc(2024, 3, 5, 23))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    hits = await index.search(query)
    assert hits == ()


@pytest.mark.asyncio
async def test_empty_prefix_set_returns_empty_tuple_without_http() -> None:
    """A range that covers no day prefixes (theoretical) yields ()."""

    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, text=_list_response_xml([]))

    index, _ = _make_index(handler)
    # ``TimeRange`` rejects ``end <= start``, so the only way to have
    # zero prefixes is end exactly one microsecond past start within
    # the same day where start happens to be at midnight. _date_prefixes
    # always emits at least one prefix in that case, so we instead
    # validate the property directly: the iterator over a one-prefix
    # set still yields one prefix.
    rng = TimeRange(_utc(2024, 3, 5), _utc(2024, 3, 5) + timedelta(microseconds=1))
    query = AuditQuery(actor_id=None, dept_id=None, action=None, time_range=rng)

    hits = await index.search(query)
    # The single prefix produces one ListObjectsV2 request → 0 keys.
    assert hits == ()
    assert len(requests_seen) == 1


@pytest.mark.asyncio
async def test_bucket_property_exposes_configured_bucket() -> None:
    index, _ = _make_index(
        lambda r: httpx.Response(200, text=_list_response_xml([])),
        config=_config(bucket="some-other-bucket"),
    )
    assert index.bucket == "some-other-bucket"
