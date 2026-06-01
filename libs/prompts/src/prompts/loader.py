"""Git-aware ``PromptLoader`` with 30-second mtime hot-reload.

The full design lives in
``.kiro/specs/platform-mimari-ops/design.md`` §`PromptLoader` and
satisfies Requirements **2.5** (hot-reload), **2.6** (``prompt_version``
= short git hash) and **2.7** (template variable injection). This
module is task **2.1** of the ``platform-mimari-ops`` plan; sibling
tasks **2.2** (:mod:`prompts.types`) and **2.3** (:mod:`prompts.validate`)
provide the data classes and template format validator the loader
delegates to.

Behavioural contract (verbatim from design):

* ``load(name)`` — file-backed; cached by ``name``. First call reads
  the prompt body from disk, calls ``git rev-parse --short HEAD --
  <path>`` to capture ``git_hash`` and stores a ``_PromptEntry``.
  Subsequent calls return the cached body.
* ``version(name)`` — returns the cached ``git_hash``. ``load`` must
  be called first; raises :class:`KeyError` otherwise (mirrors design
  pseudocode: ``self._cache[name].git_hash``).
* ``render(name, vars=...)`` — performs ``body.format(**asdict(vars))``;
  any :class:`KeyError` is converted to
  :class:`PromptTemplateError` so the CI gate flagged in
  Requirement 2.9 can fail the build.
* ``poll_loop()`` — async ``while True`` that re-stats every cached
  prompt once per ``poll_interval_s`` seconds and refreshes the cache
  if ``mtime`` advanced.
* ``_read(path)`` — every read invokes
  ``git rev-parse --short HEAD -- <path>`` via :mod:`subprocess`;
  fail-soft — when ``git`` is unavailable or the file is untracked,
  ``git_hash`` falls back to ``"unknown"`` and a ``warning`` is
  logged.

Design alignment notes
----------------------

* The cache key is the *logical name* (eg. ``"assistant_chat"``), not
  the resolved path. ``_resolve(name)`` walks ``self._roots`` in
  insertion order and returns the first matching ``<root>/<name>.md``.
  Resolution is intentionally simple — multi-root layering is a
  layering primitive, not a complex include system.
* Hot-reload is **fail-soft** (design "Hot-reload graceful failure"):
  if ``_read`` raises mid-poll, the existing cache row is kept and a
  ``warning`` is logged. Task 2.4 adds the ``audit
  prompt_hot_reload_failed`` write on top of this hook.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PromptNotFoundError, PromptTemplateError
from .types import _PromptEntry
from .validate import validate_template_format

if TYPE_CHECKING:  # pragma: no cover — only needed for static type checkers
    from .types import PromptVars


__all__ = ["PromptLoader"]


_log = logging.getLogger(__name__)


#: Default polling interval for :meth:`PromptLoader.poll_loop`. The
#: design pins this to 30 seconds (MIMARI §16.13 S18); it is
#: parametrised on :class:`PromptLoader` so tests can drive a faster
#: cadence without monkeypatching.
_DEFAULT_POLL_INTERVAL_S = 30


#: Sentinel returned by :func:`_git_short_hash_for` when the git
#: binary is missing, the repository has no commits, or the file is
#: untracked. Surfaced verbatim through :meth:`PromptLoader.version`
#: so the audit row records the fail-soft signal explicitly.
_UNKNOWN_GIT_HASH = "unknown"


#: Timeout for the ``git rev-parse`` subprocess. The call is local
#: and bounded; a multi-second hang is treated as "git unavailable"
#: and falls back to ``_UNKNOWN_GIT_HASH``.
_GIT_TIMEOUT_S = 5.0


class PromptLoader:
    """File-backed prompt loader with hot-reload + git hash audit.

    Validates:
        * R2.5 (hot-reload via 30s mtime poll)
        * R2.6 (``prompt_version`` = git short hash, audited)
        * R2.7 (template variable injection through :class:`PromptVars`)

    Args:
        roots: Ordered tuple of directories to search for prompts.
            ``load("assistant_chat")`` resolves to the first
            ``<root>/assistant_chat.md`` that exists. Earlier roots
            shadow later ones — typical layering is
            ``(service_local_root, shared_root)``.
        poll_interval_s: Seconds between mtime polls in
            :meth:`poll_loop`. Defaults to 30 (design-pinned).
    """

    def __init__(
        self,
        *,
        roots: tuple[Path, ...],
        poll_interval_s: int = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        if not roots:
            raise ValueError("PromptLoader requires at least one root path")
        # Defensive copy — callers occasionally hand in a mutable
        # list; storing the input verbatim would let them mutate our
        # search order from the outside.
        self._roots: tuple[Path, ...] = tuple(Path(r) for r in roots)
        self._cache: dict[str, _PromptEntry] = {}
        self._poll_interval_s = poll_interval_s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, name: str) -> str:
        """Resolve ``name`` and return the prompt body.

        First call reads from disk and populates the cache; subsequent
        calls return the cached body verbatim.

        Args:
            name: Logical prompt identifier — matches the file stem
                under one of ``self._roots`` (eg. ``"assistant_chat"``
                resolves to ``<root>/assistant_chat.md``).

        Returns:
            The prompt body as a string (no rendering applied).

        Raises:
            PromptNotFoundError: ``name`` does not resolve under any
                root.
        """

        entry = self._cache.get(name)
        if entry is None:
            entry = self._read(name)
            self._cache[name] = entry
        return entry.body

    def version(self, name: str) -> str:
        """Return the short git hash of the cached prompt.

        :meth:`load` must have been called for ``name`` first;
        otherwise :class:`KeyError` is raised — this matches the
        design pseudocode, which deliberately surfaces a "hot path
        before warm path" misuse instead of silently re-reading.

        Args:
            name: Same logical name passed to :meth:`load`.

        Returns:
            The short git hash (eg. ``"a1b2c3d"``) or
            ``"unknown"`` when the prompt is not tracked by git.
        """

        return self._cache[name].git_hash

    def render(self, name: str, *, vars: "PromptVars") -> str:
        """Inject template variables into the prompt body.

        Validates Requirement 2.7. The mapping passed to
        :meth:`str.format` is the ``dataclasses.asdict`` projection of
        ``vars`` — every field of :class:`prompts.types.PromptVars`
        becomes a placeholder. ``frozenset`` and ``tuple`` fields are
        converted to deterministic, comma-joined strings so the
        rendered output is stable across runs (audit reproducibility).

        Args:
            name: Prompt logical name (must already be cached).
            vars: :class:`prompts.types.PromptVars` instance.

        Returns:
            The rendered body with every placeholder substituted.

        Raises:
            PromptTemplateError: The body references a placeholder
                that is not part of the
                :class:`prompts.types.PromptVars` contract — Requirement
                2.9 forces this into a CI-failing error rather than a
                silent ``str.format`` ``KeyError``.
        """

        body = self.load(name)
        # Stable, deterministic projections for collection fields. We
        # do *not* rely solely on ``dataclasses.asdict`` because that
        # leaves ``frozenset`` / ``tuple`` as Python collections, which
        # would render as ``frozenset({'a', 'b'})`` literals — useless
        # to an LLM. The design (§PromptLoader.render) calls these
        # joins out explicitly.
        render_vars = dict(dataclasses.asdict(vars))
        render_vars["department_repos"] = ", ".join(vars.department_repos)
        render_vars["capabilities"] = ", ".join(sorted(vars.capabilities))

        try:
            return body.format(**render_vars)
        except KeyError as exc:
            # KeyError.args[0] is the missing placeholder name.
            missing = exc.args[0] if exc.args else "<unknown>"
            raise PromptTemplateError(
                f"prompt {name!r} references unknown placeholder {missing!r}; "
                "every placeholder must match a PromptVars field"
            ) from exc
        except IndexError as exc:  # noqa: BLE001
            # ``str.format`` raises IndexError for positional ``{0}``
            # / ``{}`` placeholders; PromptVars contract is *named*
            # only, so any positional placeholder is a template bug.
            raise PromptTemplateError(
                f"prompt {name!r} contains positional placeholder; "
                "only named PromptVars fields are allowed"
            ) from exc

    async def poll_loop(self) -> None:
        """30s mtime poll for hot-reload (Requirement 2.5).

        Runs forever; intended to be launched as a background task at
        service boot::

            asyncio.create_task(loader.poll_loop())

        Each iteration walks every cached prompt, re-stats the file
        and replaces the cache row when ``mtime`` advanced. Failures
        are **fail-soft** — a single broken read keeps the existing
        cache row in place and logs a warning so callers continue to
        serve the last-known-good prompt (design "Hot-reload graceful
        failure").
        """

        while True:
            for name in list(self._cache.keys()):
                try:
                    fresh = self._read(name)
                except Exception as exc:  # noqa: BLE001 — fail-soft per design
                    _log.warning(
                        "prompt hot-reload read failed; keeping cached body",
                        extra={"prompt": name, "error": str(exc)},
                    )
                    continue

                cached = self._cache.get(name)
                if cached is None:
                    # Race: the cache row was evicted while we were
                    # reading. Drop our fresh copy on the floor — the
                    # next ``load(name)`` will repopulate.
                    continue

                if fresh.mtime > cached.mtime:
                    self._cache[name] = fresh
                    _log.info(
                        "prompt hot-reloaded",
                        extra={
                            "prompt": name,
                            "old_hash": cached.git_hash,
                            "new_hash": fresh.git_hash,
                        },
                    )

            await asyncio.sleep(self._poll_interval_s)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read(self, name: str) -> _PromptEntry:
        """Load ``name`` from disk and capture its git short hash.

        Every read goes through this helper so the git lookup is the
        single source of truth for ``prompt_version`` and the audit
        contract in Requirement 2.6. The body is also fed through
        :func:`prompts.validate.validate_template_format` (Requirement
        2.9) so a malformed template fails the boot/hot-reload read
        instead of surfacing at LLM render time.
        """

        path = self._resolve(name)
        body = path.read_text(encoding="utf-8")
        # Requirement 2.9 — reject unbalanced/unescaped braces and
        # unknown placeholders before the body lands in the cache.
        # ``PromptTemplateError`` propagates: the first ``load`` call
        # surfaces it to the caller (boot fails fast), and inside
        # :meth:`poll_loop` the fail-soft branch logs and keeps the
        # last-known-good entry.
        validate_template_format(body)
        mtime = path.stat().st_mtime
        git_hash = _git_short_hash_for(path)
        return _PromptEntry(body=body, mtime=mtime, git_hash=git_hash)

    def _resolve(self, name: str) -> Path:
        """Walk ``self._roots`` and return the first ``<root>/<name>.md``.

        Args:
            name: Logical prompt name (no extension, no directory).

        Returns:
            Absolute path to the resolved file.

        Raises:
            PromptNotFoundError: ``name`` is not present under any root.
        """

        # ``name`` may carry a sub-directory (eg. ``"notifications/workflow_failed"``)
        # which the design supports without comment; we honour that
        # by appending ``.md`` to the *full* name rather than the stem.
        relative = Path(f"{name}.md")
        for root in self._roots:
            candidate = root / relative
            if candidate.is_file():
                return candidate

        raise PromptNotFoundError(
            f"prompt {name!r} not found under any of: "
            + ", ".join(str(r) for r in self._roots)
        )


def _git_short_hash_for(path: Path) -> str:
    """Return ``git log -n1 --pretty=%h -- <path>`` or ``"unknown"``.

    The design specifies *commit-bazlı versiyon* (commit-based
    version): we want the short hash of the most recent commit that
    touched ``path``. ``git log -n1 --pretty=%h -- <path>`` is the
    canonical incantation.

    Fail-soft branches:
        * ``git`` binary missing → ``FileNotFoundError`` from
          :mod:`subprocess`.
        * Path is outside a working tree → non-zero exit; stderr is
          captured for the warning.
        * Path is tracked but never committed (eg. fresh
          ``git add``-only) → ``git log`` exits 0 with empty stdout.

    All branches resolve to ``_UNKNOWN_GIT_HASH`` plus a warning log
    so operators can investigate without breaking the request.
    """

    try:
        completed = subprocess.run(
            [
                "git",
                "log",
                "-n",
                "1",
                "--pretty=%h",
                "--",
                str(path),
            ],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        # ``git`` not installed / not on PATH.
        _log.warning(
            "git binary unavailable; using fallback prompt_version",
            extra={"path": str(path), "fallback": _UNKNOWN_GIT_HASH},
        )
        return _UNKNOWN_GIT_HASH
    except subprocess.TimeoutExpired:
        _log.warning(
            "git rev-parse timed out; using fallback prompt_version",
            extra={"path": str(path), "fallback": _UNKNOWN_GIT_HASH},
        )
        return _UNKNOWN_GIT_HASH
    except OSError as exc:  # eg. EACCES on the .git dir
        _log.warning(
            "git rev-parse OS error; using fallback prompt_version",
            extra={"path": str(path), "error": str(exc)},
        )
        return _UNKNOWN_GIT_HASH

    if completed.returncode != 0:
        _log.warning(
            "git rev-parse exited non-zero; using fallback prompt_version",
            extra={
                "path": str(path),
                "returncode": completed.returncode,
                "stderr": completed.stderr.strip(),
            },
        )
        return _UNKNOWN_GIT_HASH

    short = completed.stdout.strip()
    if not short:
        # Path is untracked or never committed.
        _log.warning(
            "git rev-parse returned empty hash; using fallback prompt_version",
            extra={"path": str(path), "fallback": _UNKNOWN_GIT_HASH},
        )
        return _UNKNOWN_GIT_HASH

    return short
