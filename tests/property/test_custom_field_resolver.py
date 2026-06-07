"""Jira custom field name  id resolver TTL cache.



Hypothesis-driven complement to the resolver's unit suite
(``services/automation-service/tests/unit/test_jira_field_resolver.py``)
and the static AST scanner
(``tests/property/test_no_hardcoded_field_ids.py``). Together the
three files cover field-id lookup behavior end-to-end:

* Static (this directory's ``test_no_hardcoded_field_ids.py``) -
 *no* in-scope module hard-codes a ``customfield_<digits>`` literal.
* Runtime contract (the unit tests) - first call fetches, subsequent
 calls within TTL skip HTTP, calls past TTL refetch, concurrent
 callers coalesce, unknown name raises.
* Schedule behavior (this file) - across **randomised** schedules of
 ``resolve_field_id`` calls and time advances, the *count* of
 HTTP fetches matches a closed-form prediction derived purely from
 the schedule and the configured TTL.

The headline behavior the property pins is straightforward but
brittle to regressions: the resolver MUST issue exactly **one**
``GET /rest/api/3/field`` per TTL bucket that contains at least one:meth:`JiraFieldResolver.resolve_field_id` call. Any departure -
caching too aggressively (skipping a fetch the schedule demands) or
not aggressively enough (issuing more fetches than expected) - fails
the test with a counter-example shrunk by Hypothesis to the smallest
schedule that exhibits the bug.

Why the property is robust against implementation drift
-------------------------------------------------------

The unit tests pin a small set of *specific* schedules: one call,
two-call-within-TTL, three-call-across-TTL, eight-concurrent. They
are excellent regression sentinels but cannot enumerate every
ordering of ``resolve`` × ``advance`` events that real workloads
produce. This property fills that gap:

* Hypothesis builds an arbitrary schedule of up to ``MAX_EVENTS``
 events drawn from ``{resolve, advance}``.
* The schedule is replayed against the resolver under a manual
 clock, so wall-clock determinism is preserved (no flakiness from
 ``datetime.now``).
* A reference computation -:func:`_predicted_fetch_count` - derives
 the expected HTTP fetch count from the schedule alone, without
 consulting the resolver. The schedule is "binned" by TTL window:
 every bin that contains at least one ``resolve`` event contributes
 exactly one fetch.
* The test asserts the resolver's ``call_count`` matches the
 predicted value. A mismatch surfaces as a Hypothesis falsifying
 example (printed as ``[event, event,...]``), making the failure
 reproducible by hand.

The reference computation deliberately bypasses the resolver's own
state-machine. If the resolver mutates its cache in some new way -
e.g. by adopting a stale-while-revalidate strategy or memoising
unknown-field misses differently - the property forces the author
to reckon with whether the new behaviour still satisfies the
"one fetch per non-empty TTL bucket" contract. Drift that violates
the expected behavior fails loudly; intentional behavior changes
require updating both the resolver *and* the predictor here.

Strategy bounds
---------------

* ``MAX_EVENTS = 24`` - large enough to cross several TTL boundaries
 in a single schedule, small enough to keep per-example cost
 bounded. Hypothesis spends most of its budget shrinking, not
 exploring deeper schedules.
* ``MAX_DELTA_SECONDS = 9000`` - 2.5 hours, comfortably larger than
 the 1 hour default TTL so a substantial fraction of randomly
 generated schedules contains at least one cache-busting advance.
* ``TTL`` choices include ``timedelta(0)`` (degenerate "never
 cache" mode - every resolve is its own bucket) and a generous 4
 hour upper bound so the predictor is exercised across both
 extremes the resolver supports.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

# Importing:mod:`automation_service.jira_field_resolver` triggers
# ``automation_service.__init__``  ``automation_service.app``
# ``from src.config import Settings``. Two ``sys.path`` entries are
# therefore required: the ``src/`` directory (so the
# ``automation_service`` *package* resolves) and the
# ``automation-service/`` root (so the legacy ``src.*`` re-export
# layer resolves). Mirrors the bootstrap in
# ``services/automation-service/tests/unit/test_app.py``.
_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "automation-service"
)
for _bs in (_AUTOMATION_ROOT / "src", _AUTOMATION_ROOT):
    _bs_str = str(_bs)
    if _bs.exists() and _bs_str not in sys.path:
        sys.path.insert(0, _bs_str)

from automation_service.jira_field_resolver import (  # noqa: E402
    JiraFieldNotFoundError,
    JiraFieldResolver,
)


# ---------------------------------------------------------------------------
# Test doubles - manual clock and recording HTTP fake
# ---------------------------------------------------------------------------


@dataclass
class _ManualClock:
    """A controllable monotonic UTC clock.

 The resolver accepts any ``Callable[[], datetime]``. Driving time
 by hand keeps the property deterministic - Hypothesis
 schedules pure ``advance`` durations, the clock applies them in
 order, and the resolver evaluates its TTL predicate against the
 same sequence on every replay.
 """

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        # ``timedelta`` is signed, but the property's strategy only
        # generates non-negative deltas - keep the clock monotonic.
        self.now = self.now + delta


class _RecordingJiraClient:
    """In-memory ``JiraFieldClient`` Protocol fake.

 Records the number of ``get_fields`` invocations so the test
 can compare against the predictor. The returned descriptors are
 intentionally minimal - only the ``id`` and ``name`` keys the
 resolver consumes are populated. Mirrors the unit-test fake
 (``services/automation-service/tests/unit/test_jira_field_resolver.py``)
 minus the concurrency gate, which is exercised by the unit
 suite separately.
 """

    def __init__(self, fields: Iterable[Mapping[str, Any]]) -> None:
        self._fields = [dict(d) for d in fields]
        self.call_count: int = 0

    async def get_fields(self) -> Iterable[Mapping[str, Any]]:
        self.call_count += 1
        # Hand back fresh copies to mirror the production client's
        # "no shared mutable state" contract.
        return [dict(d) for d in self._fields]


# ---------------------------------------------------------------------------
# Sample field payload
# ---------------------------------------------------------------------------

#: Two field names are enough for the property - the test only needs
#: to verify cache behaviour, not name disambiguation. Both ids are
#: realistic Jira shapes; neither is referenced as a literal anywhere
#: in the resolver source (literal checks are enforced separately by
#: ``test_no_hardcoded_field_ids.py``).
_SAMPLE_FIELDS: tuple[dict[str, str], ...] = (
    {"id": "customfield_10020", "name": "Sprint"},
    {"id": "customfield_10014", "name": "Epic Link"},
)

#: Names the resolver may be asked to resolve. The property never
#: feeds an unknown name through this strategy (unknown handling is
#: covered exhaustively by the unit suite); randomising over the two
#: known names is enough to exercise the "second name within the
#: same TTL bucket reuses the cache" branch.
_KNOWN_NAMES: tuple[str, ...] = tuple(f["name"] for f in _SAMPLE_FIELDS)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Cap on schedule length per Hypothesis example. Keeps each replay
#: bounded while still allowing several TTL crossings.
MAX_EVENTS: int = 24

#: Upper bound (seconds) on a single ``advance`` event. 2.5 hours
#: comfortably exceeds the largest TTL the strategy generates so
#: cache-bust schedules are routinely produced.
MAX_DELTA_SECONDS: int = 9000

#: TTL strategy - sampled from ``{0, 1s, 60s, 1h, 4h}``. ``0`` is a
#: degenerate but valid configuration (every resolve refetches);
#: ``1h`` is the production default.
_ttl_strategy = st.sampled_from(
    [
        timedelta(seconds=0),
        timedelta(seconds=1),
        timedelta(seconds=60),
        timedelta(hours=1),
        timedelta(hours=4),
    ]
)

#: A ``resolve`` event carries the field name to look up.
_resolve_event = st.builds(
    lambda name: ("resolve", name),
    st.sampled_from(_KNOWN_NAMES),
)

#: An ``advance`` event carries the duration to roll the clock by.
#: ``min_value=0`` ensures we sometimes generate a no-op advance
#: (which must NOT trigger any extra fetch on its own).
_advance_event = st.builds(
    lambda secs: ("advance", timedelta(seconds=secs)),
    st.integers(min_value=0, max_value=MAX_DELTA_SECONDS),
)

#: A schedule is a sequence of mixed events. ``min_size=1`` so the
#: predictor doesn't have to special-case "no resolves at all" (which
#: trivially produces zero fetches).
_schedule_strategy = st.lists(
    st.one_of(_resolve_event, _advance_event),
    min_size=1,
    max_size=MAX_EVENTS,
)


# ---------------------------------------------------------------------------
# Reference predictor
# ---------------------------------------------------------------------------


def _predicted_fetch_count(
    schedule: list[tuple[str, Any]],
    ttl: timedelta,
) -> int:
    """Return the expected number of HTTP fetches for *schedule*.

 The contract is: every ``resolve`` event whose virtual clock is
 at or beyond the previous fetch's expiry triggers a new fetch.
 The first ``resolve`` in any schedule always fetches (the cache
 is empty); subsequent resolves reuse the cache iff
 ``clock - last_fetch_at < ttl``.

 We replay the schedule against a virtual clock and a single
 ``last_fetch_at`` slot, mirroring the resolver's internal state
 machine but without any HTTP plumbing. Keeping this predictor
 lock-step with the spec - *not* with the resolver's
 implementation - is what gives the property its independence:
 if the resolver drifts from the spec, the assertion fails; if
 the spec changes, both the resolver and the predictor must be
 updated together.

 Important: ``resolve`` events with unknown field names are not
 generated by the strategy, so the predictor doesn't need to
 handle the ``JiraFieldNotFoundError`` path. Unknown-name
 behaviour is covered exhaustively by the unit suite.
 """

    last_fetched_at: datetime | None = None
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fetches = 0

    for kind, payload in schedule:
        if kind == "advance":
            clock = clock + payload
            continue
        # ``resolve``
        if last_fetched_at is None or clock - last_fetched_at >= ttl:
            fetches += 1
            last_fetched_at = clock

    return fetches


def _iter_resolves(
    schedule: list[tuple[str, Any]],
) -> Iterator[tuple[str, Any]]:
    """Yield every event in *schedule* (preserves input order).

 Trivial helper kept named so the replay loop in the property
 body reads as plainly as the predictor.
 """

    yield from schedule


# ---------------------------------------------------------------------------
# Resolver cache behavior
# ---------------------------------------------------------------------------


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=(
        # The strategy uses ``asyncio.run`` per example which trips
        # the function-scoped fixture watchdog; the resolver state
        # is fully reset per example so this is benign.
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ),
)
@given(schedule=_schedule_strategy, ttl=_ttl_strategy)
def test_resolver_fetch_count_matches_ttl_bucket_prediction(
    schedule: list[tuple[str, Any]],
    ttl: timedelta,
) -> None:
    """For any randomised schedule of ``resolve`` and ``advance``
 events, the resolver issues exactly one ``GET /rest/api/3/field``
 fetch per TTL bucket that contains at least one resolve.

 Failure modes the property catches:

 * **Cache disabled regression** - if the resolver ever forgets
 to populate ``self._cache`` after a successful fetch, every
 ``resolve`` becomes its own bucket and the actual fetch count
 explodes past the prediction.
 * **TTL boundary inversion** - if the freshness predicate flips
 from ``>=`` to ``>`` (or vice versa), schedules that land
 exactly on the boundary diverge from the prediction by ±1.
 * **Snapshot mutation** - if the cache is mutated in place
 mid-refresh, concurrent-ish replay shapes can observe a
 half-populated mapping and trigger an unexpected
 ``KeyError``-driven refetch. The property surfaces this as a
 counter-example whose schedule mixes ``advance`` and
 ``resolve`` interleaved across the boundary.

 The test runs each example under:func:`asyncio.run` so the
 resolver's ``async`` surface is exercised end-to-end. Per-example
 state (resolver, clock, fake client) is constructed fresh, so
 one failing case cannot poison subsequent ones.
 """

    expected = _predicted_fetch_count(schedule, ttl)

    async def _replay() -> None:
        clock = _ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        client = _RecordingJiraClient(_SAMPLE_FIELDS)
        resolver = JiraFieldResolver(client, ttl=ttl, now=clock)

        for kind, payload in _iter_resolves(schedule):
            if kind == "advance":
                clock.advance(payload)
            else:
                # ``resolve`` - payload is the field name.
                result = await resolver.resolve_field_id(payload)
                # Sanity: the resolver must return the matching id
                # for every known name. The predictor doesn't depend
                # on this, but checking it inside the loop catches
                # any silent regression where the cache returns the
                # wrong descriptor.
                expected_id = next(
                    f["id"] for f in _SAMPLE_FIELDS if f["name"] == payload
                )
                assert result == expected_id, (
                    f"resolver returned {result!r} for {payload!r}; "
                    f"expected {expected_id!r}"
                )

        assert client.call_count == expected, (
            f"fetch count mismatch for ttl={ttl!r} schedule={schedule!r}: "
            f"resolver issued {client.call_count} fetches, "
            f"predictor expected {expected}"
        )

    asyncio.run(_replay())


# ---------------------------------------------------------------------------
# Targeted checks that the predictor itself is
# right. These operate over the predictor (no resolver
# involvement) so a regression in the predictor cannot silently mask
# a regression in the resolver.
# ---------------------------------------------------------------------------


class TestPredictorSelfChecks:
    """Pin:func:`_predicted_fetch_count` against hand-checked cases.

 The property above is only as strong as the predictor that
 powers it. These checks exercise the predictor against the same
 fixed schedules covered by the resolver's unit suite, so any
 drift between the spec and the predictor surfaces as a failure
 here rather than a silent mismatch in the property.
 """

    def test_empty_schedule_is_zero_fetches(self) -> None:
        # Strategy enforces ``min_size=1``, but the predictor is
        # well-defined on the empty schedule; pin that explicitly.
        assert _predicted_fetch_count([], timedelta(hours=1)) == 0

    def test_single_resolve_is_one_fetch(self) -> None:
        schedule = [("resolve", "Sprint")]
        assert _predicted_fetch_count(schedule, timedelta(hours=1)) == 1

    def test_two_resolves_within_ttl_is_one_fetch(self) -> None:
        schedule = [
            ("resolve", "Sprint"),
            ("advance", timedelta(minutes=30)),
            ("resolve", "Sprint"),
        ]
        assert _predicted_fetch_count(schedule, timedelta(hours=1)) == 1

    def test_two_resolves_across_ttl_is_two_fetches(self) -> None:
        schedule = [
            ("resolve", "Sprint"),
            ("advance", timedelta(hours=1)),
            ("resolve", "Sprint"),
        ]
        # TTL boundary is inclusive  equality is stale  refetch.
        assert _predicted_fetch_count(schedule, timedelta(hours=1)) == 2

    def test_zero_ttl_means_one_fetch_per_resolve(self) -> None:
        schedule = [
            ("resolve", "Sprint"),
            ("resolve", "Epic Link"),
            ("resolve", "Sprint"),
        ]
        assert _predicted_fetch_count(schedule, timedelta(0)) == 3

    def test_advance_only_schedule_is_zero_fetches(self) -> None:
        schedule = [
            ("advance", timedelta(hours=1)),
            ("advance", timedelta(hours=1)),
        ]
        assert _predicted_fetch_count(schedule, timedelta(hours=1)) == 0

    def test_second_name_within_ttl_reuses_cache(self) -> None:
        """Resolving a different name still hits the cache."""

        schedule = [
            ("resolve", "Sprint"),
            ("resolve", "Epic Link"),
        ]
        assert _predicted_fetch_count(schedule, timedelta(hours=1)) == 1


# ---------------------------------------------------------------------------
# Static AST counterpart - no hard-coded field id literals
# ---------------------------------------------------------------------------
#
# The structural complement to the runtime cache
# property is enforced by ``tests/property/test_no_hardcoded_field_ids.py``
# (sibling file in this directory). That scanner walks every ``.py``
# under ``services/automation-service``, ``workers/`` and ``libs/``
# and asserts no string literal matches ``^customfield_\d+$``. This
# file does not duplicate the AST scan; the two files together cover
# field-id lookup behavior end-to-end:
#
# * runtime - this file (cache + TTL semantics under randomised
# schedules);
# * static - sibling file (no literal field ids escape into source).
#
# A pointer-test below makes the relationship explicit so a future
# contributor cannot accidentally delete one half of the property
# without noticing.


def test_static_ast_counterpart_exists() -> None:
    """The sibling AST scanner must remain alongside this file.

 Runtime cache checks here pair with a static literal scanner next
 door. Removing either half weakens coverage, so we pin the
 sibling file's existence as a structural assertion. The check
 is intentionally cheap (a single ``Path.is_file``) so it adds
 no measurable cost to the suite.
 """

    sibling = Path(__file__).with_name("test_no_hardcoded_field_ids.py")
    assert sibling.is_file(), (
        "field-id lookup coverage requires both cache and literal checks: "
        f"missing static AST scanner at {sibling!s}. "
        "Restore it (or update this docstring if the property has "
        "been intentionally restructured)."
    )


# ---------------------------------------------------------------------------
# Smoke - direct exercise of the unknown-name path
# ---------------------------------------------------------------------------
#
# The property strategy never produces unknown names (the predictor
# would otherwise need to model a separate exception path that the
# unit suite already covers exhaustively). A single smoke check here
# pins the boundary so a contributor reading just this file sees the
# whole contract documented in one place.


def test_unknown_field_name_raises_after_first_fetch() -> None:
    """An unknown name surfaces as:class:`JiraFieldNotFoundError`.

 The error must be raised *after* a successful refresh so the
 caller knows the absence is real on the upstream side, not a
 stale cache miss. We assert this by checking that the recording
 client saw exactly one fetch when the resolver finally raises.
 """

    async def _run() -> None:
        clock = _ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        client = _RecordingJiraClient(_SAMPLE_FIELDS)
        resolver = JiraFieldResolver(
            client, ttl=timedelta(hours=1), now=clock
        )

        with pytest.raises(JiraFieldNotFoundError) as exc_info:
            await resolver.resolve_field_id("Definitely Not A Field")

        assert exc_info.value.field_name == "Definitely Not A Field"
        # The fetch DID happen - that's how we know the absence is
        # real and not a stale-cache artefact.
        assert client.call_count == 1

    asyncio.run(_run())
