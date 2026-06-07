"""Invariant test: Iteration authorization.

** - Iteration authorization
-------------------------------------
*For any* ``[iterate]`` command, the helper:func:`automation_worker.activities.iteration_manager.is_authorized_for_iterate`
SHALL return ``True`` if and only if the comment author is in the
department's ``approvers`` list OR is the issue reporter; in every
other case (including an empty author account id) the helper SHALL
return ``False``.

This module pins three derived sub-properties:

1. **Positive path.** If ``author_account_id`` is non-empty and either
 appears in ``approvers`` or equals a non-empty
 ``issue_reporter_account_id``, the helper returns ``True``.
2. **Negative path.** If ``author_account_id`` is non-empty but is
 *neither* in ``approvers`` *nor* equal to a non-empty
 ``issue_reporter_account_id``, the helper returns ``False``.
3. **Empty author guard.** A misconfigured webhook that drops the
 actor account id (``""``) MUST never authorize anyone - even when
 the empty string happens to land in ``approvers`` or matches an
 empty ``issue_reporter_account_id``.

Bonus - dispatcher lock-step
----------------------------
The mirror static method:py:meth:`webhooks.dispatcher.WebhookDispatcher._is_iterate_authorized`
implements the same predicate against a:class:`WebhookPayload` /:class:`DepartmentConfig` pair. below asserts both helpers
agree byte-for-byte on every example so the dispatcher and the
activity cannot drift out of sync as the surrounding code evolves.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap - mirror sibling Invariant tests
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_WORKER_SRC: Path = _WORKER_ROOT / "src"
if str(_WORKER_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKER_SRC))

# The dispatcher mirror lives in the ``automation-service`` tree so we
# add its ``src`` directory to ``sys.path`` as well. The dispatcher
# only relies on stdlib + a relative import from ``webhooks.loop_guard``
# at module load time; the audit-logger import is lazy and not exercised
# by the static authorization helper, so no further plumbing is needed.
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_AUTOMATION_SERVICE_SRC: Path = (
    _PLATFORM_ROOT / "services" / "automation-service" / "src"
)
if str(_AUTOMATION_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SERVICE_SRC))

# pylint: disable=wrong-import-position
from automation_worker.activities.iteration_manager import (  # noqa: E402
    is_authorized_for_iterate,
)
from webhooks.dispatcher import (  # noqa: E402
    DepartmentConfig,
    WebhookDispatcher,
)
from webhooks.loop_guard import WebhookPayload  # noqa: E402


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Atlassian-flavoured account-id strings.
#:
#: We deliberately allow the empty string in the *author* strategy so
#: is exercised every time Hypothesis
#: hits the ``""`` corner. ``account_ids`` (plural) and ``reporter_ids``
#: derive from this base.
_ACCOUNT_ID = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_:",
    ),
    min_size=0,
    max_size=24,
)

#: Approver list - may contain the empty string. The helper must NOT
#: treat ``"" in approvers`` as authorization for an empty author
#:.
_APPROVERS_LIST = st.lists(_ACCOUNT_ID, min_size=0, max_size=8)

#: Reporter is ``None`` or an account id (possibly empty). The helper
#: only authorises when the reporter is *truthy* and equals the author.
_REPORTER = st.one_of(st.none(), _ACCOUNT_ID)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected(
    *,
    author: str,
    approvers: list[str],
    reporter: str | None,
) -> bool:
    """Independent re-statement of the contract.

 Hypothesis tests should compare the helper's output against an
 *independent* derivation of the spec - not against the helper
 itself. This routine re-encodes the three rules from the
 requirement:

 * empty author  never authorised,
 * author in approvers  authorised,
 * author equals a non-empty reporter  authorised,
 * otherwise  not authorised.

 Booleans short-circuit through the same order the spec describes;
 the order does not affect the final result because the predicates
 are combined via ``OR``.
 """

    if not author:
        return False
    if author in approvers:
        return True
    if reporter and author == reporter:
        return True
    return False


def _dispatcher_decision(
    *,
    author: str,
    approvers: list[str],
    reporter: str | None,
) -> bool:
    """Invoke the dispatcher's mirror predicate with equivalent inputs."""

    payload = WebhookPayload(
        actor_account_id=author or None,
        # The remaining payload fields are irrelevant to the
        # authorization predicate - the static method only reads
        # ``actor_account_id`` and ``reporter_account_id``.
        issue_key="PAY-1",
        event_type="jira:comment_created",
        comment_body="[iterate]",
        assignee_account_id=None,
        reporter_account_id=reporter,
        dept_id="platform",
        trace_id=None,
    )
    dept_config = DepartmentConfig(
        dept_id="platform",
        mode="active",
        approvers=tuple(approvers),
    )
    return WebhookDispatcher._is_iterate_authorized(payload, dept_config)


