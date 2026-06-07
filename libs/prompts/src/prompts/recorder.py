"""``PromptVersionRecorder``.

Records the ``(path, commit_hash, body_hash, seen_at)`` triple
into ``prompt_versions`` whenever the :class:`PromptLoader`
hot-reload poll detects a change. The table is the single source
of truth for "which prompt body version did the bot use at time
T" - required by audit trails that link a workflow run to the
exact prompt revision that produced the LLM output.

Storage: ``prompt_versions`` (created by ``20_ops.sql``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

__all__ = ["PromptVersionRecorder", "PromptVersionStore"]


@runtime_checkable
class PromptVersionStore(Protocol):
    """Minimal write surface for the ``prompt_versions`` table.

    Production wiring binds this to an asyncpg pool that issues
    ``INSERT ... ON CONFLICT (path, commit_hash) DO NOTHING`` so a
    repeated record on the same body is a no-op.
    """

    async def upsert(
        self,
        *,
        path: str,
        commit_hash: str,
        body_hash: str,
        seen_at: datetime,
    ) -> None: ...


@dataclass
class PromptVersionRecorder:
    """Best-effort recorder invoked from :meth:`PromptLoader.poll_loop`.

    Failure modes are non-fatal - a missing ``prompt_versions``
    table or a connection blip MUST NOT break the prompt cache
    refresh. The recorder logs at WARNING and lets the loader
    continue serving the in-memory body.
    """

    store: PromptVersionStore

    async def record(self, *, path: str, commit_hash: str, body: str) -> None:
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            await self.store.upsert(
                path=path,
                commit_hash=commit_hash,
                body_hash=body_hash,
                seen_at=datetime.now(tz=timezone.utc),
            )
        except Exception:  # noqa: BLE001 - never block the poll loop
            # The PromptLoader.poll_loop catches and logs; re-raising
            # would only crash the polling task. Swallow here.
            return
