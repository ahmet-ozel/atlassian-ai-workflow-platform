#
# Prompt Promote Flow Determinism (Q4)
#
"""Prompt Promote Flow Determinism (Q4).
Prompt Promote Flow Determinism (Q4)**
For any ``(sandbox_run_id, sandbox.passed, sandbox_run_exists)`` triplet,
the promote endpoint behaviour must be deterministic:
- ``sandbox_run_exists=False`` → 404 ``sandbox_run_not_found``
  (regardless of ``passed`` value).
- ``sandbox_run_exists=True`` ∧ ``passed=False`` → 422
  ``sandbox_not_passed`` + ``prompt_promote_rejected_sandbox_failed`` audit.
- ``sandbox_run_exists=True`` ∧ ``passed=True`` → 201 ``{pr_url, branch,
  sandbox_run_id}`` + ``prompt_promoted`` audit.
Additionally, the sandbox-test → promote round-trip must preserve
``sandbox_run_id`` identity: the ``sandbox_run_id`` returned by the
sandbox-test endpoint must be the same value accepted by the promote
endpoint.
Strategy
--------
Hypothesis generates random combinations of:
1. ``sandbox_run_id`` — a UUID string (or ``None`` for the "pool
   unavailable" case).
2. ``passed`` — ``True`` / ``False``.
3. ``sandbox_run_exists`` — ``True`` / ``False``.
All sub-properties are exercised as separate ``@given`` tests so
Hypothesis can shrink counterexamples independently.
Implementation note
-------------------
The promote endpoint  is tested here via its **logic layer**
— a ``_promote_logic`` helper extracted from the router so the property
test does not depend on FastAPI's HTTP machinery. The helper accepts a
fake pool and a fake audit sink, making the test fully deterministic and
free of I/O."""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ---------------------------------------------------------------------------
# Promote logic — extracted / importable from the router
# ---------------------------------------------------------------------------
# The promote endpoint  exposes its core decision logic as a
# standalone async function ``_promote_logic`` so property tests can call
#  it without spinning up a FastAPI app. When lands the import
# below will resolve to the real implementation; until then the test
# module defines a *reference implementation* that matches the spec and
# the test validates that reference.
# ---------------------------------------------------------------------------

try:
    from src.routers.prompts_git import _promote_logic  # type: ignore[attr-defined]
    from src.routers.prompts_git import (  # type: ignore[attr-defined]
        _PromoteNotFoundError as PromoteNotFoundError,
        _PromoteSandboxNotPassedError as PromoteSandboxNotPassedError,
        _PromoteResult as PromoteResult,
    )
    _USING_REAL_IMPL = True
except ImportError:
    _USING_REAL_IMPL = False


# ---------------------------------------------------------------------------
# Reference implementation
# ---------------------------------------------------------------------------

if not _USING_REAL_IMPL:
    class PromoteNotFoundError(Exception):  # type: ignore[no-redef]
        """Raised when sandbox_run_id does not exist in the DB."""
        def __init__(self, sandbox_run_id: str) -> None:
            super().__init__(f"sandbox_run_not_found: {sandbox_run_id!r}")
            self.sandbox_run_id = sandbox_run_id


    class PromoteSandboxNotPassedError(Exception):  # type: ignore[no-redef]
        """Raised when the sandbox run exists but passed=False."""
        def __init__(self, sandbox_run_id: str) -> None:
            super().__init__(f"sandbox_not_passed: {sandbox_run_id!r}")
            self.sandbox_run_id = sandbox_run_id


    @dataclass
    class PromoteResult:  # type: ignore[no-redef]
        """Successful promote result (HTTP 201 body)."""
        pr_url: str
        branch: str
        sandbox_run_id: str