# ---------------------------------------------------------------------------
# - positive path
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    author=_ACCOUNT_ID.filter(lambda s: bool(s)),
    other_approvers=_APPROVERS_LIST,
    reporter=_REPORTER,
    include_in_approvers=st.booleans(),
    use_reporter_match=st.booleans(),
)
def test_authorized_when_in_approvers_or_is_reporter(
    author: str,
    other_approvers: list[str],
    reporter: str | None,
    include_in_approvers: bool,
    use_reporter_match: bool,
) -> None:
    """If author ∈ approvers OR author == non-empty reporter  ``True``.

 The strategy synthesises both authorization paths in a single
 example: ``include_in_approvers`` injects the author into the
 approvers list, while ``use_reporter_match`` overrides ``reporter``
 with the author's id. At least one of the two flags must be true
 for the example to count toward this property; when neither holds
 the example is filtered out via Hypothesis ``assume`` semantics.
 """

    approvers = list(other_approvers)
    if include_in_approvers:
        approvers.append(author)

    effective_reporter = author if use_reporter_match else reporter

    # Skip examples that don't actually exercise the positive path -
    # they belong to.
    if not include_in_approvers and not (
        effective_reporter and author == effective_reporter
    ):
        return

    result = is_authorized_for_iterate(
        author_account_id=author,
        approvers=approvers,
        issue_reporter_account_id=effective_reporter,
    )

    assert result is True, (
        "is_authorized_for_iterate returned False for an authorized "
        f"actor: author={author!r}, approvers={approvers!r}, "
        f"reporter={effective_reporter!r}"
    )

    # Cross-check against the independent spec re-statement so a bug
    # in the helper that *also* matches the spec re-statement would
    # require two independent regressions.
    assert (
        _expected(
            author=author,
            approvers=approvers,
            reporter=effective_reporter,
        )
        is True
    )


# ---------------------------------------------------------------------------
# - negative path
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    author=_ACCOUNT_ID.filter(lambda s: bool(s)),
    approvers=_APPROVERS_LIST,
    reporter=_REPORTER,
)
def test_unauthorized_when_neither_approver_nor_reporter(
    author: str,
    approvers: list[str],
    reporter: str | None,
) -> None:
    """If author ∉ approvers AND author ≠ non-empty reporter  ``False``.

 The example is only counted when *both* conditions fail
 simultaneously; otherwise we fall under 's domain.
 """

    if author in approvers:
        return
    if reporter and author == reporter:
        return

    result = is_authorized_for_iterate(
        author_account_id=author,
        approvers=approvers,
        issue_reporter_account_id=reporter,
    )

    assert result is False, (
        "is_authorized_for_iterate returned True for an unauthorized "
        f"actor: author={author!r}, approvers={approvers!r}, "
        f"reporter={reporter!r}"
    )
    assert (
        _expected(
            author=author,
            approvers=approvers,
            reporter=reporter,
        )
        is False
    )


