"""Tests for workflow_id idempotency and ID format determinism.

* **workflow_id idempotency**
  *For all* valid ``workflow_id`` strings, two consecutive
  ``start_workflow`` calls with the same id produce exactly **one**
  Temporal execution; the second call returns the existing
  ``execution_id`` and ``was_existing=True`` (HTTP 202 in the caller).
  Tested with a mocked Temporal client via
  :func:`temporal_shared.start_helper.start_workflow_idempotent`.
* **Workflow ID format determinism / uniqueness**. The
  :mod:`temporal_shared.identifiers`
  formatters must be deterministic, injective, and produce IDs that
  match the documented regex patterns.

Both halves run under Hypothesis with ``max_examples ≥ 100``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from temporalio.exceptions import WorkflowAlreadyStartedError

from temporal_shared.identifiers import (
    InvalidIssueKeyError,
    InvalidSlugError,
    agent_workflow_id,
    automation_workflow_id_bb,
    automation_workflow_id_jira,
    execution_workflow_id,
)
from temporal_shared.start_helper import (
    StartResult,
    start_workflow_idempotent,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid Jira issue key: ^[A-Z][A-Z0-9_]+-[1-9][0-9]*$
# The regex requires at least one leading [A-Z] followed by one or more [A-Z0-9_],
# so the prefix must be at least 2 characters with the first being uppercase alpha.
_ISSUE_KEY_FIRST_CHAR = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ISSUE_KEY_REST = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"),
    min_size=1,
    max_size=7,
)

_ISSUE_KEY_NUMBER = st.integers(min_value=1, max_value=999999)

_VALID_ISSUE_KEY = st.builds(
    lambda first, rest, num: f"{first}{rest}-{num}",
    _ISSUE_KEY_FIRST_CHAR,
    _ISSUE_KEY_REST,
    _ISSUE_KEY_NUMBER,
)

# Valid slug: ^[a-z0-9][a-z0-9-]*$
_VALID_SLUG = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=30,
).filter(lambda s: s[0] in "abcdefghijklmnopqrstuvwxyz0123456789")

# Positive PR IDs
_PR_ID = st.integers(min_value=1, max_value=999999)

# Parent workflow IDs (non-empty strings without whitespace)
_PARENT_ID = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyz0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ),
    min_size=1,
    max_size=60,
)

# Iteration numbers
_ITERATION = st.integers(min_value=1, max_value=1000)

# Timestamps (Unix epoch seconds)
_TIMESTAMP = st.integers(min_value=0, max_value=9999999999)

# ---------------------------------------------------------------------------
# Expected format patterns
# ---------------------------------------------------------------------------

_JIRA_ID_RE = re.compile(r"^automation-jira-[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_BB_ID_RE = re.compile(r"^automation-bb-[a-z0-9][a-z0-9-]*-[a-z0-9][a-z0-9-]*-\d+$")
_AGENT_ID_RE = re.compile(r"^agent-.+-iter-\d+$")
_EXEC_ID_RE = re.compile(r"^exec-.+-\d+$")


# ---------------------------------------------------------------------------
# Format match - Jira workflow ID
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(issue_key=_VALID_ISSUE_KEY)
def test_jira_workflow_id_format_match(issue_key: str) -> None:
    """Every valid issue_key produces an ID matching the documented pattern."""
    wf_id = automation_workflow_id_jira(issue_key)
    assert _JIRA_ID_RE.match(wf_id), f"ID {wf_id!r} does not match expected format"


# ---------------------------------------------------------------------------
# Format match - Bitbucket workflow ID
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(workspace=_VALID_SLUG, repo=_VALID_SLUG, pr_id=_PR_ID)
def test_bb_workflow_id_format_match(workspace: str, repo: str, pr_id: int) -> None:
    """Every valid (workspace, repo, pr_id) produces a correctly formatted ID."""
    wf_id = automation_workflow_id_bb(workspace, repo, pr_id)
    assert _BB_ID_RE.match(wf_id), f"ID {wf_id!r} does not match expected format"


# ---------------------------------------------------------------------------
# Format match - Agent workflow ID
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(parent_id=_PARENT_ID, iteration=_ITERATION)
def test_agent_workflow_id_format_match(parent_id: str, iteration: int) -> None:
    """Every valid (parent_id, N) produces an ID matching agent-{...}-iter-{N}."""
    wf_id = agent_workflow_id(parent_id, iteration)
    assert _AGENT_ID_RE.match(wf_id), f"ID {wf_id!r} does not match expected format"


# ---------------------------------------------------------------------------
# Format match - Execution workflow ID
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(parent_id=_PARENT_ID, ts=_TIMESTAMP)
def test_execution_workflow_id_format_match(parent_id: str, ts: int) -> None:
    """Every valid (parent_id, ts) produces an ID matching exec-{...}-{ts}."""
    wf_id = execution_workflow_id(parent_id, ts)
    assert _EXEC_ID_RE.match(wf_id), f"ID {wf_id!r} does not match expected format"


# ---------------------------------------------------------------------------
# Idempotence - repeated calls yield identical output
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(issue_key=_VALID_ISSUE_KEY)
def test_jira_workflow_id_idempotent(issue_key: str) -> None:
    """Calling automation_workflow_id_jira twice with the same key yields the same ID."""
    assert automation_workflow_id_jira(issue_key) == automation_workflow_id_jira(
        issue_key
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(workspace=_VALID_SLUG, repo=_VALID_SLUG, pr_id=_PR_ID)
def test_bb_workflow_id_idempotent(workspace: str, repo: str, pr_id: int) -> None:
    """Calling automation_workflow_id_bb twice with the same args yields the same ID."""
    assert automation_workflow_id_bb(workspace, repo, pr_id) == automation_workflow_id_bb(
        workspace, repo, pr_id
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(parent_id=_PARENT_ID, iteration=_ITERATION)
def test_agent_workflow_id_idempotent(parent_id: str, iteration: int) -> None:
    """Calling agent_workflow_id twice with the same args yields the same ID."""
    assert agent_workflow_id(parent_id, iteration) == agent_workflow_id(
        parent_id, iteration
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(parent_id=_PARENT_ID, ts=_TIMESTAMP)
def test_execution_workflow_id_idempotent(parent_id: str, ts: int) -> None:
    """Calling execution_workflow_id twice with the same args yields the same ID."""
    assert execution_workflow_id(parent_id, ts) == execution_workflow_id(parent_id, ts)


# ---------------------------------------------------------------------------
# Injectivity - distinct inputs produce distinct IDs
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    key_a=_VALID_ISSUE_KEY,
    key_b=_VALID_ISSUE_KEY,
)
def test_jira_workflow_id_injectivity(key_a: str, key_b: str) -> None:
    """Distinct issue keys produce distinct Jira workflow IDs."""
    if key_a != key_b:
        assert automation_workflow_id_jira(key_a) != automation_workflow_id_jira(key_b)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    workspace_a=_VALID_SLUG,
    repo_a=_VALID_SLUG,
    pr_id_a=_PR_ID,
    workspace_b=_VALID_SLUG,
    repo_b=_VALID_SLUG,
    pr_id_b=_PR_ID,
)
def test_bb_workflow_id_injectivity(
    workspace_a: str,
    repo_a: str,
    pr_id_a: int,
    workspace_b: str,
    repo_b: str,
    pr_id_b: int,
) -> None:
    """Distinct (workspace, repo, pr_id) tuples produce distinct BB workflow IDs."""
    if (workspace_a, repo_a, pr_id_a) != (workspace_b, repo_b, pr_id_b):
        assert automation_workflow_id_bb(
            workspace_a, repo_a, pr_id_a
        ) != automation_workflow_id_bb(workspace_b, repo_b, pr_id_b)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    parent_a=_PARENT_ID,
    iter_a=_ITERATION,
    parent_b=_PARENT_ID,
    iter_b=_ITERATION,
)
def test_agent_workflow_id_injectivity(
    parent_a: str, iter_a: int, parent_b: str, iter_b: int
) -> None:
    """Distinct (parent_id, iteration) pairs produce distinct agent workflow IDs."""
    if (parent_a, iter_a) != (parent_b, iter_b):
        assert agent_workflow_id(parent_a, iter_a) != agent_workflow_id(
            parent_b, iter_b
        )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    parent_a=_PARENT_ID,
    ts_a=_TIMESTAMP,
    parent_b=_PARENT_ID,
    ts_b=_TIMESTAMP,
)
def test_execution_workflow_id_injectivity(
    parent_a: str, ts_a: int, parent_b: str, ts_b: int
) -> None:
    """Distinct (parent_id, ts) pairs produce distinct execution workflow IDs."""
    if (parent_a, ts_a) != (parent_b, ts_b):
        assert execution_workflow_id(parent_a, ts_a) != execution_workflow_id(
            parent_b, ts_b
        )


# ---------------------------------------------------------------------------
# Invalid inputs raise appropriate errors
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    bad_key=st.text(min_size=1, max_size=20).filter(
        lambda s: not re.match(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$", s)
    )
)
def test_jira_workflow_id_rejects_invalid_issue_key(bad_key: str) -> None:
    """Invalid issue keys raise InvalidIssueKeyError."""
    try:
        automation_workflow_id_jira(bad_key)
        # If it didn't raise, the key must actually be valid (filter miss)
        assert re.match(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$", bad_key)
    except InvalidIssueKeyError:
        pass  # Expected


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    bad_slug=st.text(min_size=1, max_size=20).filter(
        lambda s: not re.match(r"^[a-z0-9][a-z0-9-]*$", s)
    )
)
def test_bb_workflow_id_rejects_invalid_workspace_slug(bad_slug: str) -> None:
    """Invalid workspace slugs raise InvalidSlugError."""
    try:
        automation_workflow_id_bb(bad_slug, "valid-repo", 1)
        assert re.match(r"^[a-z0-9][a-z0-9-]*$", bad_slug)
    except InvalidSlugError:
        pass  # Expected


# =============================================================================
# workflow_id idempotency
#
# For all valid ``workflow_id`` strings, two consecutive
# ``start_workflow`` calls with the same id produce exactly **one**
# Temporal execution.  The second call must surface
# ``WorkflowAlreadyStartedError`` from the SDK, which the helper
# (:func:`temporal_shared.start_helper.start_workflow_idempotent`)
# absorbs and converts into ``StartResult(execution_id=workflow_id,
# was_existing=True)``.  Callers map this to HTTP 202.
#
# Temporal is mocked; we verify the helper's behaviour against a fake
# client that records calls and is configurable to either succeed or
# raise ``WorkflowAlreadyStartedError`` on demand.  The fake captures
# the canonical Temporal contract: any second start with the same
# workflow_id (while the first execution is alive) raises the
# duplicate exception.
# =============================================================================


# ---------------------------------------------------------------------------
# Workflow-id strategy: every documented format produced by
# ``temporal_shared.identifiers`` is a valid input here.  We sample
# from each formatter's input space and let Hypothesis explore the
# union, so the property is tested against the full surface of ids
# the platform actually generates.
# ---------------------------------------------------------------------------


_WORKFLOW_ID_STRATEGY = st.one_of(
    # automation-jira-<ISSUE_KEY>
    _VALID_ISSUE_KEY.map(automation_workflow_id_jira),
    # automation-bb-<ws>-<repo>-<pr_id>
    st.tuples(_VALID_SLUG, _VALID_SLUG, _PR_ID).map(
        lambda t: automation_workflow_id_bb(*t)
    ),
    # agent-<parent>-iter-<N>
    st.tuples(_PARENT_ID, _ITERATION).map(lambda t: agent_workflow_id(*t)),
    # exec-<parent>-<ts>
    st.tuples(_PARENT_ID, _TIMESTAMP).map(lambda t: execution_workflow_id(*t)),
    # plus a free-form fallback so the property doesn't accidentally
    # over-constrain to the four canonical formats.  The helper itself
    # treats workflow_id as an opaque string (Temporal's contract).
    st.text(
        alphabet=st.sampled_from(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_"
        ),
        min_size=1,
        max_size=80,
    ),
)


# ---------------------------------------------------------------------------
# Mock Temporal client
# ---------------------------------------------------------------------------


class _MockTemporalClient:
    """In-memory stand-in for :class:`temporalio.client.Client`.

    Tracks which ``workflow_id`` values are "running" and raises
    :class:`temporalio.exceptions.WorkflowAlreadyStartedError` on any
    second start with a known id - exactly mirroring the real SDK's
    contract for the duplicate-start case.
    """

    def __init__(self) -> None:
        self._running: set[str] = set()
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        wf_id = kwargs["id"]
        wf_type = args[0] if args else kwargs.get("workflow", "")
        if wf_id in self._running:
            raise WorkflowAlreadyStartedError(
                workflow_id=wf_id,
                workflow_type=wf_type,
                run_id="existing-run",
            )
        self._running.add(wf_id)
        return f"handle:{wf_id}"


# ---------------------------------------------------------------------------
# First start produces was_existing=False; second start
# with the same id produces was_existing=True with the same execution_id.
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(workflow_id=_WORKFLOW_ID_STRATEGY)
@pytest.mark.asyncio
async def test_second_start_with_same_id_is_idempotent(workflow_id: str) -> None:
    """Two consecutive starts with the same ``workflow_id`` produce
    exactly one execution.  The second call returns
    ``was_existing=True`` and echoes the caller-supplied id.
    """
    client = _MockTemporalClient()

    first = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        workflow_id,
        [],
        task_queue="automation-tq",
    )
    second = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        workflow_id,
        [],
        task_queue="automation-tq",
    )

    # Exactly one execution was registered with the mock.
    assert len(client._running) == 1
    assert workflow_id in client._running

    # First call: fresh start.
    assert first == StartResult(execution_id=workflow_id, was_existing=False)

    # Second call: duplicate, returns the same id.
    assert second == StartResult(execution_id=workflow_id, was_existing=True)
    assert second.execution_id == first.execution_id


# ---------------------------------------------------------------------------
# Three+ consecutive starts with the same id all collapse
# onto the same single execution; only the first call sets
# was_existing=False.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    workflow_id=_WORKFLOW_ID_STRATEGY,
    extra_starts=st.integers(min_value=1, max_value=5),
)
@pytest.mark.asyncio
async def test_n_starts_with_same_id_yield_single_execution(
    workflow_id: str, extra_starts: int
) -> None:
    """Repeated starts (1 + N) with the same id always yield exactly one
    execution, and every start after the first reports was_existing=True.
    """
    client = _MockTemporalClient()

    first = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        workflow_id,
        [],
        task_queue="automation-tq",
    )
    assert first.was_existing is False
    assert first.execution_id == workflow_id

    for _ in range(extra_starts):
        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            workflow_id,
            [],
            task_queue="automation-tq",
        )
        assert result.was_existing is True
        assert result.execution_id == workflow_id

    # Single execution end-state regardless of how many duplicate
    # starts were attempted.
    assert len(client._running) == 1


# ---------------------------------------------------------------------------
# Distinct workflow_ids produce distinct executions -
# idempotency is keyed strictly on the workflow_id, never collapsing
# unrelated workflows.
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    workflow_id_a=_WORKFLOW_ID_STRATEGY,
    workflow_id_b=_WORKFLOW_ID_STRATEGY,
)
@pytest.mark.asyncio
async def test_distinct_ids_yield_distinct_executions(
    workflow_id_a: str, workflow_id_b: str
) -> None:
    """Different workflow_ids must always produce separate executions -
    the idempotency rule keys on workflow_id and nothing else.
    """
    client = _MockTemporalClient()

    res_a = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        workflow_id_a,
        [],
        task_queue="automation-tq",
    )
    res_b = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        workflow_id_b,
        [],
        task_queue="automation-tq",
    )

    if workflow_id_a == workflow_id_b:
        # Same id  idempotent collapse.
        assert res_a.was_existing is False
        assert res_b.was_existing is True
        assert len(client._running) == 1
    else:
        # Different ids  two separate executions.
        assert res_a.was_existing is False
        assert res_b.was_existing is False
        assert res_a.execution_id != res_b.execution_id
        assert len(client._running) == 2


# ---------------------------------------------------------------------------
# The helper returns the caller-supplied workflow_id even
# when the SDK exception happens to carry a different id - pinning the
# contract that the response id is provable from inputs alone.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    caller_id=_WORKFLOW_ID_STRATEGY,
    sdk_id=_WORKFLOW_ID_STRATEGY,
)
@pytest.mark.asyncio
async def test_helper_echoes_caller_supplied_id_on_duplicate(
    caller_id: str, sdk_id: str
) -> None:
    """On a duplicate start the helper returns the *caller's* workflow_id
    even if the SDK exception happens to carry a different value.
    This locks the HTTP 202 response contract: the id returned to the
    webhook caller is always the id they supplied.
    """

    class _AlwaysDuplicateClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise WorkflowAlreadyStartedError(
                workflow_id=sdk_id,
                workflow_type=args[0] if args else "",
                run_id="run-from-sdk",
            )

    client = _AlwaysDuplicateClient()
    result = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        caller_id,
        [],
        task_queue="automation-tq",
    )

    assert result.was_existing is True
    # Even when sdk_id != caller_id, the helper returns caller_id.
    assert result.execution_id == caller_id


# =============================================================================
# workflow_id format ve round-trip parse
#
# For any ``(project_key, issue_num)`` Jira tuple,
# ``parse_workflow_id(jira_workflow_id(project_key, issue_num))`` must
# return ``WorkflowIdRef(provider="jira", project_key=project_key,
# issue_num=issue_num)``; the analogous round-trip holds for any
# ``(repo_slug, pr_id)`` Bitbucket tuple.  Every formatted string matches
# **exactly one** of the two pinned regexes:
#
# ^automation-jira-[A-Z][A-Z0-9_]{1,9}-\d+$
# ^automation-bb-[a-z0-9-]+-pr-\d+$
#
# Two distinct inputs never produce the same workflow_id (injectivity is
# preserved across the two namespaces, which are disjoint by prefix).
#
# These properties extend the foundation-parity surface above with the
# new formatters (``jira_workflow_id``,
# ``bitbucket_pr_workflow_id``, ``parse_workflow_id``,
# :class:`WorkflowIdRef`).
# =============================================================================


from temporal_shared.identifiers import (
    InvalidWorkflowIdError,
    WorkflowIdRef,
    bitbucket_pr_workflow_id,
    jira_workflow_id,
    parse_workflow_id,
)

# ---------------------------------------------------------------------------
# Pinned workflow-id regexes
# ---------------------------------------------------------------------------

_JIRA_WF_FMT_RE = re.compile(r"^automation-jira-[A-Z][A-Z0-9_]{1,9}-\d+$")
_BB_WF_FMT_RE = re.compile(r"^automation-bb-[a-z0-9-]+-pr-\d+$")


# ---------------------------------------------------------------------------
# Strategies tailored to the workflows-spec formatters
#
# ``jira_workflow_id`` constrains project_key to ``^[A-Z][A-Z0-9_]{1,9}$``
# (i.e. 2..10 characters), so the strategy mirrors that bound.  The
# Bitbucket formatter accepts any slug matching
# ``^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`` with no consecutive dashes; the
# strategy generates such slugs directly to maximise example density
# while staying inside the accepted input space.
# ---------------------------------------------------------------------------

_PROJECT_KEY_FIRST = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_PROJECT_KEY_REST = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"),
    min_size=1,
    max_size=9,
)
_PROJECT_KEY = st.builds(lambda f, r: f + r, _PROJECT_KEY_FIRST, _PROJECT_KEY_REST)

_ISSUE_NUM = st.integers(min_value=1, max_value=10**9)

_REPO_SLUG_CHAR = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789")
_REPO_SLUG_MID_CHAR = st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-")


def _build_repo_slug(first: str, middle: str, last: str) -> str:
    """Compose a slug that always satisfies the formatter's preconditions.

    The slug is anchored on alnum boundary characters and the middle
    body is post-processed to collapse any accidental ``--`` sequence
    into a single dash.  This keeps Hypothesis exploring the full slug
    shape while never producing a slug that the formatter would reject,
    so the round-trip property tests stay focused on the invariant
    rather than burning shrinking budget on filtered-out examples.
    """
    body = first + middle + last
    while "--" in body:
        body = body.replace("--", "-")
    # The post-processing above can leave a trailing dash if ``middle``
    # ends in dashes followed by ``last`` after dedup; guard against it.
    if body.endswith("-"):
        body = body.rstrip("-") + last
    return body


_REPO_SLUG = st.one_of(
    _REPO_SLUG_CHAR,  # single-char slug (allowed: ``a``, ``9``, etc.)
    st.builds(
        _build_repo_slug,
        _REPO_SLUG_CHAR,
        st.text(alphabet=_REPO_SLUG_MID_CHAR, min_size=0, max_size=30),
        _REPO_SLUG_CHAR,
    ),
)

_PR_ID = st.integers(min_value=1, max_value=10**9)


# ---------------------------------------------------------------------------
# Format regex match - Jira
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(project_key=_PROJECT_KEY, issue_num=_ISSUE_NUM)
def test_jira_workflow_id_matches_documented_regex(
    project_key: str, issue_num: int
) -> None:
    """Every formatted Jira workflow_id matches exactly the pinned regex
    ``^automation-jira-[A-Z][A-Z0-9_]{1,9}-\\d+$`` and does **not** match
    the Bitbucket regex.
    """
    wf_id = jira_workflow_id(project_key, issue_num)
    assert _JIRA_WF_FMT_RE.fullmatch(wf_id), (
        f"id {wf_id!r} fails Jira regex {_JIRA_WF_FMT_RE.pattern}"
    )
    assert not _BB_WF_FMT_RE.fullmatch(wf_id), (
        f"id {wf_id!r} unexpectedly matches Bitbucket regex"
    )


# ---------------------------------------------------------------------------
# Format regex match - Bitbucket
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(repo_slug=_REPO_SLUG, pr_id=_PR_ID)
def test_bitbucket_pr_workflow_id_matches_documented_regex(
    repo_slug: str, pr_id: int
) -> None:
    """Every formatted Bitbucket workflow_id matches exactly the pinned
    regex ``^automation-bb-[a-z0-9-]+-pr-\\d+$`` and does **not** match
    the Jira regex.
    """
    wf_id = bitbucket_pr_workflow_id(repo_slug, pr_id)
    assert _BB_WF_FMT_RE.fullmatch(wf_id), (
        f"id {wf_id!r} fails Bitbucket regex {_BB_WF_FMT_RE.pattern}"
    )
    assert not _JIRA_WF_FMT_RE.fullmatch(wf_id), (
        f"id {wf_id!r} unexpectedly matches Jira regex"
    )


# ---------------------------------------------------------------------------
# Round-trip - Jira
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(project_key=_PROJECT_KEY, issue_num=_ISSUE_NUM)
def test_jira_workflow_id_round_trip(project_key: str, issue_num: int) -> None:
    """``parse_workflow_id(jira_workflow_id(pk, n))`` returns the original
    ``(pk, n)`` tuple wrapped in a ``WorkflowIdRef`` with provider
    ``"jira"`` and Bitbucket fields cleared to ``None``.
    """
    wf_id = jira_workflow_id(project_key, issue_num)
    ref = parse_workflow_id(wf_id)
    assert ref == WorkflowIdRef(
        provider="jira",
        project_key=project_key,
        issue_num=issue_num,
    )
    # And the inverse: re-formatting the parsed ref reproduces the id.
    assert (
        jira_workflow_id(ref.project_key, ref.issue_num)  # type: ignore[arg-type]
        == wf_id
    )


# ---------------------------------------------------------------------------
# Round-trip - Bitbucket
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(repo_slug=_REPO_SLUG, pr_id=_PR_ID)
def test_bitbucket_pr_workflow_id_round_trip(repo_slug: str, pr_id: int) -> None:
    """``parse_workflow_id(bitbucket_pr_workflow_id(slug, pr))`` returns
    the original ``(slug, pr)`` tuple wrapped in a ``WorkflowIdRef``
    with provider ``"bitbucket"`` and Jira fields cleared to ``None``.
    """
    wf_id = bitbucket_pr_workflow_id(repo_slug, pr_id)
    ref = parse_workflow_id(wf_id)
    assert ref == WorkflowIdRef(
        provider="bitbucket",
        repo_slug=repo_slug,
        pr_id=pr_id,
    )
    assert (
        bitbucket_pr_workflow_id(ref.repo_slug, ref.pr_id)  # type: ignore[arg-type]
        == wf_id
    )


# ---------------------------------------------------------------------------
# Injectivity - Jira (no two distinct inputs collide)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    pk_a=_PROJECT_KEY,
    num_a=_ISSUE_NUM,
    pk_b=_PROJECT_KEY,
    num_b=_ISSUE_NUM,
)
def test_jira_workflow_id_injective(
    pk_a: str, num_a: int, pk_b: str, num_b: int
) -> None:
    """Two distinct ``(project_key, issue_num)`` tuples never produce the
    same formatted Jira workflow_id.
    """
    if (pk_a, num_a) != (pk_b, num_b):
        assert jira_workflow_id(pk_a, num_a) != jira_workflow_id(pk_b, num_b)


# ---------------------------------------------------------------------------
# Injectivity - Bitbucket (no two distinct inputs collide)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    slug_a=_REPO_SLUG,
    pr_a=_PR_ID,
    slug_b=_REPO_SLUG,
    pr_b=_PR_ID,
)
def test_bitbucket_pr_workflow_id_injective(
    slug_a: str, pr_a: int, slug_b: str, pr_b: int
) -> None:
    """Two distinct ``(repo_slug, pr_id)`` tuples never produce the same
    formatted Bitbucket workflow_id.  The ``-pr-`` literal infix is
    what disambiguates the slug from the pr_id even when the slug
    happens to contain trailing digits.
    """
    if (slug_a, pr_a) != (slug_b, pr_b):
        assert (
            bitbucket_pr_workflow_id(slug_a, pr_a)
            != bitbucket_pr_workflow_id(slug_b, pr_b)
        )


# ---------------------------------------------------------------------------
# Cross-namespace injectivity
#
# A Jira-formatted id and a Bitbucket-formatted id are always distinct,
# regardless of inputs - the prefix alone (``automation-jira-`` vs
# ``automation-bb-``) keeps the namespaces disjoint, and parsing one
# never yields the other's provider.
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    project_key=_PROJECT_KEY,
    issue_num=_ISSUE_NUM,
    repo_slug=_REPO_SLUG,
    pr_id=_PR_ID,
)
def test_jira_and_bitbucket_namespaces_disjoint(
    project_key: str, issue_num: int, repo_slug: str, pr_id: int
) -> None:
    """No Jira-formatted id collides with any Bitbucket-formatted id, and
    each id parses back to the correct provider.
    """
    jira_id = jira_workflow_id(project_key, issue_num)
    bb_id = bitbucket_pr_workflow_id(repo_slug, pr_id)

    assert jira_id != bb_id
    assert parse_workflow_id(jira_id).provider == "jira"
    assert parse_workflow_id(bb_id).provider == "bitbucket"


# ---------------------------------------------------------------------------
# Strings outside both regexes are rejected
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    junk=st.text(
        alphabet=st.sampled_from(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789-_:/. "
        ),
        min_size=0,
        max_size=80,
    )
)
def test_parse_rejects_strings_outside_both_regexes(junk: str) -> None:
    """Any string that does not match either documented regex must raise
    :class:`InvalidWorkflowIdError`.  Conversely, any string that *does*
    match one of the regexes must parse cleanly (modulo the additional
    structural checks the parser layers on top, e.g. forbidding
    leading-zero numerics).
    """
    if _JIRA_WF_FMT_RE.fullmatch(junk) or _BB_WF_FMT_RE.fullmatch(junk):
        # Inside the regex space - parsing must either succeed or raise
        # only because of the layered structural rules (leading zeros,
        # double dashes in the slug, etc.).
        try:
            ref = parse_workflow_id(junk)
        except InvalidWorkflowIdError:
            return
        assert ref.provider in {"jira", "bitbucket"}
    else:
        # Outside both regexes - parser must reject.
        with pytest.raises(InvalidWorkflowIdError):
            parse_workflow_id(junk)