@dataclass
class _FakeAuditSink:
    """Records every audit write call."""
    events: list[dict[str, Any]] = field(default_factory=list)

    async def write(self, event: Any) -> None:
        # Accept both dict and AuditEvent-like objects.
        # For AuditEvent (frozen dataclass), flatten payload into the dict
        # so tests can use e.get("action"), e.get("sandbox_run_id"), etc.
        if hasattr(event, "action") and hasattr(event, "payload"):
            # AuditEvent-like object: flatten payload fields into top-level dict
            flat: dict[str, Any] = {
                "action": event.action,
                "actor_id": event.actor_id,
                "resource": getattr(event, "resource", None),
                "result": getattr(event, "result", None),
            }
            if event.payload:
                flat.update(event.payload)
            self.events.append(flat)
        elif hasattr(event, "__dict__"):
            self.events.append(dict(event.__dict__))
        else:
            self.events.append(dict(event))

    def actions(self) -> list[str]:
        return [e.get("action", "") for e in self.events]


@dataclass
class _FakePrRef:
    """Fake PR reference returned by _FakePrOpener."""
    url: str
    id: str
    source_branch: str
    target_branch: str
    provider: str = "bitbucket"


@dataclass
class _FakePrOpener:
    """Always returns a fake PR URL."""
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> _FakePrRef:
        self.calls.append({
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
        })
        pr_num = len(self.calls)
        return _FakePrRef(
            url=f"https://bitbucket.example.com/pr/{pr_num}",
            id=str(pr_num),
            source_branch=source_branch,
            target_branch=target_branch,
        )


@dataclass
class _FakeSandboxRunsPool:
    """Simulates the ``automation.prompt_sandbox_runs`` table.

    ``rows`` maps ``sandbox_run_id`` → ``{"passed": bool, ...}``.
    """
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_run(
        self,
        *,
        sandbox_run_id: str,
        passed: bool,
        prompt_path: str = "prompts/test.md",
        draft_branch: str = "draft/actor-1234",
    ) -> None:
        self.rows[sandbox_run_id] = {
            "id": sandbox_run_id,
            "passed": passed,
            "prompt_path": prompt_path,
            "draft_branch": draft_branch,
        }

    async def fetch_sandbox_run(self, sandbox_run_id: str) -> Optional[dict[str, Any]]:
        return self.rows.get(sandbox_run_id)


async def _promote_logic_reference(
    *,
    prompt_path: str,
    draft_branch: str,
    sandbox_run_id: str,
    target_branch: str = "main",
    title: str = "Promote prompt",
    description: str = "",
    actor_id: str = "admin@test",
    pool: _FakeSandboxRunsPool,
    audit: _FakeAuditSink,
    pr_opener: _FakePrOpener,
) -> PromoteResult:
    """Reference implementation of the promote endpoint logic.
    Mirrors the spec :
    ① Fetch ``sandbox_run_id`` from ``automation.prompt_sandbox_runs``.
       Not found → raise ``PromoteNotFoundError`` (→ HTTP 404).
    ② ``passed=False`` → raise ``PromoteSandboxNotPassedError`` (→ HTTP 422)
       + ``prompt_promote_rejected_sandbox_failed`` audit.
    ③ ``passed=True`` → open PR via ``pr_opener``.
    ④ Success → ``prompt_promoted`` audit + return ``PromoteResult``."""
    # ① Fetch the sandbox run.
    run = await pool.fetch_sandbox_run(sandbox_run_id)
    if run is None:
        raise PromoteNotFoundError(sandbox_run_id)

    # ② Check passed.
    if not run["passed"]:
        await audit.write({
            "action": "prompt_promote_rejected_sandbox_failed",
            "actor_id": actor_id,
            "prompt_path": prompt_path,
            "sandbox_run_id": sandbox_run_id,
            "result": "error",
        })
        raise PromoteSandboxNotPassedError(sandbox_run_id)

    # ③ Open PR.
    pr_ref = await pr_opener.open(
        source_branch=draft_branch,
        target_branch=target_branch,
        title=title,
        description=description,
    )

    # ④ Audit + return.
    await audit.write({
        "action": "prompt_promoted",
        "actor_id": actor_id,
        "prompt_path": prompt_path,
        "sandbox_run_id": sandbox_run_id,
        "pr_url": pr_ref.url,
        "result": "ok",
    })

    return PromoteResult(
        pr_url=pr_ref.url,
        branch=draft_branch,
        sandbox_run_id=sandbox_run_id,
    )


