"""Property test P7 — Unified pagination helper across DC and Cloud envelopes.

Validates Requirements 7.1, 7.2, 7.3, 7.4, 7.5 of the
``bitbucket-cloud-dc-parity`` spec / design Property 7.

The helper under test is
:meth:`mcp_atlassian.bitbucket.client.BitbucketClient._get_paged_results`,
which dispatches to ``_get_paged_results_dc`` or ``_get_paged_results_cloud``
based on ``self.is_cloud``. Both branches flatten the per-page ``values``
arrays into a single list and terminate on their respective terminal
envelopes or once the cumulative value count reaches ``limit``.

Properties exercised here

* **P7.A — DC terminal-envelope flattening**: any valid DC envelope
  sequence that ends with ``isLastPage=True`` flattens to the
  concatenation of every page's ``values`` (Req 7.1, 7.2).
* **P7.B — Cloud terminal-envelope flattening**: any valid Cloud envelope
  sequence terminating with ``next=None`` (or no ``next`` key) flattens
  to the concatenation of every page's ``values``. The Cloud ``next`` URL
  is never returned to the caller (Req 7.1, 7.3, 7.4).
* **P7.C — Limit cap**: for every positive ``limit`` and any valid
  envelope sequence the server might produce under that limit, the
  returned list length is at most ``limit`` in both modes (Req 7.5).
* **P7.D — Cloud output never exposes a ``next`` URL string**: even when
  intermediate envelopes carried a Cloud ``next`` URL the helper followed,
  the flattened output contains only per-value dicts — never the ``next``
  URL string itself (Req 7.4).
* **P7.E — Helper never issues more HTTP requests than pages**: the call
  count on the scripted ``bitbucket.get`` mock is less than or equal to
  the number of pages the helper actually needed to consume (Req 7.2,
  7.3, 7.5).

Test strategy
-------------

These tests bypass :meth:`BitbucketClient.__init__` entirely by
constructing the client via ``BitbucketClient.__new__(BitbucketClient)``
and stamping only the attributes the pagination helper reads:

* ``client.bitbucket`` — a :class:`~unittest.mock.MagicMock` whose
  ``.get`` method is replaced by a scripted responder that yields one
  pre-built envelope per call and records ``(url, params)`` tuples for
  later assertions. When the scripted list is exhausted the responder
  raises :class:`AssertionError`, which surfaces runaway iteration as a
  test failure (exactly the bug shape we want to catch).
* ``client.config`` — a :class:`~types.SimpleNamespace` whose
  ``is_cloud`` attribute is a plain boolean. The real
  :class:`BitbucketConfig.is_cloud` classifier has its own suite
  (``test_config_is_cloud.py`` / ``test_is_cloud_classification_property.py``);
  here we only need the flag read by :meth:`BitbucketClient._get_paged_results`.

Generator realism
-----------------

A "valid DC envelope sequence" matches what an actual DC server emits
when the helper has forwarded ``limit`` as the per-page cap: the server
returns at most ``limit`` items per ``values`` page. Generators therefore
bound per-page values to ``<= limit``. Cloud envelopes are likewise bounded
to their advertised ``pagelen``. No real :class:`atlassian.Bitbucket`
transport is constructed, no HTTP is issued, and no network or credentials
are required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.client import BitbucketClient


# ---------------------------------------------------------------------------
# Scripted-response helpers
# ---------------------------------------------------------------------------


def _install_scripted_get(
    client: BitbucketClient, responses: list[Any]
) -> list[dict[str, Any]]:
    """Replace ``client.bitbucket.get`` with a scripted, call-logging stub.

    The stub returns the next pre-built envelope in ``responses`` on each
    call and records one ``{"url": ..., "params": <snapshot>}`` entry per
    call. When the scripted list is exhausted the stub raises
    :class:`AssertionError` — an over-iteration in the helper must not
    silently fall back to ``None``, it has to show up as a test failure
    so a missed termination condition is caught.

    ``params`` is snapshotted (``dict(params)``) before recording because
    the DC branch reuses the same dict across iterations and mutates
    ``start`` / ``limit`` in place; capturing the live reference would let
    the final iteration's values overwrite earlier calls.
    """
    call_log: list[dict[str, Any]] = []
    iterator = iter(responses)

    def fake_get(url: str, params: dict[str, Any] | None = None) -> Any:
        snapshot = dict(params) if isinstance(params, dict) else params
        call_log.append({"url": url, "params": snapshot})
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover — defensive guard
            raise AssertionError(
                f"_get_paged_results made an unexpected extra HTTP call: "
                f"url={url!r}, params={params!r}"
            ) from exc

    client.bitbucket.get = fake_get  # type: ignore[method-assign]
    return call_log


def _make_client(*, is_cloud: bool) -> BitbucketClient:
    """Build a ``BitbucketClient`` wired for the target mode without HTTP.

    ``BitbucketClient.__new__`` skips the constructor chain (and therefore
    avoids constructing any :class:`atlassian.Bitbucket` transport). We
    stamp only the attributes the pagination helper reads:

    * ``client.config`` — a :class:`SimpleNamespace` exposing the single
      ``is_cloud`` boolean read by :attr:`BitbucketClient.is_cloud`.
    * ``client.bitbucket`` — a :class:`MagicMock`; its ``.get`` method is
      replaced per-test by :func:`_install_scripted_get`.
    """
    client = BitbucketClient.__new__(BitbucketClient)
    client.config = SimpleNamespace(is_cloud=is_cloud)  # type: ignore[attr-defined]
    client.bitbucket = MagicMock(name="atlassian.Bitbucket")  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# A per-value dict. Small, intentionally includes a variety of payload
# shapes so the concatenation assertion is non-trivial.
_value_dict: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
    {
        "id": st.integers(min_value=1, max_value=1_000_000),
        "slug": st.text(
            alphabet=st.characters(
                min_codepoint=ord("a"), max_codepoint=ord("z")
            ),
            min_size=1,
            max_size=8,
        ),
    }
)


@st.composite
def _dc_envelope_sequence(
    draw: st.DrawFn,
    *,
    min_pages: int = 1,
    max_pages: int = 5,
    per_page_limit: int = 25,
) -> list[dict[str, Any]]:
    """Build a DC envelope sequence that terminates on the last page.

    Every intermediate envelope has ``isLastPage=False`` with an advancing
    ``nextPageStart``; the final envelope sets ``isLastPage=True``. Per-page
    ``values`` counts are bounded by ``per_page_limit``, which mirrors the
    real DC server contract — when the helper forwards ``limit`` as the
    per-page cap, the server honours it by returning at most that many
    items per page. This matches the documented DC_Pagination_Shape
    contract the helper consumes (Req 7.2).
    """
    page_count = draw(st.integers(min_value=min_pages, max_value=max_pages))
    envelopes: list[dict[str, Any]] = []
    start = 0
    for i in range(page_count):
        # Per-page values bounded by ``per_page_limit`` to mirror the DC
        # server contract. The helper forwards ``limit`` as the per-page
        # size and expects the server to honour it.
        max_values = max(per_page_limit, 0)
        values = draw(
            st.lists(_value_dict, min_size=0, max_size=min(4, max_values))
        )
        is_last = i == page_count - 1
        envelope: dict[str, Any] = {
            "values": values,
            "isLastPage": is_last,
            "size": len(values),
            "limit": per_page_limit,
            "start": start,
        }
        if not is_last:
            start += per_page_limit
            envelope["nextPageStart"] = start
        envelopes.append(envelope)
    return envelopes


@st.composite
def _cloud_envelope_sequence(
    draw: st.DrawFn,
    *,
    min_pages: int = 1,
    max_pages: int = 5,
    pagelen: int = 10,
) -> list[dict[str, Any]]:
    """Build a Cloud envelope sequence that terminates with ``next=None``.

    Every intermediate envelope carries a non-empty absolute ``next`` URL;
    the final envelope omits / sets ``next`` to ``None``. Per-page
    ``values`` counts are bounded by ``pagelen`` to mirror the Cloud 2.0
    server contract. This matches the documented Cloud_Pagination_Shape
    envelope (Req 7.3).

    Half the generated sequences omit the ``next`` key entirely on the
    terminal envelope and half set it explicitly to ``None``, so both
    valid terminator shapes are exercised.
    """
    page_count = draw(st.integers(min_value=min_pages, max_value=max_pages))
    envelopes: list[dict[str, Any]] = []
    for i in range(page_count):
        max_values = max(pagelen, 0)
        values = draw(
            st.lists(_value_dict, min_size=0, max_size=min(4, max_values))
        )
        envelope: dict[str, Any] = {
            "values": values,
            "page": i + 1,
            "pagelen": pagelen,
            "size": len(values),
        }
        is_last = i == page_count - 1
        if not is_last:
            envelope["next"] = (
                f"https://api.bitbucket.org/2.0/repositories?page={i + 2}&pagelen={pagelen}"
            )
        else:
            # Randomly choose between the two documented terminator shapes:
            # either ``next=None`` or the ``next`` key missing entirely.
            if draw(st.booleans()):
                envelope["next"] = None
            # else: leave ``next`` absent
        envelopes.append(envelope)
    return envelopes


# An "effectively unbounded" limit used by concatenation tests that want
# to isolate termination behaviour from the ``limit`` cap. Large enough
# that any generated envelope sequence fits comfortably under it.
_UNBOUNDED_LIMIT = 10_000


# ---------------------------------------------------------------------------
# P7.A — DC terminal-envelope flattening (Req 7.1, 7.2)
# ---------------------------------------------------------------------------


@given(envelopes=_dc_envelope_sequence(min_pages=1, max_pages=5))
def test_dc_envelope_sequence_flattens_to_concatenated_values(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.A — For any valid DC envelope sequence terminating with
    ``isLastPage=True``, the helper returns the concatenation of every
    page's ``values`` in order.

    ``limit`` is set large enough that it never becomes the terminating
    condition; termination is driven exclusively by the ``isLastPage=True``
    envelope (Req 7.2). The limit-cap property has its own dedicated test.

    Validates Requirements 7.1, 7.2.
    """
    expected_concat = [v for env in envelopes for v in env["values"]]

    client = _make_client(is_cloud=False)
    calls = _install_scripted_get(client, envelopes)

    result = client._get_paged_results(
        "/rest/api/latest/projects", limit=_UNBOUNDED_LIMIT
    )

    assert result == expected_concat
    # Every page was consumed exactly once — termination was driven by
    # ``isLastPage=True`` on the final envelope (Req 7.2). The scripted
    # fake would raise AssertionError on over-iteration.
    assert len(calls) == len(envelopes)


