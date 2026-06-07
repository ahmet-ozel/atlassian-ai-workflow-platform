"""Invariant tests for bot ``account_id`` uniqueness.

The uniqueness invariant has three enforcement layers:

* DB layer - partial UNIQUE INDEX on
  ``automation.department_bots(service, account_id) WHERE account_id <> ''``.
* CRUD layer - :func:`_find_account_id_conflicts` in
  ``admin-dashboard-api/src/routers/departments.py`` rejects POST / PATCH
  bodies that would introduce a clash with HTTP 409.
* Boot-time - :func:`db_shared.bot_identity.validate_bot_account_id_uniqueness`
  scans the parsed ``departments.json`` document at service start-up.

This module exercises the **boot-time** validator and the
**CRUD** detector as a single Hypothesis suite so the two
implementations stay in lock-step. The same ``(service, account_id)``
pair MUST never reach a "claimed by two depts" state regardless of which
layer the operator hits.

Generation strategy
-------------------
A small alphabet keeps the search space dense in collisions - Hypothesis
shrinks pathological inputs down to a handful of dept_ids / account_ids
which makes assertion failures readable. The empty / whitespace
``account_id`` placeholder shape lives in the strategy so coverage of
the skip rule is automatic, not parameterised by hand.

Invariants under test
---------------------
* **A - clean input passes silently.** When the generated input has no
  ``(service, account_id)`` collision (post-placeholder filtering and
  intra-dept dedup), the validator returns ``None`` and never raises.
* **B - injected collision is reported with the exact tuple.** When the
  test inserts two distinct depts claiming the same non-empty
  ``(service, account_id)``, the validator raises
  ``BotAccountIdConflictError`` *and* ``.conflicts`` carries the exact
  ``(service, account_id)`` pair the test inserted.
* **C - placeholders never trigger a conflict.** Two depts whose
  ``account_id`` is empty / whitespace-only on the same service must
  pass the validator silently - those rows are not yet routing keys.
* **D - duplicate dept rows do not self-conflict.** A single ``dept_id``
  appearing twice in the input with the same non-empty ``account_id``
  is dedup'd to a single claim - the validator must not flag it.
* **E - CRUD detector mirrors the same set semantics.** For
  ``_find_account_id_conflicts(candidate, existing, skip_dept_id=...)``
  the returned list of conflicts matches the oracle "every non-skipped
  ``other`` dept × every shared ``(service, account_id)`` pair in
  ``candidate``" exactly, including the ``skip_dept_id`` semantics used
  on PATCH (a dept never compares against its own pre-image).

The CRUD detector lives outside ``db-shared`` (under
``services/admin-dashboard-api``); the test bootstraps the necessary
``sys.path`` entries so we can run the suite from the ``db-shared``
package root with ``pytest`` and still cross-validate both layers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------
# ``tests/conftest.py`` already injects ``libs/db-shared/src`` so the
# boot-time validator import below resolves. For the CRUD detector we additionally
# need the admin-dashboard-api source tree (CRUD detector) and the
# ``auth-shared`` / ``http-shared`` libs that ``routers.departments``
# transitively imports.
_DB_SHARED_TESTS = Path(__file__).resolve().parents[1]
_DB_SHARED_ROOT = _DB_SHARED_TESTS.parent
_PLATFORM_ROOT = _DB_SHARED_ROOT.parents[1]
_ADMIN_API = _PLATFORM_ROOT / "services" / "admin-dashboard-api"

for _entry in (
    _DB_SHARED_ROOT / "src",
    _ADMIN_API,
    _ADMIN_API / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
):
    if _entry.is_dir() and str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))


from db_shared.bot_identity import (  # noqa: E402
    BOT_IDENTITY_SERVICES,
    BotAccountIdConflictError,
    validate_bot_account_id_uniqueness,
)


# The CRUD detector is imported lazily so a missing optional dep
# (eg. ``filelock``) inside ``routers.departments`` only skips that
# check rather than aborting the whole module collection.
try:  # pragma: no cover - import-time guard, exercised in CI
    from src.routers.departments import (  # type: ignore[import-not-found]
        _extract_bot_identities,
        _find_account_id_conflicts,
    )

    _CRUD_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _extract_bot_identities = None  # type: ignore[assignment]
    _find_account_id_conflicts = None  # type: ignore[assignment]
    _CRUD_AVAILABLE = False
    _CRUD_IMPORT_ERROR = _exc


# ---------------------------------------------------------------------------
# Test alphabet - small enough to make collisions frequent under random
# generation, large enough that Hypothesis can still shrink to readable
# counterexamples.
# ---------------------------------------------------------------------------

_DEPT_IDS: tuple[str, ...] = ("d1", "d2", "d3", "d4")
_ACCOUNT_IDS: tuple[str, ...] = ("acct-a", "acct-b", "acct-c", "acct-d")
_PLACEHOLDERS: tuple[str, ...] = ("", " ", "   ", "\t", "\n", " \t \n ")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _account_id_strategy() -> st.SearchStrategy[str]:
    """Mix of real ids and whitespace placeholders.

    The placeholder branch is intentionally weighted lower than the real
    branch so the bulk of generated inputs exercise the conflict path; we
    still get enough placeholder coverage to validate that branch without
    needing a separate strategy.
    """

    return st.one_of(
        st.sampled_from(_ACCOUNT_IDS),
        st.sampled_from(_PLACEHOLDERS),
    )


@st.composite
def _bot_block(draw: st.DrawFn) -> dict[str, Any]:
    """A ``bot`` sub-document covering 0..N services.

    Each service is independently present-or-absent so the strategy
    explores every cell of the dept × service Cartesian space, including
    the all-empty bot block (a dept with no automation surfaces yet).
    """

    bot: dict[str, Any] = {}
    for service in BOT_IDENTITY_SERVICES:
        if draw(st.booleans()):
            bot[service] = {"account_id": draw(_account_id_strategy())}
    return bot


@st.composite
def _dept(draw: st.DrawFn, *, dept_id: str | None = None) -> dict[str, Any]:
    """A single dept config dict shaped like a ``departments.json`` row."""

    chosen_id = dept_id if dept_id is not None else draw(st.sampled_from(_DEPT_IDS))
    return {"id": chosen_id, "bot": draw(_bot_block())}


def _real_pairs(dept: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the dept's *non-placeholder* ``(service, account_id)`` pairs.

    Mirrors the validator's skip rule (whitespace-only ids do not
    participate). Used as the in-test oracle so we never depend on the
    SUT to compute its own expectation.
    """

    bot = dept.get("bot") or {}
    if not isinstance(bot, dict):
        return set()
    out: set[tuple[str, str]] = set()
    for service in BOT_IDENTITY_SERVICES:
        entry = bot.get(service)
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            out.add((service, account_id.strip()))
    return out