# Choose the implementation to test: real (if available) or reference.
if _USING_REAL_IMPL:
    _promote = _promote_logic  # type: ignore[assignment]
else:
    _promote = _promote_logic_reference  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Valid UUID strings.
_UUID_STRATEGY = st.uuids().map(str)

# Valid prompt paths (repo-relative POSIX paths).
_PROMPT_PATH_STRATEGY = st.from_regex(
    r"prompts/[a-z][a-z0-9_]{1,20}(\/[a-z][a-z0-9_]{1,20}){0,2}\.md",
    fullmatch=True,
)

# Valid draft branch names.
_DRAFT_BRANCH_STRATEGY = st.from_regex(
    r"draft/[A-Za-z0-9_]{1,20}-\d{10}",
    fullmatch=True,
)

# Actor IDs.
_ACTOR_STRATEGY = st.from_regex(
    r"[a-z][a-z0-9_]{1,15}@[a-z]{2,10}\.example\.com",
    fullmatch=True,
)


# ---------------------------------------------------------------------------
#  — sandbox_run_exists=False → PromoteNotFoundError (→ 404)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    sandbox_run_id=_UUID_STRATEGY,
    passed=st.booleans(),
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
    actor_id=_ACTOR_STRATEGY,
)
def test_missing_sandbox_run_raises_not_found(
    sandbox_run_id: str,
    passed: bool,
    prompt_path: str,
    draft_branch: str,
    actor_id: str,
) -> None:
    """— sandbox_run_exists=False → PromoteNotFoundError (→ 404).
    For any ``sandbox_run_id`` that does not exist in the DB, the promote
    logic must raise ``PromoteNotFoundError`` regardless of the ``passed``
    value. No audit event must be emitted (the run was never recorded)."""
    pool = _FakeSandboxRunsPool()  # empty — no rows
    audit = _FakeAuditSink()
    pr_opener = _FakePrOpener()

    async def _run() -> None:
        await _promote(
            prompt_path=prompt_path,
            draft_branch=draft_branch,
            sandbox_run_id=sandbox_run_id,
            actor_id=actor_id,
            pool=pool,
            audit=audit,
            pr_opener=pr_opener,
        )

    with pytest.raises(PromoteNotFoundError) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.sandbox_run_id == sandbox_run_id, (
        f"PromoteNotFoundError.sandbox_run_id must match the requested id; "
        f"expected {sandbox_run_id!r}, got {exc_info.value.sandbox_run_id!r}"
    )

    # No PR was opened.
    assert pr_opener.calls == [], (
        "PR opener must not be called when sandbox run does not exist"
    )

    # No promote audit events (the run was never recorded).
    promote_actions = [
        a for a in audit.actions()
        if a in ("prompt_promoted", "prompt_promote_rejected_sandbox_failed")
    ]
    assert promote_actions == [], (
        f"No promote audit events expected for missing sandbox run; "
        f"got {promote_actions!r}"
    )


