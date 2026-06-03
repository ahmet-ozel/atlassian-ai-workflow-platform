"""``PromptSandbox`` — isolated LLM invocation for prompt drafts.

The sandbox performs one isolated LLM call for a prompt draft: sample
input plus draft prompt produces an LLM response, does not affect
production workflows, and records cost with the ``sandbox`` tag.

The sandbox is the **single isolation point** between the prompt
editor (admin-dashboard ``/prompts``) and the live LLM. Three
invariants matter:

1. **No production tools.** The sandbox does *not* hand the LLM a
   tool catalogue — it issues a flat ``system`` + ``user`` prompt and
   returns the raw text response. Production assistant-service writes
   to Atlassian via banned-tool-filtered MCP calls; the sandbox runs
   without any tool surface so a draft prompt cannot accidentally
   open a Bitbucket PR or post a Confluence page.
2. **Cost tagged ``"sandbox"``.** Every LLM round-trip in the
   sandbox is recorded with ``cost_tag="sandbox"`` against
   ``shared.cost_tracking``. ``BudgetCapPolicy`` filters
   on ``cost_tag = 'production'`` so sandbox usage never eats into a
   department's weekly / monthly cap.
3. **Deterministic ``SandboxResult``.** The return value is a frozen
   dataclass carrying ``response_text``, ``token_in``, ``token_out``,
   ``cost_usd`` and ``invoked_at``. Callers (the
   ``PromptsGitRouter``'s sandbox-test endpoint, the future PR
   description renderer) consume this single shape.

The collaborators are typed against small :class:`~typing.Protocol`
interfaces so the sandbox can be exercised without standing up
assistant-service or Postgres. Production wiring lives in
``src/main.py``'s lifespan context and uses the configured LLM
provider; isolated tests can inject :class:`SyntheticLlmInvoker` with
the :class:`NullCostTracker`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Final, Protocol, runtime_checkable

__all__ = [
    "COST_TAG_SANDBOX",
    "CostEntryLike",
    "CostTrackerLike",
    "LlmInvocationResult",
    "LlmInvokerLike",
    "NullCostTracker",
    "ProviderLlmInvoker",
    "PromptSandbox",
    "SandboxResult",
    "SyntheticLlmInvoker",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sandbox cost tag — module-level constant
# ---------------------------------------------------------------------------


#: Cost tag written to ``shared.cost_tracking`` for every sandbox
#: invocation. Mirrors the ``cost_tag`` ``CHECK`` constraint in
#: ``platform/infra/postgres/20_ops.sql`` and the literal that
#: :class:`BudgetCapPolicy` filters out of production budget queries
#: (``cost_tag IN ('sandbox','probe')`` is excluded). The value is
#: pinned as a module-level :data:`Final` so accidental drift between
#: writer and policy is impossible.
COST_TAG_SANDBOX: Final[str] = "sandbox"


# ---------------------------------------------------------------------------
# Public value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LlmInvocationResult:
    """Return value of :class:`LlmInvokerLike.invoke`.

    The four fields are the **minimum** every LLM provider exposes
    after a call: the textual response, the input/output token counts
    and the resulting cost in USD. Concrete invokers (vLLM, OpenAI,
    Anthropic) populate all four; synthetic test invokers can provide
    deterministic placeholders without network access.

    ``cost_usd`` is a :class:`~decimal.Decimal` to match the
    ``NUMERIC(12, 6)`` column on ``shared.cost_tracking``; using
    floats here would silently lose precision when the sandbox-test
    runs against expensive frontier models.
    """

    response_text: str
    token_in: int
    token_out: int
    cost_usd: Decimal
    model: str = "unknown"
    provider: str = "unknown"


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Frozen result returned by :meth:`PromptSandbox.run`.

    Carries ``response_text``, ``token_in``, ``token_out``,
    ``cost_usd`` and ``invoked_at``.

    The callers that consume this shape:

    * The ``POST /admin/prompts/{path}/sandbox-test`` HTTP handler in
      :mod:`src.routers.prompts_git`, which serialises it to JSON
      for the admin UI.
    * The PR description renderer, which embeds the last N sandbox results as a Markdown
      table.
    """

    response_text: str
    token_in: int
    token_out: int
    cost_usd: Decimal
    invoked_at: datetime
    model: str = "unknown"
    provider: str = "unknown"
    # ``cost_tag`` is fixed at construction time — exposing it on the
    # response makes the isolation contract auditable from a single
    # JSON envelope without re-reading the source.
    cost_tag: str = COST_TAG_SANDBOX


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmInvokerLike(Protocol):
    """Provider-agnostic single-shot invoker.

    The sandbox does **not** stream — it issues one request, waits
    for the full response, records its cost and returns. This keeps
    the collaborator surface tiny: ``LlmOrchestrator.stream_with_tool_loop``
    and this sandbox are deliberately separate code paths because their
    failure modes differ — the streaming chat loop has retry +
    fallback + token cap, while the sandbox never retries (a sandbox
    test that fails should surface the error to the developer
    immediately).

    Production wiring will adapt
    :class:`assistant_service.llm.LlmOrchestrator` into
    an :class:`LlmInvokerLike` by collapsing its async-generator
    surface into a single coroutine; the ``cost_tag`` keyword is
    forwarded to the provider so the cost record carries the
    sandbox tag.
    """

    async def invoke(
        self,
        *,
        system: str,
        user: str,
        cost_tag: str,
    ) -> LlmInvocationResult:  # pragma: no cover - protocol
        """Run a single ``(system, user)`` round-trip.

        Args:
            system: Draft prompt body (the candidate the user is
                testing in the sandbox).
            user: Sample input the developer typed into the
                sandbox-test form.
            cost_tag: Always ``"sandbox"`` when called by the
                sandbox; pass-through for the cost layer.
        """