def _has_collision(depts: list[dict[str, Any]]) -> bool:
    """Oracle: True iff two distinct dept_ids share a real pair."""

    seen: dict[tuple[str, str], str] = {}
    for dept in depts:
        dept_id = dept.get("id")
        if not isinstance(dept_id, str) or not dept_id:
            continue
        for pair in _real_pairs(dept):
            owner = seen.get(pair)
            if owner is not None and owner != dept_id:
                return True
            if owner is None:
                seen[pair] = dept_id
    return False


# ---------------------------------------------------------------------------
# Clean inputs pass silently.
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(depts=st.lists(_dept(), min_size=0, max_size=8))
def test_no_collision_passes_silently(depts: list[dict[str, Any]]) -> None:
    """A: when the generated input has no real ``(service, account_id)``
    collision the validator MUST return ``None`` without raising.
    """

    assume(not _has_collision(depts))

    # Validator returns None on success - capture the return value to
    # make the contract explicit in the assertion.
    assert validate_bot_account_id_uniqueness(depts) is None


# ---------------------------------------------------------------------------
# Injected collision is reported with the exact tuple.
# ---------------------------------------------------------------------------


@st.composite
def _collision_input(draw: st.DrawFn) -> tuple[
    list[dict[str, Any]], str, str, str, str
]:
    """Build a dept list that is guaranteed to contain a known clash.

    The fixture returns the dept list together with the
    ``(service, account_id, dept_a, dept_b)`` quadruple the test
    deliberately inserted so the assertion can pin the exact tuple
    without re-deriving it from the SUT.
    """

    base = draw(st.lists(_dept(), min_size=0, max_size=4))
    service = draw(st.sampled_from(BOT_IDENTITY_SERVICES))
    account_id = draw(st.sampled_from(_ACCOUNT_IDS))
    dept_a = draw(st.sampled_from(_DEPT_IDS))
    dept_b = draw(st.sampled_from(_DEPT_IDS).filter(lambda x: x != dept_a))

    # Inserting via ``.copy()`` keeps the upstream ``base`` list
    # immutable across Hypothesis shrink rounds.
    depts = list(base) + [
        {"id": dept_a, "bot": {service: {"account_id": account_id}}},
        {"id": dept_b, "bot": {service: {"account_id": account_id}}},
    ]
    return depts, service, account_id, dept_a, dept_b


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(payload=_collision_input())
def test_collision_raises_with_exact_pair(
    payload: tuple[list[dict[str, Any]], str, str, str, str],
) -> None:
    """B: an inserted collision MUST raise and surface the same
    ``(service, account_id)`` tuple in ``.conflicts``.
    """

    depts, service, account_id, dept_a, dept_b = payload

    with pytest.raises(BotAccountIdConflictError) as exc_info:
        validate_bot_account_id_uniqueness(depts)

    surfaced = {(c.service, c.account_id) for c in exc_info.value.conflicts}
    assert (service, account_id) in surfaced

    # The dept_ids attached to the offending conflict include both
    # depts the test inserted (and possibly more, if ``base`` already
    # claimed the pair). Use a subset assertion so we tolerate that.
    inserted_pair_conflict = next(
        c
        for c in exc_info.value.conflicts
        if c.service == service and c.account_id == account_id
    )
    assert {dept_a, dept_b}.issubset(set(inserted_pair_conflict.dept_ids))