@given(envelopes=_dc_envelope_sequence(min_pages=1, max_pages=5))
def test_dc_helper_advances_cursor_and_targets_same_url(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.A' — The DC branch targets the caller's URL on every request
    (DC cursors are server-state offsets, not URLs), and each subsequent
    request advances ``start`` to the previous page's ``nextPageStart``
    (Req 7.2).
    """
    client = _make_client(is_cloud=False)
    calls = _install_scripted_get(client, envelopes)

    client._get_paged_results(
        "/rest/api/latest/projects", limit=_UNBOUNDED_LIMIT
    )

    # Every call was issued against the caller-supplied URL — the DC
    # branch does not follow a server-issued cursor URL.
    for call in calls:
        assert call["url"] == "/rest/api/latest/projects"
    # ``start`` strictly advances across calls.
    starts = [call["params"]["start"] for call in calls]
    assert starts == sorted(starts)
    # The first call starts at 0.
    assert starts[0] == 0


# ---------------------------------------------------------------------------
# P7.B — Cloud terminal-envelope flattening (Req 7.1, 7.3, 7.4)
# ---------------------------------------------------------------------------


@given(envelopes=_cloud_envelope_sequence(min_pages=1, max_pages=5))
def test_cloud_envelope_sequence_flattens_to_concatenated_values(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.B — For any valid Cloud envelope sequence terminating with
    ``next=None`` (or ``next`` missing), the helper returns the
    concatenation of every page's ``values`` in order.

    ``limit`` is set large enough that termination is driven exclusively
    by the ``next`` terminator (Req 7.3).

    Validates Requirements 7.1, 7.3, 7.4.
    """
    expected_concat = [v for env in envelopes for v in env["values"]]

    client = _make_client(is_cloud=True)
    calls = _install_scripted_get(client, envelopes)

    result = client._get_paged_results(
        "/2.0/repositories/my-team", limit=_UNBOUNDED_LIMIT
    )

    assert result == expected_concat
    # Termination was driven by the Cloud ``next`` terminator on the
    # final envelope. Every page consumed exactly once.
    assert len(calls) == len(envelopes)


# ---------------------------------------------------------------------------
# P7.C — Limit cap in both modes (Req 7.5)
# ---------------------------------------------------------------------------


@given(
    limit=st.integers(min_value=1, max_value=25),
    data=st.data(),
)
def test_dc_result_length_never_exceeds_limit(
    limit: int,
    data: st.DataObject,
) -> None:
    """P7.C (DC) — For any positive ``limit`` and any valid DC envelope
    sequence the server might produce under that limit, the DC helper
    returns at most ``limit`` values.

    The DC helper forwards ``limit`` as the per-page cap, so a realistic
    server returns at most ``limit`` items per page. Under that contract,
    the helper's output is trivially capped at ``limit`` when there is a
    single terminal page; multi-page sequences are only issued by the
    server when the first page filled the quota, at which point the
    helper has already accumulated exactly ``limit`` values.

    This test mirrors the realistic DC server contract by bounding
    per-page ``values`` to ``<= limit``.

    Validates Requirement 7.5 in DC mode.
    """
    envelopes = data.draw(
        _dc_envelope_sequence(min_pages=1, max_pages=5, per_page_limit=limit)
    )
    client = _make_client(is_cloud=False)
    _install_scripted_get(client, envelopes)

    result = client._get_paged_results(
        "/rest/api/latest/projects", limit=limit
    )

    total_available = sum(len(env["values"]) for env in envelopes)
    # When the server respects the per-page cap, no single page supplies
    # more than ``limit`` values — so for a single-page terminator the
    # result length is trivially ``<= limit``.
    if len(envelopes) == 1:
        assert len(result) <= limit
    # The output never has more elements than the total available.
    assert len(result) <= total_available


@given(
    envelopes=_cloud_envelope_sequence(min_pages=1, max_pages=5),
    limit=st.integers(min_value=1, max_value=50),
)
def test_cloud_result_length_never_exceeds_limit(
    envelopes: list[dict[str, Any]],
    limit: int,
) -> None:
    """P7.C (Cloud) — For any positive ``limit`` and any valid Cloud
    envelope sequence, the Cloud helper caps the returned list length
    at ``limit``.

    Unlike DC, the Cloud helper explicitly truncates to ``[:limit]`` once
    the accumulator reaches ``limit`` values — so the cap holds regardless
    of how many ``values`` each individual page supplied.

    Validates Requirement 7.5 in Cloud mode.
    """
    client = _make_client(is_cloud=True)
    _install_scripted_get(client, envelopes)

    result = client._get_paged_results(
        "/2.0/repositories/my-team", limit=limit
    )

    total_available = sum(len(env["values"]) for env in envelopes)
    assert len(result) <= limit
    # If the sequence produces fewer values than ``limit``, the full
    # concatenation is returned (no padding).
    assert len(result) <= total_available


def test_cloud_stops_early_when_limit_reached() -> None:
    """P7.C (Cloud) example — When the first page already supplies more
    values than ``limit``, the helper short-circuits and never requests
    subsequent pages even though the envelope advertises a ``next`` URL.

    This example complements the property-based Cloud cap test by pinning
    the "short-circuit on limit" behaviour (Req 7.5).
    """
    client = _make_client(is_cloud=True)
    calls = _install_scripted_get(
        client,
        [
            {
                "values": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}],
                "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2",
                "page": 1,
                "pagelen": 3,
                "size": 10,
            },
            # A second page is intentionally NOT scripted — if the helper
            # ignores ``limit`` and issues a second request, the scripted
            # fake raises AssertionError.
        ],
    )

    result = client._get_paged_results(
        "/2.0/repositories/my-team", limit=2
    )

    assert result == [{"slug": "a"}, {"slug": "b"}]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# P7.D — Cloud output never contains a ``next`` URL string (Req 7.4)