@dataclass(frozen=True, slots=True)
class CostEntryLike:
    """Minimal payload accepted by :class:`CostTrackerLike.record`.

    Mirrors the columns of ``shared.cost_tracking``
    (``platform/infra/postgres/20_ops.sql``). Held here as a
    package-local dataclass so the sandbox does not have to import
    from a cost-tracking package — the
    real ``cost_tracking.CostEntry`` will share the same field names
    so the swap is mechanical.

    Attributes:
        activity_id: Globally unique identifier for the LLM round
            trip; the ``UNIQUE`` constraint on
            ``shared.cost_tracking.activity_id`` makes the insert
            idempotent.
        dept_id: Department the prompt belongs to. Sandbox calls do
            not reduce a dept's budget, but the row is still tagged
            with the originating dept so the costs panel can
            attribute sandbox spend to the right team.
        user_id: Optional caller id. The sandbox-test endpoint
            populates this from the OIDC ``sub`` claim so
            per-developer sandbox usage can be reported.
        workflow_id: Always ``None`` for sandbox runs — the sandbox
            does not start a Temporal workflow.
        model: LLM model identifier (eg. ``"qwen2.5-coder"``).
        provider: One of ``"vllm"`` / ``"openai"`` / ``"anthropic"``.
        token_in: Prompt token count.
        token_out: Completion token count.
        cost_usd: Cost in USD as :class:`Decimal`.
        cost_tag: Always :data:`COST_TAG_SANDBOX` for sandbox runs;
            kept on the entry so the writer cannot omit it.
    """

    activity_id: str
    dept_id: str | None
    user_id: str | None
    workflow_id: str | None
    model: str
    provider: str
    token_in: int
    token_out: int
    cost_usd: Decimal
    cost_tag: str


@runtime_checkable
class CostTrackerLike(Protocol):
    """``CostTracker.record`` write surface.

    The sandbox is the first caller of this protocol; the production
    implementation (``libs/cost-tracking/src/cost_tracking/tracker.py``)
    runs ``INSERT ... ON CONFLICT (activity_id) DO NOTHING`` against
    ``shared.cost_tracking``. Until that lib lands the sandbox uses
    :class:`NullCostTracker` so the round-trip still works in
    standalone dev environments.
    """

    async def record(self, entry: CostEntryLike) -> None:  # pragma: no cover - protocol
        """Persist ``entry`` (idempotent on ``activity_id``)."""


# ---------------------------------------------------------------------------
# Defaults — used by tests and standalone mode
# ---------------------------------------------------------------------------


class NullCostTracker:
    """No-op cost tracker.

    Returned by the lifespan hook in :mod:`src.main` when the
    cost-tracking backend is unavailable, and used by the unit tests
    that focus on sandbox behaviour rather than the cost write path.
    The class still satisfies :class:`CostTrackerLike` so the
    sandbox does not need a separate code path for "no tracker
    configured".
    """

    def __init__(self) -> None:
        self.records: list[CostEntryLike] = []

    async def record(self, entry: CostEntryLike) -> None:
        # We retain the entries on the instance so unit tests can
        # assert the sandbox issued exactly one record per ``run``
        # call with the correct tag.
        self.records.append(entry)
        _LOG.debug(
            "sandbox cost recorded (null tracker)",
            extra={
                "activity_id": entry.activity_id,
                "cost_tag": entry.cost_tag,
                "cost_usd": str(entry.cost_usd),
            },
        )