# ---------------------------------------------------------------------------
# Placeholders never trigger a conflict.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    service=st.sampled_from(BOT_IDENTITY_SERVICES),
    placeholder_a=st.sampled_from(_PLACEHOLDERS),
    placeholder_b=st.sampled_from(_PLACEHOLDERS),
)
def test_placeholder_account_ids_never_conflict(
    service: str, placeholder_a: str, placeholder_b: str
) -> None:
    """C: two depts with empty/whitespace ``account_id`` on the same
    service MUST pass the validator silently.
    """

    depts: list[dict[str, Any]] = [
        {"id": "d1", "bot": {service: {"account_id": placeholder_a}}},
        {"id": "d2", "bot": {service: {"account_id": placeholder_b}}},
    ]
    # Must not raise.
    assert validate_bot_account_id_uniqueness(depts) is None


# ---------------------------------------------------------------------------
# Duplicate dept rows are deduped, not flagged.
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    service=st.sampled_from(BOT_IDENTITY_SERVICES),
    account_id=st.sampled_from(_ACCOUNT_IDS),
    dept_id=st.sampled_from(_DEPT_IDS),
)
def test_intra_dept_duplicate_is_single_claim(
    service: str, account_id: str, dept_id: str
) -> None:
    """D: the same ``dept_id`` declaring the same ``account_id`` twice
    counts as a single claim - the validator MUST NOT raise.
    """

    depts: list[dict[str, Any]] = [
        {"id": dept_id, "bot": {service: {"account_id": account_id}}},
        {"id": dept_id, "bot": {service: {"account_id": account_id}}},
    ]
    assert validate_bot_account_id_uniqueness(depts) is None


# ---------------------------------------------------------------------------
# CRUD detector ``_find_account_id_conflicts`` matches the
# oracle's set-intersection semantics, with ``skip_dept_id`` honoured.
# ---------------------------------------------------------------------------


@st.composite
def _crud_inputs(draw: st.DrawFn) -> tuple[
    dict[str, Any], list[dict[str, Any]], str | None
]:
    candidate_id = draw(st.sampled_from(_DEPT_IDS))
    candidate = draw(_dept(dept_id=candidate_id))
    existing = draw(st.lists(_dept(), min_size=0, max_size=6))
    # On PATCH the CRUD layer skips the dept being updated. ``None``
    # mirrors POST (no skip).
    skip_self = draw(st.booleans())
    skip_dept_id = candidate_id if skip_self else None
    return candidate, existing, skip_dept_id


@pytest.mark.skipif(
    not _CRUD_AVAILABLE,
    reason=(
        "admin-dashboard-api routers.departments could not be imported "
        f"(reason: {locals().get('_CRUD_IMPORT_ERROR', 'unknown')})"
    ),
)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(payload=_crud_inputs())
def test_crud_conflict_detector_matches_oracle(
    payload: tuple[dict[str, Any], list[dict[str, Any]], str | None],
) -> None:
    """E: ``_find_account_id_conflicts`` returns one conflict entry per
    non-skipped existing dept × each ``(service, account_id)`` pair the
    candidate shares with that dept.
    """

    candidate, existing, skip_dept_id = payload

    actual = _find_account_id_conflicts(  # type: ignore[misc]
        candidate, existing, skip_dept_id=skip_dept_id
    )

    # Oracle reproduces the detector's loop using only the
    # ``_extract_bot_identities`` helper (which we test transitively
    # via the SUT itself).
    candidate_pairs = set(_extract_bot_identities(candidate))  # type: ignore[misc]
    expected: list[dict[str, str]] = []
    for other in existing:
        other_id = other.get("id")
        if skip_dept_id is not None and other_id == skip_dept_id:
            continue
        other_pairs = set(_extract_bot_identities(other))  # type: ignore[misc]
        for service, account_id in candidate_pairs:
            if (service, account_id) in other_pairs:
                expected.append(
                    {
                        "service": service,
                        "account_id": account_id,
                        "dept_id": str(other_id) if other_id else "",
                    }
                )

    # The detector preserves ``existing`` iteration order and
    # iterates ``candidate_pairs`` from the candidate's bot dict in
    # service-declaration order, but ``_extract_bot_identities``
    # returns pairs in ``BOT_IDENTITY_SERVICES`` order - same as our
    # oracle. Compare as multisets so order across nested loops is
    # not over-specified.
    def _key(c: dict[str, str]) -> tuple[str, str, str]:
        return (c["service"], c["account_id"], c["dept_id"])

    assert sorted(actual, key=_key) == sorted(expected, key=_key)

    # Additionally check the empty-conflict invariants:
    # * a candidate with no real pairs never produces conflicts;
    # * a candidate self-skipped against existing == [candidate]
    # never produces conflicts.
    if not candidate_pairs:
        assert actual == []
    if (
        skip_dept_id is not None
        and len(existing) == 1
        and existing[0].get("id") == skip_dept_id
    ):
        assert actual == []