# ---------------------------------------------------------------------------
# - empty author always denied
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    approvers=_APPROVERS_LIST,
    reporter=_REPORTER,
)
def test_empty_author_is_never_authorized(
    approvers: list[str],
    reporter: str | None,
) -> None:
    """Empty author id  ``False`` regardless of approvers / reporter.

 A webhook that drops the actor accountId must not silently grant
 access - even when the approvers list happens to contain the
 empty string, or when ``reporter_account_id`` is itself empty.
 """

    result = is_authorized_for_iterate(
        author_account_id="",
        approvers=approvers,
        issue_reporter_account_id=reporter,
    )

    assert result is False, (
        "is_authorized_for_iterate returned True for an empty "
        f"author: approvers={approvers!r}, reporter={reporter!r}"
    )
    assert (
        _expected(author="", approvers=approvers, reporter=reporter)
        is False
    )


# ---------------------------------------------------------------------------
# - dispatcher mirror lock-step
# ---------------------------------------------------------------------------


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    author=_ACCOUNT_ID,
    approvers=_APPROVERS_LIST,
    reporter=_REPORTER,
)
def test_dispatcher_mirror_matches_helper(
    author: str,
    approvers: list[str],
    reporter: str | None,
) -> None:
    """Dispatcher and activity helpers MUST agree on every input.

 The dispatcher's:py:meth:`WebhookDispatcher._is_iterate_authorized`
 and the activity's:func:`is_authorized_for_iterate` encode the
 same predicate. Drift between the two would let a
 ``[iterate]`` command pass the dispatcher's gate only to be
 rejected by the activity (or vice versa). This property pins
 them to byte-for-byte agreement on every input - including the
 ``author == ""`` corner the dispatcher reads as ``actor_account_id
 is None`` via:class:`WebhookPayload`.
 """

    helper_decision = is_authorized_for_iterate(
        author_account_id=author,
        approvers=approvers,
        issue_reporter_account_id=reporter,
    )
    dispatcher_decision = _dispatcher_decision(
        author=author,
        approvers=approvers,
        reporter=reporter,
    )
    spec_decision = _expected(
        author=author,
        approvers=approvers,
        reporter=reporter,
    )

    assert helper_decision == dispatcher_decision == spec_decision, (
        "Authorization decisions diverged: helper="
        f"{helper_decision}, dispatcher={dispatcher_decision}, "
        f"spec={spec_decision} for author={author!r}, "
        f"approvers={approvers!r}, reporter={reporter!r}"
    )


# ---------------------------------------------------------------------------
# Pinned regression examples
# ---------------------------------------------------------------------------
#
# A handful of explicit corner cases for the helper. Hypothesis covers
# them in expectation but pinning them as plain examples keeps the
# regression signal sharp when running just this file.


def test_pinned_in_approvers() -> None:
    assert is_authorized_for_iterate(
        author_account_id="alice",
        approvers=["alice", "bob"],
        issue_reporter_account_id="charlie",
    )


def test_pinned_is_reporter() -> None:
    assert is_authorized_for_iterate(
        author_account_id="charlie",
        approvers=["alice"],
        issue_reporter_account_id="charlie",
    )


def test_pinned_neither() -> None:
    assert not is_authorized_for_iterate(
        author_account_id="dave",
        approvers=["alice", "bob"],
        issue_reporter_account_id="charlie",
    )


def test_pinned_empty_author_with_empty_in_approvers() -> None:
    # The empty string lives in ``approvers`` but the empty-author
    # guard rejects the request before the membership check fires.
    assert not is_authorized_for_iterate(
        author_account_id="",
        approvers=["", "alice"],
        issue_reporter_account_id="",
    )


def test_pinned_empty_reporter_does_not_authorize_empty_author() -> None:
    # Both author and reporter are empty - the truthiness gate on
    # ``issue_reporter_account_id`` blocks the reporter branch.
    assert not is_authorized_for_iterate(
        author_account_id="",
        approvers=[],
        issue_reporter_account_id="",
    )
