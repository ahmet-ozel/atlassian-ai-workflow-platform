"""FastAPI entrypoint for assistant-service.

Listens on port 8081 and exposes:

* ``GET /healthz`` / ``GET /readyz`` — service health endpoints.
* ``POST /api/chat/stream`` — SSE chat endpoint owned by
  :class:`src.chat.handler.ChatHandler`. Every request flows through the deterministic
  six-step pipeline (PII mask → sliding window → system prompt
  render → tool filter → LLM tool-call loop → audit) before the
  first SSE byte is yielded.
* ``POST /api/session-credentials/...`` — per-user session
  credential relay.

Lifespan wiring:

* :class:`prompts.PromptLoader` is constructed from the
  ``service-local prompts/`` + ``platform/prompts/`` roots and its
  :meth:`poll_loop` is launched as a background task so prompt
  changes hot-reload within 30s.
* :class:`llm_orchestrator.LlmOrchestrator` is wired with the
  primary (vLLM) and fallback (OpenAI) providers; the property
  tests inject fakes into this same surface.
* :class:`audit_logger.AuditLogger` is opened against
  ``automation.audit_events`` for the ``chat_message`` row.
* The :class:`src.chat.handler.ChatHandler` is built once and stored
  on ``app.state.chat_handler`` so every SSE request reuses the same
  collaborator graph.

When any optional collaborator (Vault for credentials, Postgres for
audit, vLLM for the primary provider) is unavailable, the lifespan
catches the failure, leaves the slot ``None``, and lets ``/readyz``
flip to 503 with a clear reason. ``/healthz`` stays 200 so the
container does not flap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from http_shared import SecurityHeadersMiddleware, install_redaction_filter

# ``observability.TraceMiddleware`` extracts / generates the
# ``X-Trace-Id`` header per inbound request and binds it onto the
# per-request :mod:`contextvars` context so MCP calls issued by the
# chat handler's tool-dispatcher (and SSE error replies) carry the
# same trace_id as the originating webhook
# for the originating request.
from observability import TraceMiddleware

from .config import Settings

settings = Settings()


logger = logging.getLogger(__name__)

# Wire the secret-hygiene log redaction filter onto the root logger
# before FastAPI / uvicorn build their handler chain.
install_redaction_filter(loggers=[logging.getLogger()], attach_to_root=True)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the ChatHandler graph and launch the prompt poll loop.

    Every collaborator is wired in a try/except so a partial
    deployment never blocks the rest of the surface. The pieces
    are:

    1. :class:`prompts.PromptLoader` — service-local prompts/ +
       platform/prompts/ search roots.
    2. :class:`llm_orchestrator.LlmOrchestrator` — primary (vLLM) +
       fallback (OpenAI). Starts in a degraded "no-providers"
       mode when neither lib can be imported; SSE requests then
       return 503.
    3. :class:`audit_logger.AuditLogger` — Postgres-backed when
       asyncpg + DSN are reachable, log-only otherwise.
    4. :class:`src.chat.handler.ChatHandler` — composed from the
       three collaborators above plus the foundation
       :class:`mcp_client` capability gate.
    """

    app.state.chat_handler = None
    app.state.prompt_loader = None
    app.state.poll_task = None
    app.state.audit_logger = None
    app.state.bot_info_deps = None
    app.state.db_pool = None
    app.state.llm_client = None
    app.state.session_creds = None

    # ---- 1. PromptLoader --------------------------------------------
    try:
        from prompts import PromptLoader

        # Resolve prompts roots:
        #   service_local: services/assistant-service/prompts/
        #   shared:        platform/prompts/
        # The module file lives at services/assistant-service/src/main.py;
        # parents[1] → services/assistant-service, parents[3] → platform.
        module_path = Path(__file__).resolve()
        candidate_roots = [module_path.parents[1] / "prompts"]
        if len(module_path.parents) > 3:
            candidate_roots.append(module_path.parents[3] / "prompts")
        roots = tuple(p for p in candidate_roots if p.is_dir())
        if roots:
            loader = PromptLoader(roots=roots, poll_interval_s=30)
            app.state.prompt_loader = loader
            app.state.poll_task = asyncio.create_task(loader.poll_loop())
            logger.info(
                "prompt_loader wired with roots=%s; poll_loop started",
                [str(r) for r in roots],
            )
        else:
            logger.warning(
                "no prompts roots resolved; chat handler will return 503"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("PromptLoader unavailable: %s", exc)

    # ---- 2. Provider credential validation (fail-fast) ---------------
    #
    # If the configured LLM provider requires
    # credentials that are missing or invalid, the service MUST fail at
    # boot time with a clear ConfigurationError. This prevents a
    # half-wired deployment from accepting traffic.
    settings.validate_provider_credentials()

    # ---- 3. LLM orchestrator ---------------------------------------
    llm = None
    try:
        from llm_orchestrator.orchestrator import LlmOrchestrator
        from llm_orchestrator.provider import LLMProviderFactory
        from .llm_stream_adapter import StreamingProviderAdapter

        primary, fallback = LLMProviderFactory.from_env_with_fallback()
        app.state.llm_client = primary
        llm = LlmOrchestrator(
            primary=StreamingProviderAdapter(primary),
            fallback=StreamingProviderAdapter(fallback) if fallback else None,
        )
        logger.info(
            "llm_orchestrator wired (primary=%s); app.state.llm_client set",
            type(primary).__name__,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LlmOrchestrator wiring failed: %s", exc)

    # ---- 4. Database pool + audit writer ---------------------------
    try:
        import asyncpg

        pool = None
        last_exc: Exception | None = None
        for attempt in range(1, 13):
            try:
                pool = await asyncpg.create_pool(
                    dsn=settings.postgres_dsn,
                    min_size=1,
                    max_size=5,
                    command_timeout=10,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "Database pool unavailable on attempt %s/12: %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(2)
        if pool is None:
            raise last_exc or RuntimeError("Database pool unavailable")
        app.state.db_pool = pool

        from .bot_info import BotInfoDeps

        app.state.bot_info_deps = BotInfoDeps(db=pool)
        from audit_logger import AuditLogger
        from .audit_writer import AsyncpgAuditEventsWriter

        app.state.audit_logger = AuditLogger(
            writer=AsyncpgAuditEventsWriter(pool=pool)
        )
        logger.info("bot_info_deps and audit_logger wired (asyncpg pool)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Database pool / audit logger unavailable: %s", exc)

    # ---- 4b. Vault client for per-user session credentials ----------
    try:
        from vault_client import make_client
        from .session_credentials import SessionCredentialDeps

        app.state.session_creds = SessionCredentialDeps(vault=make_client(os.environ))
        logger.info("session credential vault client wired")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Session credential Vault wiring failed: %s", exc)

    # ---- 5. ChatHandler --------------------------------------------
    try:
        from .chat.handler import ChatHandler, ChatHandlerDeps
        from .chat.sliding_window import compress
        from mcp_client import AtlassianClient, filter_tools  # noqa: F401 - sanity check
        from .mcp_tool_dispatch import McpToolDispatch

        # Instantiate the MCP client with the mandatory client_source
        # identifier. This client is used for
        # read calls within the chat handler's tool-call loop.
        mcp_client = AtlassianClient(
            client_source=settings.client_source,
            mcp_base_url=settings.mcp_base_url,
        )
        app.state.mcp_client = mcp_client

        if (
            app.state.prompt_loader is not None
            and llm is not None
            and app.state.audit_logger is not None
        ):
            # The list_tools callable returns the MCP tool catalog
            # pre-filtered through the AtlassianClient's banned-tool
            # filter. The handler applies capability-gate filtering on
            # top. Until the full MCP HTTP wiring is available, the
            # catalog is empty and the handler gracefully handles that
            # state.
            tool_dispatch = McpToolDispatch(
                mcp_base_url=settings.mcp_base_url,
                session_deps=app.state.session_creds,
            )

            async def _list_tools_via_mcp():
                return mcp_client.available_tools(await tool_dispatch.list_tools())

            deps = ChatHandlerDeps(
                prompt_loader=app.state.prompt_loader,
                compress=compress,
                summariser=_default_summariser,
                capability_gate=_passthrough_capability_gate,
                llm=llm,  # type: ignore[arg-type]
                tool_dispatch=tool_dispatch,
                audit=app.state.audit_logger,
                token_cap=int(getattr(settings, "chat_token_cap", 100_000)),
                sliding_window_n=20,
                prompt_name="assistant_chat",
                list_tools=_list_tools_via_mcp,
                timeout_s=settings.llm_request_timeout_s,
                max_tokens_output=settings.llm_max_tokens_output,
            )
            app.state.chat_handler = ChatHandler(deps)
            logger.info("chat_handler wired with AtlassianClient(client_source=%s)", settings.client_source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ChatHandler wiring failed: %s", exc)

    try:
        yield
    finally:
        poll_task = getattr(app.state, "poll_task", None)
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Close the database pool if it was created.
        db_pool = getattr(app.state, "db_pool", None)
        if db_pool is not None:
            await db_pool.close()


# ---------------------------------------------------------------------------
# Default collaborators for the chat handler
# ---------------------------------------------------------------------------


def _default_summariser(messages):
    """Trivial summariser used until the LLM-backed one lands.

    Returns the joined first 200 characters of the dropped older
    messages so the cache-friendly behaviour of the sliding window
    is still observable in dev. Production replaces this with an
    LLM-backed summariser invoked through the orchestrator.
    """

    return " | ".join(m.text[:80] for m in messages)[:200]


def _passthrough_capability_gate(tools, *, capabilities):
    """Pass-through capability gate.

    The foundation gate lives in :mod:`mcp_client`. Until it's wired
    here we surface every banned-tool-
    filtered tool — the chat handler still applies the foundation
    banned-tool list before reaching this gate.
    """

    return list(tools)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


app = FastAPI(
    title="assistant-service",
    version="0.1.0",
    description="Assistant HTTP service — SSE chat tool-call loop.",
    lifespan=lifespan,
)


# Mount :class:`TraceMiddleware` so every
# inbound HTTP request (``/api/chat/stream`` and the session-credential
# relay) extracts the inbound ``X-Trace-Id`` (or generates a fresh
# UUIDv7) and exposes it to the chat handler via
# :func:`observability.get_trace_id`.  The MCP-client request hook in
# :mod:`http_shared.client` reads the same context variable so every
# tool-call HTTP request issued during the SSE stream carries the
# originating trace_id.
app.add_middleware(TraceMiddleware)

# Mount :class:`SecurityHeadersMiddleware`
# so every HTTP response carries X-Frame-Options, X-Content-Type-Options
# and X-XSS-Protection headers regardless of status code or content type.
app.add_middleware(SecurityHeadersMiddleware)


# Mount the per-user session credential relay.
from .session_credentials import router as session_credentials_router  # noqa: E402

app.include_router(session_credentials_router)

# Mount the bot-info endpoint for the Task Creator assignee card.
from .bot_info import router as bot_info_router  # noqa: E402

app.include_router(bot_info_router)

# Mount Streamlit Task Creator proxy. It creates Jira issues through MCP.
from .task_creator import router as task_creator_router  # noqa: E402

app.include_router(task_creator_router)

# Mount the credential-ref aware MCP proxy used by Streamlit Explorer /
# MCP Inspector. The proxy dereferences session Vault paths server-side,
# then calls the stateless MCP with canonical X-Atlassian-* headers.
from .mcp_proxy import router as mcp_proxy_router  # noqa: E402

app.include_router(mcp_proxy_router)


# ---------------------------------------------------------------------------
# Standard probes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz(request: Request) -> Response:
    """Liveness probe.

    Returns 200 when the service process is alive AND the LLM provider
    has been successfully wired at boot. If ``app.state.llm_client`` is
    None (provider wiring failed or credentials were invalid), returns
    503 ``not_ready`` so load-balancers stop routing traffic here.
    """
    llm_client = getattr(request.app.state, "llm_client", None)
    if llm_client is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "llm_provider_not_wired"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/readyz")
async def readyz(response: Response) -> JSONResponse:
    """Readiness probe with real dependency checks.

    Probes PostgreSQL (``SELECT 1``), Redis (``PING``), and MCP
    (``/healthz``) in parallel with a 3-second per-probe timeout.

    Returns 200 ``{"status": "ready"}`` when all dependencies are
    reachable. Returns 503 ``{"status": "not_ready",
    "failed_dependencies": [...]}`` when any probe fails.
    """
    from . import readiness as _readiness

    all_ready, details = await _readiness.check_readiness([
        lambda: _readiness.probe_postgres(settings.postgres_dsn),
        lambda: _readiness.probe_redis(settings.redis_url),
        lambda: _readiness.probe_mcp(settings.mcp_base_url),
    ])

    if not all_ready:
        return JSONResponse(status_code=503, content=details)
    if getattr(app.state, "chat_handler", None) is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "failed_dependencies": ["chat_handler"],
                "reason": "chat_handler_not_wired",
            },
        )
    return JSONResponse(status_code=200, content=details)


# ---------------------------------------------------------------------------
# /api/chat/stream
# ---------------------------------------------------------------------------


@app.post("/api/chat/stream")
async def chat_stream(request: Request) -> StreamingResponse:
    """SSE chat endpoint.

    Body shape (JSON):
        {
            "user_message": str,
            "history": [{"role": "user"|"assistant"|"system", "text": str}, ...],
            "dept_id": str,
            "actor_id": str,
            "actor_role": str
        }

    Response: ``text/event-stream`` SSE stream. Each chunk is
    ``data: <json>\\n\\n`` where ``<json>`` is a serialised
    :class:`messages.SseEvent`. Terminal events are ``done``,
    ``rate_limit_exhausted``, ``token_cap_exceeded``,
    ``redirect_to_task_creator`` or ``error``.
    """

    chat_handler = getattr(request.app.state, "chat_handler", None)
    if chat_handler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "chat_handler_not_wired"},
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON body")

    try:
        from messages import ChatRequest, Message
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=503,
            detail={"reason": "messages lib not importable", "error": str(exc)},
        ) from exc

    history_payload = payload.get("history", [])
    history = tuple(
        Message(role=h.get("role", "user"), text=h.get("text", ""))
        for h in history_payload
        if isinstance(h, dict)
    )
    chat_request = ChatRequest(
        user_message=payload.get("user_message", ""),
        history=history,
        dept_id=payload.get("dept_id", "default"),
        session_id=payload.get("session_id", ""),
    )

    actor = _ActorAdapter(
        actor_id=payload.get("actor_id", "anonymous"),
        actor_role=payload.get("actor_role", "operator"),
    )

    from .chat.handler import DeptContext

    dept = DeptContext(
        dept_id=payload.get("dept_id", "default"),
        department_repos=tuple(payload.get("department_repos", [])),
        capabilities=frozenset(payload.get("capabilities", [])),
        default_language=payload.get("default_language", "tr"),
        bot_username=payload.get("bot_username", "assistant-bot"),
    )

    async def _stream() -> AsyncIterator[bytes]:
        from .mcp_tool_dispatch import bind_credential_refs, reset_credential_refs

        refs = {
            "jira": request.headers.get("X-Credential-Ref-Jira")
            or request.headers.get("X-Credential-Ref")
            or "",
            "bitbucket": request.headers.get("X-Credential-Ref-Bitbucket") or "",
            "confluence": request.headers.get("X-Credential-Ref-Confluence") or "",
        }
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            for service, ref in tuple(refs.items()):
                if not ref:
                    refs[service] = (
                        f"vault:atlassian/_user_session/{session_id}/{service}"
                    )
        token = bind_credential_refs({k: v for k, v in refs.items() if v})
        try:
            async for event in chat_handler.stream(chat_request, actor, dept):
                serialised = {
                    "type": event.type,
                    "payload": event.payload,
                }
                yield f"data: {json.dumps(serialised, default=str)}\n\n".encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat_stream raised: %s", exc, exc_info=True)
            yield (
                f"data: {json.dumps({'type': 'error', 'payload': {'reason': type(exc).__name__, 'error': str(exc)}})}\n\n"
            ).encode("utf-8")
        finally:
            reset_credential_refs(token)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class _ActorAdapter:
    """Minimal adapter exposing ``actor_id`` / ``actor_role`` to the handler.

    The full :class:`auth_shared.AuthContext` is built by the
    foundation OIDC dependency; until that's wired we surface the
    pair from the request body so the audit row carries the right
    fields without forcing every dev environment to stand up an IdP.
    """

    def __init__(self, *, actor_id: str, actor_role: str) -> None:
        self.actor_id = actor_id
        self.actor_role = actor_role


# ---------------------------------------------------------------------------
# Local launcher
# ---------------------------------------------------------------------------


def main() -> None:
    """Local dev entrypoint: ``python -m src.main``."""
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
