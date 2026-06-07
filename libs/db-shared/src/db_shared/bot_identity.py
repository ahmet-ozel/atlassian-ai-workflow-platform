"""Bot account_id uniqueness validator.

This module owns the third layer of the bot account uniqueness defence:

* DB layer  - partial UNIQUE INDEX on ``automation.department_bots``
  ``(service, account_id) WHERE account_id <> ''`` (migration
  ``012_bot_identity_unique.sql``).
* CRUD layer - ``_find_account_id_conflicts`` in
  ``admin-dashboard-api/src/routers/departments.py`` rejects POST /
  PATCH bodies that would introduce a clash with HTTP 409
  ``account_id_conflict``.
* Boot-time - :func:`validate_bot_account_id_uniqueness` (this module)
  scans the parsed ``departments.json`` document at service start-up
  and refuses to bring the process up if a clash exists. Wired into
  the ``admin-dashboard-api`` lifespan so an admin who hand-edits the
  config file outside the CRUD endpoint still gets a fail-fast error
  instead of silent routing ambiguity.

The function is intentionally pure and synchronous so it can be called
from any startup hook - FastAPI lifespan, ``__main__`` entry point,
unit tests - without dragging an asyncio runtime, a DB pool, or the
admin-dashboard-api package into the dependency graph.

Whitespace-only ``account_id`` values are treated as placeholders (the
bundled ``departments.json`` ships ``"account_id": ""`` rows for depts
whose Vault probe has not yet populated the field). They never
participate in conflict detection - same convention as
``_extract_bot_identities`` in the CRUD layer so the three layers
agree on the same notion of "real" id.

"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BOT_IDENTITY_SERVICES",
    "BotAccountIdConflict",
    "BotAccountIdConflictError",
    "validate_bot_account_id_uniqueness",
    "validate_bot_account_id_uniqueness_from_file",
]

_LOG = logging.getLogger(__name__)

#: Atlassian surfaces the webhook dispatcher routes on. Mirrors
#: ``_BOT_IDENTITY_SERVICES`` in
#: ``admin-dashboard-api/src/routers/departments.py`` so the CRUD
#: layer and the boot-time validator agree on the same list. Kept in
#: declaration order so error messages enumerate services in a stable
#: way.
BOT_IDENTITY_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")


@dataclass(frozen=True, slots=True)
class BotAccountIdConflict:
    """A single ``(service, account_id)`` pair claimed by 2+ depts.

    Attributes:
        service: Atlassian surface - one of
            :data:`BOT_IDENTITY_SERVICES`.
        account_id: The non-empty ``account_id`` value that two or
            more departments listed under
            ``bot.{service}.account_id``.
        dept_ids: The departments that all claimed this id. The
            tuple preserves the order departments appear in the
            input so error messages stay deterministic across runs.
    """

    service: str
    account_id: str
    dept_ids: tuple[str, ...]

    def message(self) -> str:
        """Render the canonical fail-fast line for this conflict."""

        return (
            f"service={self.service!r} account_id={self.account_id!r} "
            f"claimed by depts={list(self.dept_ids)}"
        )


class BotAccountIdConflictError(ValueError):
    """Raised when boot-time uniqueness validation finds any clash.

    Subclasses :class:`ValueError` so callers that already catch the
    standard "bad config" exception type pick this up automatically.
    The message lists every offending pair so an operator can fix the
    config in a single edit.

    Attributes:
        conflicts: The full list of detected conflicts. Always
            non-empty - the constructor would not be reached
            otherwise.
    """

    def __init__(self, conflicts: Sequence[BotAccountIdConflict]) -> None:
        if not conflicts:  # pragma: no cover - defensive
            raise AssertionError(
                "BotAccountIdConflictError requires at least one conflict"
            )
        self.conflicts = tuple(conflicts)
        header = (
            f"refusing to start: {len(self.conflicts)} bot account_id "
            f"uniqueness violation(s) detected in departments.json:"
        )
        body = "\n".join(f"  - {c.message()}" for c in self.conflicts)
        super().__init__(f"{header}\n{body}")


def validate_bot_account_id_uniqueness(
    departments: Iterable[Mapping[str, Any]],
) -> None:
    """Validate every dept's bot.{jira,bitbucket,confluence}.account_id.

    Walks the iterable of department config dicts (each one a parsed
    entry from ``departments.json``'s ``departments`` array) and
    builds a ``(service, account_id) -> [dept_id, ...]`` map. Any
    pair claimed by more than one department is flagged.

    Empty / whitespace ``account_id`` values are treated as
    placeholders and skipped - those rows are not yet routing keys.

    Args:
        departments: Iterable of dept config mappings. Mappings that
            are not dicts, lack an ``id``, or whose ``bot`` field is
            absent / non-dict are skipped silently - schema-level
            validation catches those upstream
            (:func:`db_shared.config_validator.validate_departments_config`).
            This validator focuses on a single semantic invariant.

    Raises:
        BotAccountIdConflictError: One or more ``(service,
            account_id)`` pairs are claimed by two or more
            departments. The exception message lists every clash so
            the operator can resolve all conflicts in one pass.
    """

    # ``defaultdict(list)`` preserves the order departments appear in
    # the iterable so the error message is deterministic across runs.
    claims: dict[tuple[str, str], list[str]] = defaultdict(list)

    for dept in departments:
        if not isinstance(dept, Mapping):
            continue
        dept_id_raw = dept.get("id")
        if not isinstance(dept_id_raw, str) or not dept_id_raw:
            # Schema validator catches missing ids; we just skip so a
            # malformed row cannot trip the uniqueness check with an
            # empty dept_id sentinel.
            continue
        dept_id = dept_id_raw

        bot = dept.get("bot")
        if not isinstance(bot, Mapping):
            continue

        for service in BOT_IDENTITY_SERVICES:
            entry = bot.get(service)
            if not isinstance(entry, Mapping):
                continue
            account_id = entry.get("account_id")
            if not isinstance(account_id, str):
                continue
            stripped = account_id.strip()
            if not stripped:
                # Placeholder - Vault probe has not run yet for this
                # dept × service pair. Same skip rule as the CRUD
                # layer's ``_extract_bot_identities``.
                continue

            key = (service, stripped)
            # Defend against an operator who copy-pasted the same
            # dept block twice: the same dept_id should still count
            # as a single claim, otherwise we would surface a
            # confusing "claimed by depts=['x', 'x']" message.
            if dept_id not in claims[key]:
                claims[key].append(dept_id)

    conflicts: list[BotAccountIdConflict] = []
    for (service, account_id), dept_ids in claims.items():
        if len(dept_ids) < 2:
            continue
        conflicts.append(
            BotAccountIdConflict(
                service=service,
                account_id=account_id,
                dept_ids=tuple(dept_ids),
            )
        )

    if conflicts:
        # Keep the order the conflicts were discovered in (insertion
        # order on ``claims`` - Python 3.7+ guarantees dict ordering)
        # so the message is stable across runs against the same
        # config.
        raise BotAccountIdConflictError(conflicts)

    _LOG.info(
        "bot_identity.uniqueness_ok service_count=%d unique_pairs=%d",
        len(BOT_IDENTITY_SERVICES),
        len(claims),
    )


def validate_bot_account_id_uniqueness_from_file(
    config_path: Path,
) -> None:
    """Convenience wrapper: read ``config_path`` and validate it.

    Reads ``departments.json`` from disk, extracts the
    ``departments`` array, and forwards to
    :func:`validate_bot_account_id_uniqueness`. Any
    :class:`OSError` / :class:`json.JSONDecodeError` raised by the
    read is **not** caught - the caller (typically the FastAPI
    lifespan handler) wraps the call in its own try / except so the
    process exits with a clear stderr line.

    Args:
        config_path: Absolute path to ``departments.json``.

    Raises:
        BotAccountIdConflictError: See
            :func:`validate_bot_account_id_uniqueness`.
        OSError: Propagated from the file read.
        json.JSONDecodeError: Propagated when the file is not valid
            JSON.
    """

    import json

    with open(config_path, encoding="utf-8") as f:
        doc = json.load(f)

    departments: Sequence[Mapping[str, Any]]
    if isinstance(doc, Mapping):
        raw = doc.get("departments", [])
        departments = raw if isinstance(raw, list) else []
    elif isinstance(doc, list):  # pragma: no cover - legacy shape
        departments = doc
    else:
        departments = []

    validate_bot_account_id_uniqueness(departments)