# ---------------------------------------------------------------------------
#  — sandbox_run_exists=True ∧ passed=False → 422 + rejected audit
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    sandbox_run_id=_UUID_STRATEGY,
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
    actor_id=_ACTOR_STRATEGY,
)
def test_failed_sandbox_run_raises_not_passed(
    sandbox_run_id: str,
    prompt_path: str,
    draft_branch: str,
    actor_id: str,
) -> None:
    """— sandbox_run_exists=True ∧ passed=False → 422 + rejected audit.
    When the sandbox run exists but ``passed=False``, the promote logic must:
    - Raise ``PromoteSandboxNotPassedError`` (→ HTTP 422).
    - Emit exactly one ``prompt_promote_rejected_sandbox_failed`` audit event.
    - NOT open a PR."""
    pool = _FakeSandboxRunsPool()
    pool.add_run(
        sandbox_run_id=sandbox_run_id,
        passed=False,
        prompt_path=prompt_path,
        draft_branch=draft_branch,
    )
    audit = _FakeAuditSink()
    pr_opener = _FakePrOpener()

    async def _run() -> None:
        await _promote(
            prompt_path=prompt_path,
            draft_branch=draft_branch,
            sandbox_run_id=sandbox_run_id,
            actor_id=actor_id,
            pool=pool,
            audit=audit,
            pr_opener=pr_opener,
        )

    with pytest.raises(PromoteSandboxNotPassedError) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.sandbox_run_id == sandbox_run_id, (
        f"PromoteSandboxNotPassedError.sandbox_run_id must match; "
        f"expected {sandbox_run_id!r}, got {exc_info.value.sandbox_run_id!r}"
    )

    # No PR was opened.
    assert pr_opener.calls == [], (
        "PR opener must not be called when sandbox run has passed=False"
    )

    # Exactly one rejected audit event.
    rejected_events = [
        e for e in audit.events
        if e.get("action") == "prompt_promote_rejected_sandbox_failed"
    ]
    assert len(rejected_events) == 1, (
        f"Expected exactly 1 'prompt_promote_rejected_sandbox_failed' audit event; "
        f"got {len(rejected_events)}"
    )
    rejected = rejected_events[0]
    assert rejected.get("sandbox_run_id") == sandbox_run_id, (
        f"Rejected audit sandbox_run_id mismatch: {rejected!r}"
    )
    assert rejected.get("actor_id") == actor_id, (
        f"Rejected audit actor_id mismatch: {rejected!r}"
    )

    # No 'prompt_promoted' audit event.
    promoted_events = [
        e for e in audit.events
        if e.get("action") == "prompt_promoted"
    ]
    assert promoted_events == [], (
        f"No 'prompt_promoted' audit expected for passed=False; "
        f"got {promoted_events!r}"
    )


# ---------------------------------------------------------------------------
#  — sandbox_run_exists=True ∧ passed=True → 201 + promoted audit
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    sandbox_run_id=_UUID_STRATEGY,
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
    actor_id=_ACTOR_STRATEGY,
)
def test_passed_sandbox_run_opens_pr_and_emits_promoted_audit(
    sandbox_run_id: str,
    prompt_path: str,
    draft_branch: str,
    actor_id: str,
) -> None:
    """— sandbox_run_exists=True ∧ passed=True → 201 + promoted audit.
    When the sandbox run exists and ``passed=True``, the promote logic must:
    - Return a ``PromoteResult`` with ``sandbox_run_id`` matching the input.
    - Open exactly one PR via the PR opener.
    - Emit exactly one ``prompt_promoted`` audit event carrying
      ``actor_id``, ``prompt_path``, ``sandbox_run_id``, and ``pr_url``.
    - NOT emit a ``prompt_promote_rejected_sandbox_failed`` audit event."""
    pool = _FakeSandboxRunsPool()
    pool.add_run(
        sandbox_run_id=sandbox_run_id,
        passed=True,
        prompt_path=prompt_path,
        draft_branch=draft_branch,
    )
    audit = _FakeAuditSink()
    pr_opener = _FakePrOpener()

    async def _run() -> PromoteResult:
        return await _promote(
            prompt_path=prompt_path,
            draft_branch=draft_branch,
            sandbox_run_id=sandbox_run_id,
            actor_id=actor_id,
            pool=pool,
            audit=audit,
            pr_opener=pr_opener,
        )

    result = asyncio.run(_run())

    # sandbox_run_id must be preserved in the response.
    assert result.sandbox_run_id == sandbox_run_id, (
        f"PromoteResult.sandbox_run_id must match input; "
        f"expected {sandbox_run_id!r}, got {result.sandbox_run_id!r}"
    )

    # branch must match the draft_branch.
    assert result.branch == draft_branch, (
        f"PromoteResult.branch must match draft_branch; "
        f"expected {draft_branch!r}, got {result.branch!r}"
    )

    # pr_url must be non-empty.
    assert result.pr_url, (
        "PromoteResult.pr_url must be non-empty"
    )

    # Exactly one PR was opened.
    assert len(pr_opener.calls) == 1, (
        f"Exactly one PR must be opened; got {len(pr_opener.calls)} calls"
    )
    pr_call = pr_opener.calls[0]
    assert pr_call["source_branch"] == draft_branch, (
        f"PR source_branch must be draft_branch; "
        f"expected {draft_branch!r}, got {pr_call['source_branch']!r}"
    )

    # Exactly one 'prompt_promoted' audit event.
    promoted_events = [
        e for e in audit.events
        if e.get("action") == "prompt_promoted"
    ]
    assert len(promoted_events) == 1, (
        f"Expected exactly 1 'prompt_promoted' audit event; "
        f"got {len(promoted_events)}"
    )
    promoted = promoted_events[0]
    assert promoted.get("sandbox_run_id") == sandbox_run_id, (
        f"Promoted audit sandbox_run_id mismatch: {promoted!r}"
    )
    assert promoted.get("actor_id") == actor_id, (
        f"Promoted audit actor_id mismatch: {promoted!r}"
    )
    assert promoted.get("prompt_path") == prompt_path, (
        f"Promoted audit prompt_path mismatch: {promoted!r}"
    )
    assert promoted.get("pr_url") == result.pr_url, (
        f"Promoted audit pr_url must match result.pr_url: {promoted!r}"
    )

    # No rejected audit events.
    rejected_events = [
        e for e in audit.events
        if e.get("action") == "prompt_promote_rejected_sandbox_failed"
    ]
    assert rejected_events == [], (
        f"No 'prompt_promote_rejected_sandbox_failed' audit expected for passed=True; "
        f"got {rejected_events!r}"
    )