class SyntheticLlmInvoker:
    """Deterministic, dependency-free invoker reserved for isolated tests.

    The output is a stable synthetic preview string so UI snapshots
    stay readable; token counts approximate the prompt length so the
    cost recorder always sees plausible numbers. Production deployments
    use :class:`ProviderLlmInvoker`.
    """

    def __init__(
        self,
        *,
        model: str = "synthetic-model",
        provider: str = "synthetic",
        cost_per_1k_tokens: Decimal = Decimal("0.0"),
    ) -> None:
        self._model = model
        self._provider = provider
        self._cost_per_1k = cost_per_1k_tokens

    async def invoke(
        self,
        *,
        system: str,
        user: str,
        cost_tag: str,
    ) -> LlmInvocationResult:
        # We do not actually use ``cost_tag`` to alter the response —
        # the tag is the sandbox's signal to the cost layer, not the
        # provider. Production providers also accept it as a passive
        # passthrough.
        _ = cost_tag

        # Token estimates: roughly one "token" per whitespace-split
        # word, which is enough for the unit tests that assert the
        # tracker received non-negative integers.
        token_in = len(system.split()) + len(user.split())
        # The synthetic invoker does not actually run a model; we generate a
        # short echo so the developer can see the prompt + sample
        # were forwarded correctly.
        flat_user = " ".join(user.split())
        preview = flat_user[:32]
        response_text = f"[synthetic] {preview}" if preview else "[synthetic]"
        token_out = len(response_text.split())
        cost = (
            self._cost_per_1k
            * Decimal(token_in + token_out)
            / Decimal(1000)
        )
        return LlmInvocationResult(
            response_text=response_text,
            token_in=token_in,
            token_out=token_out,
            cost_usd=cost.quantize(Decimal("0.000001")),
            model=self._model,
            provider=self._provider,
        )


class ProviderLlmInvoker:
    """Production sandbox invoker backed by the configured LLM provider."""

    def __init__(self) -> None:
        from llm_orchestrator import LLMProviderFactory

        self._provider = LLMProviderFactory.from_env()

    async def invoke(
        self,
        *,
        system: str,
        user: str,
        cost_tag: str,
    ) -> LlmInvocationResult:
        _ = cost_tag
        prompt = f"SYSTEM:\n{system}\n\nUSER:\n{user}"
        response_text = await asyncio.to_thread(self._provider.complete, prompt)
        token_in = len(system.split()) + len(user.split())
        token_out = len(response_text.split())
        return LlmInvocationResult(
            response_text=response_text,
            token_in=token_in,
            token_out=token_out,
            cost_usd=Decimal("0.0"),
            model=str(getattr(self._provider, "model_name", "unknown")),
            provider=str(getattr(self._provider, "name", "unknown")),
        )


# ---------------------------------------------------------------------------
# PromptSandbox
# ---------------------------------------------------------------------------