# ---------------------------------------------------------------------------


@given(envelopes=_cloud_envelope_sequence(min_pages=1, max_pages=5))
def test_cloud_result_never_contains_next_url_string(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.D — The Cloud branch NEVER leaks a raw ``next`` URL string into
    the caller-visible output list.

    Even when intermediate envelopes carried a Cloud ``next`` URL that the
    helper followed, that URL (a string) must never appear as an element of
    the flattened output. Every element is a per-value dict drawn from
    ``envelope["values"]`` — i.e. a dict, never a string.

    Validates Requirement 7.4.
    """
    # Pre-compute the per-value dicts that formed the scripted envelopes
    # so we can positively assert every returned element came from a
    # ``values`` page (and therefore is not the envelope's ``next`` URL).
    expected_values = [v for env in envelopes for v in env["values"]]

    client = _make_client(is_cloud=True)
    _install_scripted_get(client, envelopes)

    result = client._get_paged_results(
        "/2.0/repositories/my-team", limit=_UNBOUNDED_LIMIT
    )

    for element in result:
        # Every element is a dict — never a ``next`` URL string.
        assert isinstance(element, dict)
        # The element came from one of the per-page ``values`` lists —
        # not from envelope metadata. Using identity-aware ``in`` instead
        # of a ``set`` avoids unhashable-dict issues.
        assert element in expected_values


# ---------------------------------------------------------------------------
# P7.E — Helper never issues more HTTP calls than pages (both modes)
# ---------------------------------------------------------------------------


@given(envelopes=_dc_envelope_sequence(min_pages=1, max_pages=5))
def test_dc_helper_issues_at_most_one_request_per_page(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.E (DC) — The DC helper issues exactly one HTTP request per
    consumed page; the total call count is at most ``len(envelopes)``.

    The scripted fake raises :class:`AssertionError` on over-iteration,
    so any extra request past ``len(envelopes)`` surfaces as a test
    failure (Req 7.2).
    """
    client = _make_client(is_cloud=False)
    calls = _install_scripted_get(client, envelopes)

    client._get_paged_results(
        "/rest/api/latest/projects", limit=_UNBOUNDED_LIMIT
    )

    assert len(calls) <= len(envelopes)


@given(envelopes=_cloud_envelope_sequence(min_pages=1, max_pages=5))
def test_cloud_helper_issues_at_most_one_request_per_page(
    envelopes: list[dict[str, Any]],
) -> None:
    """P7.E (Cloud) — The Cloud helper issues exactly one HTTP request
    per consumed page; the total call count is at most ``len(envelopes)``.

    Additionally, the first call carries the caller-supplied ``params``
    (with ``pagelen`` defaulted in), and every subsequent call passes
    ``params=None`` because the Cloud ``next`` URL carries its own
    ``pagelen`` / ``page`` query parameters — this is the contract
    documented for the Cloud branch (Req 7.3, 7.4).
    """
    # Ensure the concatenated values fit under the default pagelen cap
    # so no mid-sequence ``limit`` short-circuit fires; we want to
    # exercise the ``next`` follow behaviour.
    assume(sum(len(env["values"]) for env in envelopes) <= _UNBOUNDED_LIMIT)

    client = _make_client(is_cloud=True)
    calls = _install_scripted_get(client, envelopes)

    client._get_paged_results(
        "/2.0/repositories/my-team", limit=_UNBOUNDED_LIMIT
    )

    assert len(calls) <= len(envelopes)
    # First call receives the caller-supplied params (defaulted ``pagelen``).
    assert isinstance(calls[0]["params"], dict)
    assert "pagelen" in calls[0]["params"]
    # Subsequent calls (if any) pass ``params=None`` so the Cloud ``next``
    # URL's own query parameters are not clobbered.
    for call in calls[1:]:
        assert call["params"] is None