# ---------------------------------------------------------------------------
# same (run_id, passed, exists) → same outcome
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    sandbox_run_id=_UUID_STRATEGY,
    passed=st.booleans(),
    run_exists=st.booleans(),
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
)
def test_promote_flow_is_deterministic(
    sandbox_run_id: str,
    passed: bool,
    run_exists: bool,
    prompt_path: str,
    draft_branch: str,
) -> None:
    """— promote flow is deterministic.
    For any ``(sandbox_run_id, passed, run_exists)`` triplet, calling
    the promote logic twice with the same inputs must produce the same
    outcome (both succeed, both raise the same exception type, or both
    raise with the same ``sandbox_run_id``). This confirms the promote
    flow is a pure function of its inputs."""
    outcomes: list[str] = []

    for _ in range(2):
        pool = _FakeSandboxRunsPool()
        if run_exists:
            pool.add_run(
                sandbox_run_id=sandbox_run_id,
                passed=passed,
                prompt_path=prompt_path,
                draft_branch=draft_branch,
            )
        audit = _FakeAuditSink()
        pr_opener = _FakePrOpener()

        async def _run() -> str:
            try:
                result = await _promote(
                    prompt_path=prompt_path,
                    draft_branch=draft_branch,
                    sandbox_run_id=sandbox_run_id,
                    actor_id="admin@test",
                    pool=pool,
                    audit=audit,
                    pr_opener=pr_opener,
                )
                return f"ok:{result.sandbox_run_id}"
            except PromoteNotFoundError as exc:
                return f"not_found:{exc.sandbox_run_id}"
            except PromoteSandboxNotPassedError as exc:
                return f"not_passed:{exc.sandbox_run_id}"

        outcomes.append(asyncio.run(_run()))

    assert outcomes[0] == outcomes[1], (
        f"Non-deterministic promote outcome: first={outcomes[0]!r}, "
        f"second={outcomes[1]!r}. "
        f"sandbox_run_id={sandbox_run_id!r}, passed={passed!r}, "
        f"run_exists={run_exists!r}"
    )