class PromptSandbox:
    """Isolated LLM call for prompt drafts.

    Args:
        llm: Single-shot LLM invoker (any object satisfying
            :class:`LlmInvokerLike`). Production wiring adapts the
            assistant-service orchestrator here; tests inject
            :class:`SyntheticLlmInvoker`.
        cost_tracker: Cost recorder (any
            :class:`CostTrackerLike` implementation). Production
            wiring uses the asyncpg-backed
            ``cost_tracking.CostTracker``; tests use
            :class:`NullCostTracker`.
        activity_id_factory: Callable returning a fresh activity id
            on every ``run`` call. Defaults to a monotonic
            ``time.time_ns()`` based string so tests can override
            with a deterministic counter.
        clock: Callable returning the wall-clock time used to stamp
            :class:`SandboxResult.invoked_at`. Defaults to
            ``datetime.now(timezone.utc)``.

    The class is intentionally small — it owns the ``cost_tag``
    contract and the activity id assignment; everything else is
    delegated to the collaborator protocols.
    """

    def __init__(
        self,
        *,
        llm: LlmInvokerLike,
        cost_tracker: CostTrackerLike,
        activity_id_factory: "Callable[[], str] | None" = None,
        clock: "Callable[[], datetime] | None" = None,
    ) -> None:
        self._llm = llm
        self._cost = cost_tracker
        self._activity_id_factory = activity_id_factory or _default_activity_id
        self._clock = clock or _default_clock

    async def run(
        self,
        prompt_body: str,
        sample_input: str,
        *,
        dept_id: str | None = None,
        user_id: str | None = None,
    ) -> SandboxResult:
        """Invoke the LLM with the draft prompt and record the cost.

        Args:
            prompt_body: The draft prompt body (system message). The
                caller (``PromptsGitRouter.post_sandbox_test``) reads
                this from the draft branch and forwards it verbatim
                — the sandbox does **not** validate or render
                template variables, because the editor wants to test
                the raw body.
            sample_input: Sample user message the developer typed
                into the sandbox-test form. Forwarded as the
                ``user`` role in the LLM call.
            dept_id: Department the prompt belongs to (optional).
                Carried through to the cost record so
                ``/admin/costs`` can attribute sandbox spend per
                team. ``None`` is acceptable for cross-dept prompts
                (eg. assistant_chat.md is global).
            user_id: Caller id from the OIDC ``sub`` claim. Carried
                through for per-developer sandbox spend reporting.

        Returns:
            A frozen :class:`SandboxResult` with the LLM response
            text, token counts, cost and invocation timestamp. The
            ``cost_tag`` field is always ``"sandbox"``.

        Notes:
            The method is **not** retried on LLM failure. Sandbox
            tests are interactive — a transient 429 should surface
            to the developer rather than being silently retried,
            because the developer is iterating on the prompt body
            and a stale-but-successful retry would confuse the
            edit/test loop.
        """

        invoked_at = self._clock()
        activity_id = self._activity_id_factory()

        # ---- 1. Issue the LLM call with the sandbox tag --------------
        # The ``cost_tag="sandbox"`` keyword is the **single** signal
        # the LLM provider gets that this call should not feed a
        # production budget. Production providers ignore the tag at
        # the invocation level (they care only about model + tokens)
        # and forward it to the cost record we build in step 2.
        invocation = await self._llm.invoke(
            system=prompt_body,
            user=sample_input,
            cost_tag=COST_TAG_SANDBOX,
        )

        # ---- 2. Record the cost with cost_tag="sandbox" -------------
        # The CostEntry carries ``cost_tag=COST_TAG_SANDBOX`` so the
        # row in ``shared.cost_tracking`` is filtered out by
        # ``BudgetCapPolicy`` (which selects on
        # ``cost_tag = 'production'``). Failing to record cost must
        # NOT mask the sandbox response — the developer still wants
        # to see the LLM output even if the cost write transiently
        # fails, so we log + continue.
        cost_entry = CostEntryLike(
            activity_id=activity_id,
            dept_id=dept_id,
            user_id=user_id,
            workflow_id=None,  # Sandbox runs are not Temporal workflows
            model=invocation.model,
            provider=invocation.provider,
            token_in=invocation.token_in,
            token_out=invocation.token_out,
            cost_usd=invocation.cost_usd,
            cost_tag=COST_TAG_SANDBOX,
        )
        try:
            await self._cost.record(cost_entry)
        except Exception as exc:  # noqa: BLE001 — fail-soft per ops policy
            _LOG.warning(
                "sandbox cost write failed; response still returned to caller",
                extra={
                    "activity_id": activity_id,
                    "cost_tag": COST_TAG_SANDBOX,
                    "error": str(exc),
                },
            )

        # ---- 3. Build the deterministic SandboxResult ---------------
        return SandboxResult(
            response_text=invocation.response_text,
            token_in=invocation.token_in,
            token_out=invocation.token_out,
            cost_usd=invocation.cost_usd,
            invoked_at=invoked_at,
            model=invocation.model,
            provider=invocation.provider,
            cost_tag=COST_TAG_SANDBOX,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_activity_id() -> str:
    """Generate a process-unique activity id for a sandbox run.

    The id format is ``"sandbox-<ns>-<counter>"`` where ``<ns>`` is
    ``time.time_ns()`` (nanosecond clock — already unique across
    processes started within the same nanosecond would still be
    differentiated by the counter), and ``<counter>`` is a
    monotonically incrementing int local to this process. The
    counter guarantees uniqueness even if two ``run`` calls land in
    the same nanosecond bucket on a fast clock.

    The format is *only* an implementation detail of the standalone
    default; production callers should pass an
    ``activity_id_factory`` rooted in their own correlation id
    stream (eg. the request id from FastAPI).
    """

    global _ACTIVITY_COUNTER
    _ACTIVITY_COUNTER += 1
    return f"sandbox-{time.time_ns()}-{_ACTIVITY_COUNTER}"


def _default_clock() -> datetime:
    """Return the current UTC wall-clock time."""

    return datetime.now(tz=timezone.utc)


# Module-level counter used by :func:`_default_activity_id`. Reset
# only via process restart; tests that need determinism inject a
# custom ``activity_id_factory`` via the constructor.
_ACTIVITY_COUNTER: int = 0