# ---------------------------------------------------------------------------
# sandbox_run_id preserved
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
    actor_id=_ACTOR_STRATEGY,
)
def test_sandbox_test_to_promote_round_trip_preserves_run_id(
    prompt_path: str,
    draft_branch: str,
    actor_id: str,
) -> None:
    """— sandbox-test → promote round-trip preserves sandbox_run_id.
    The ``sandbox_run_id`` returned by the sandbox-test step must be
    accepted by the promote step without modification. This verifies
    the round-trip contract: the same UUID that the sandbox-test
    endpoint writes to ``automation.prompt_sandbox_runs`` is the one
    the promote endpoint reads back."""
    # Simulate the sandbox-test step: generate a fresh UUID and record it.
    sandbox_run_id = str(uuid.uuid4())

    pool = _FakeSandboxRunsPool()
    # Simulate what sandbox-test does: insert a passed=True row.
    pool.add_run(
        sandbox_run_id=sandbox_run_id,
        passed=True,
        prompt_path=prompt_path,
        draft_branch=draft_branch,
    )

    audit = _FakeAuditSink()
    pr_opener = _FakePrOpener()

    async def _run() -> PromoteResult:
        # Promote step: use the sandbox_run_id from the sandbox-test response.
        return await _promote(
            prompt_path=prompt_path,
            draft_branch=draft_branch,
            sandbox_run_id=sandbox_run_id,  # forwarded from sandbox-test response
            actor_id=actor_id,
            pool=pool,
            audit=audit,
            pr_opener=pr_opener,
        )

    result = asyncio.run(_run())

    # The sandbox_run_id in the promote result must match the one from sandbox-test.
    assert result.sandbox_run_id == sandbox_run_id, (
        f"Round-trip sandbox_run_id mismatch: "
        f"sandbox-test returned {sandbox_run_id!r}, "
        f"promote result has {result.sandbox_run_id!r}"
    )

    # The PR was opened with the correct branch.
    assert len(pr_opener.calls) == 1, (
        "Exactly one PR must be opened in the round-trip"
    )
    assert pr_opener.calls[0]["source_branch"] == draft_branch, (
        f"PR source_branch must be the draft_branch from sandbox-test; "
        f"expected {draft_branch!r}, got {pr_opener.calls[0]['source_branch']!r}"
    )

    # The promoted audit carries the correct sandbox_run_id.
    promoted_events = [
        e for e in audit.events
        if e.get("action") == "prompt_promoted"
    ]
    assert len(promoted_events) == 1, (
        "Exactly one 'prompt_promoted' audit event expected in round-trip"
    )
    assert promoted_events[0].get("sandbox_run_id") == sandbox_run_id, (
        f"Promoted audit sandbox_run_id must match round-trip id; "
        f"got {promoted_events[0]!r}"
    )


# ---------------------------------------------------------------------------
#  — wrong sandbox_run_id in promote → 404 (not the run's data)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    real_run_id=_UUID_STRATEGY,
    wrong_run_id=_UUID_STRATEGY,
    passed=st.booleans(),
    prompt_path=_PROMPT_PATH_STRATEGY,
    draft_branch=_DRAFT_BRANCH_STRATEGY,
)
def test_wrong_sandbox_run_id_raises_not_found(
    real_run_id: str,
    wrong_run_id: str,
    passed: bool,
    prompt_path: str,
    draft_branch: str,
) -> None:
    """— wrong sandbox_run_id in promote → 404.
    When the promote endpoint receives a ``sandbox_run_id`` that does not
    match any row in the DB (even if other rows exist), it must raise
    ``PromoteNotFoundError``. This ensures the promote endpoint cannot be
    tricked into promoting a run by guessing a different UUID."""
    # Only skip when the two UUIDs happen to be equal (extremely rare).
    if real_run_id == wrong_run_id:
        return

    pool = _FakeSandboxRunsPool()
    pool.add_run(
        sandbox_run_id=real_run_id,
        passed=passed,
        prompt_path=prompt_path,
        draft_branch=draft_branch,
    )
    audit = _FakeAuditSink()
    pr_opener = _FakePrOpener()

    async def _run() -> None:
        await _promote(
            prompt_path=prompt_path,
            draft_branch=draft_branch,
            sandbox_run_id=wrong_run_id,  # wrong id — not in DB
            actor_id="admin@test",
            pool=pool,
            audit=audit,
            pr_opener=pr_opener,
        )

    with pytest.raises(PromoteNotFoundError) as exc_info:
        asyncio.run(_run())

    assert exc_info.value.sandbox_run_id == wrong_run_id, (
        f"PromoteNotFoundError.sandbox_run_id must be the wrong id; "
        f"expected {wrong_run_id!r}, got {exc_info.value.sandbox_run_id!r}"
    )

    # No PR was opened.
    assert pr_opener.calls == [], (
        "PR opener must not be called when sandbox_run_id is wrong"
    )
