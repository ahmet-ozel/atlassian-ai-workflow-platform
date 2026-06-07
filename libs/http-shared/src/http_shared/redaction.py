"""Logging redaction filter for credential / secret hygiene.

Implements the regex-based :class:`RedactionFilter`:

    The Logging_Layer SHALL ``Authorization: Basic <...>``,
    ``Bearer <...>``, ``api_token=<...>``, ``password=<...>``,
    ``secret=<...>`` desenlerini regex tabanlı redaction kuralı ile
    ``***REDACTED***`` ile değiştirir.

Design principles
-----------------

* **logging.Filter subclass.** The filter mutates ``LogRecord`` *before*
  any formatter sees it. This works for both ``logger.info("got %s",
  token)`` style (where the filter must redact ``record.msg`` and the
  rendered ``getMessage()``) and pre-formatted strings.

* **Defence in depth.** We also walk ``record.args`` (positional /
  keyword) so a sensitive value passed as a ``%s`` argument is masked
  before formatter substitution. ``record.msg`` is rewritten to its
  pre-redacted *rendered* form when args were present so the formatter
  sees a flat string with no remaining ``%`` placeholders - that
  prevents accidental ``TypeError: not enough arguments`` if any arg
  was reduced to its redacted scalar.

* **Idempotent sentinel.** The replacement token ``***REDACTED***``
  is itself opaque to every redaction pattern (no ``=``, no
  ``Authorization`` / ``Bearer`` keyword), so applying the filter to
  an already-redacted line is a no-op.

* **No I/O, no global state.** The module compiles its patterns at
  import time and exposes them as module-level constants. The filter
  instance is cheap to construct and safe to attach to every handler
  in every process.

The matching surface deliberately does *not* try to detect arbitrary
high-entropy strings - it enumerates the exact patterns that matter and
the test ``test_log_redaction.py`` validates
the ``KEY=<redacted>`` form for environment dumps. This filter
complements that test by covering the *log-call site* surface (HTTP
header echoes, OAuth bearer dumps, exception messages with literal
``password=...`` payloads).

Public API
~~~~~~~~~~

* :data:`REDACTION_PATTERNS` - the compiled regex list, exposed for
  unit-testing parity and ad-hoc redaction of non-log strings.
* :data:`REDACTION_PLACEHOLDER` - the literal sentinel
  (``"***REDACTED***"``).
* :func:`redact_text` - pure-string helper (no logging coupling).
* :class:`RedactionFilter` - :class:`logging.Filter` subclass for
  attachment to handlers.
* :func:`install_redaction_filter` - convenience wiring helper that
  attaches the filter to a logger's handlers and (optionally) to the
  root logger so every record flowing through ``logging`` is redacted.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final, Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentinel string substituted in place of every matched secret. Chosen
#: to be visually distinctive in logs and to *not* itself match any of
#: the redaction patterns (so :func:`redact_text` is idempotent).
REDACTION_PLACEHOLDER: Final[str] = "***REDACTED***"


def _kv_pattern(key: str) -> re.Pattern[str]:
    """Build a ``KEY=value`` redaction regex for the given key name.

    The value run is ``[^\\s&,;]+`` - one or more characters that are
    *not* a typical separator. This intentionally stops at:

    * whitespace (``\\s``) - the natural boundary in prose log lines
      and HTTP header echoes;
    * ``&`` - the form-body / query-string separator, so a line like
      ``api_token=AAA&password=BBB&secret=CCC`` redacts each value
      independently and preserves the operator-visible key names;
    * ``,`` and ``;`` - common separators in cookie / config dumps.

    Stopping at these boundaries preserves the surrounding context
    (other ``KEY=`` tokens, prose) while still removing the secret
    value wholesale. The key is matched case-insensitively because
    ``Authorization``, ``api_token``, ``password``, ``secret`` show
    up in mixed case across HTTP headers, query strings, and form
    bodies.
    """
    # ``(?i:KEY)`` keeps the case-insensitivity local to the key token
    # so the value group stays a strict ``[^\s&,;]+``. The capture group
    # over the key allows the substitution to preserve whatever case
    # the original log line used (so operators can still grep for the
    # specific key form they expect).
    return re.compile(rf"((?i:{re.escape(key)}))=[^\s&,;]+")


#: Compiled regex patterns for credential redaction. Ordering is
#: not significant - every pattern is applied in turn and replacements
#: do not interact (the placeholder is opaque to every pattern).
REDACTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # ``Authorization: Basic <base64-blob>`` - HTTP header echo. The
    # ``<base64-blob>`` run can include ``+/=`` so we match ``\S+``
    # rather than a strict base64 charset; the goal is full removal of
    # the value, not parser-grade validation.
    re.compile(r"(?i:Authorization):\s*(?i:Basic)\s+\S+"),
    # ``Bearer <token>`` - OAuth / OIDC access tokens. Matches both
    # ``Authorization: Bearer ...`` (already covered by the line above
    # via the value ``\S+``, but we keep this pattern for bare
    # ``Bearer abc.def.ghi`` mentions in exception messages) and
    # standalone ``Bearer ...`` runs.
    re.compile(r"(?i:Bearer)\s+\S+"),
    # ``api_token=<...>`` (Atlassian PAT echo).
    _kv_pattern("api_token"),
    # ``password=<...>`` (form bodies, connection strings, exception text).
    _kv_pattern("password"),
    # ``secret=<...>`` (HMAC payloads, generic config dumps).
    _kv_pattern("secret"),
    # ---------------------------------------------------------------
    # LLM provider key patterns.
    # ---------------------------------------------------------------
    # Anthropic keys: ``sk-ant-...`` - the public docs use the
    # ``sk-ant-`` prefix and a 95-char body for live keys; we match
    # at least 16 chars of body to keep the pattern conservative
    # while still catching every legitimate Anthropic credential.
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    # OpenAI project keys: ``sk-proj-...``.
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{16,}"),
    # OpenAI live keys (legacy ``sk-live-...``).
    re.compile(r"sk-live-[A-Za-z0-9_\-]{16,}"),
    # OpenAI test keys (``sk-test-...``).
    re.compile(r"sk-test-[A-Za-z0-9_\-]{16,}"),
    # OpenAI generic keys: ``sk-`` followed by 20+ chars. Listed
    # AFTER the more specific ``sk-{ant,proj,live,test}-`` patterns
    # so the longer-prefix match wins; the regex engine still tries
    # patterns in order but the placeholder is opaque to every
    # subsequent pass, so a key matched by ``sk-ant-...`` is no
    # longer visible to this rule.
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Google Gemini keys: ``AIza...`` followed by 30+ chars.
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
)


# ---------------------------------------------------------------------------
# Pure-string helper
# ---------------------------------------------------------------------------


def redact_text(text: str) -> str:
    """Apply every :data:`REDACTION_PATTERNS` rule to *text*.

    Returns a new string with each matched secret replaced by
    :data:`REDACTION_PLACEHOLDER`. The function is pure, idempotent,
    and safe to call on arbitrary user input (including the empty
    string, which is returned unchanged).

    For ``KEY=value`` patterns the original key (in its source case)
    is preserved as ``KEY=***REDACTED***`` so operators can still see
    *which* credential was masked.
    """
    if not text:
        return text

    out = text
    for pattern in REDACTION_PATTERNS:
        # ``Authorization: Basic ...`` and ``Bearer ...`` patterns have
        # no capture group: the whole match is a secret and is replaced
        # wholesale. ``KEY=value`` patterns have a single group around
        # the key name; we substitute ``KEY=<placeholder>`` so the
        # operator-visible key survives.
        if pattern.groups == 0:
            out = pattern.sub(REDACTION_PLACEHOLDER, out)
        else:
            out = pattern.sub(rf"\1={REDACTION_PLACEHOLDER}", out)
    return out


# ---------------------------------------------------------------------------
# logging.Filter subclass
# ---------------------------------------------------------------------------


class RedactionFilter(logging.Filter):
    """``logging.Filter`` that redacts secrets from every ``LogRecord``.

    Applied to a handler, this filter rewrites the record's ``msg`` /
    ``args`` so the downstream formatter only ever sees redacted
    content. Both common log-call shapes are covered:

    * ``logger.info("got %s", token)`` - ``token`` is in
      ``record.args``; we redact each positional / keyword arg, then
      collapse the rendered form into ``record.msg`` with empty args
      so the formatter does not try to re-substitute.
    * ``logger.info(f"got {token}")`` - the rendered string is in
      ``record.msg`` directly with ``record.args is None``; we redact
      ``record.msg`` in place.

    Errors are swallowed (the filter never *raises* and never
    *suppresses* a record) - a misformatted log call must still reach
    the operator, just with the secret masked. We rely on the
    pattern set being conservative enough that false positives are
    cosmetic rather than load-bearing.
    """

    name: str = "http_shared.redaction"

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        """Mutate *record* in place and always return ``True``."""

        try:
            self._redact_record(record)
        except Exception:  # noqa: BLE001 - filters MUST NOT crash logging
            # If anything goes wrong we let the original (unredacted)
            # record through rather than silently dropping it. A leaked
            # secret is bad, a missing log line about a leaked secret
            # is worse - the rest of the platform still has property
            # tests (``test_no_disk_secret_leak.py`` etc.) to catch
            # the failure at a coarser granularity.
            return True
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _redact_record(record: logging.LogRecord) -> None:
        """Apply :func:`redact_text` to ``record.msg`` and ``record.args``.

        When ``record.args`` is non-empty we render the message via
        :meth:`logging.LogRecord.getMessage` *after* redacting the
        args, then store the rendered string back on ``record.msg``
        and clear ``record.args``. This guarantees:

        * The secret never appears in the formatter input.
        * Subsequent ``record.getMessage()`` calls (eg. by additional
          handlers further down the chain) are stable - no double
          ``%``-substitution occurs because we cleared the args.
        """

        # --- 1. Redact each arg -----------------------------------------
        if record.args:
            record.args = _redact_args(record.args)

        # --- 2. Redact the raw msg / collapse into rendered form -------
        if isinstance(record.msg, str):
            if record.args:
                # Build the rendered message under the (already
                # redacted) args, then redact again to catch secret
                # patterns that span the ``%s`` boundary (eg.
                # ``logger.info("Authorization: Basic %s", blob)`` ⇒
                # the literal ``Authorization: Basic`` is in
                # ``record.msg`` and the blob is in ``record.args``;
                # the post-render redaction collapses both halves).
                try:
                    rendered = record.msg % record.args
                except (TypeError, ValueError):
                    # Misformatted log call - fall back to a plain
                    # join so we still emit *something*.
                    rendered = (
                        f"{record.msg} | args={record.args!r}"
                    )
                record.msg = redact_text(rendered)
                record.args = None
            else:
                record.msg = redact_text(record.msg)
        else:
            # Non-string ``msg`` (eg. an exception object). Coerce to
            # str, redact, store back as a string. Loses the original
            # type but logging treats ``msg`` as ``str`` downstream.
            try:
                record.msg = redact_text(str(record.msg))
            except Exception:  # noqa: BLE001
                # Leave it alone if str() blows up - the formatter will
                # raise instead and the record gets dropped at that
                # layer; no secret can leak through a non-string repr.
                return


def _redact_args(
    args: tuple[Any, ...] | dict[str, Any],
) -> tuple[Any, ...] | dict[str, Any]:
    """Return a copy of *args* with every string value redacted.

    Non-string args pass through unchanged - ``%d`` / ``%r`` consumers
    are unaffected. The returned object preserves the input shape
    (``tuple`` for positional, ``dict`` for keyword).
    """

    if isinstance(args, dict):
        return {
            k: (redact_text(v) if isinstance(v, str) else v)
            for k, v in args.items()
        }
    return tuple(
        redact_text(v) if isinstance(v, str) else v for v in args
    )


# ---------------------------------------------------------------------------
# Wiring helper
# ---------------------------------------------------------------------------


def install_redaction_filter(
    *,
    loggers: Iterable[logging.Logger] | None = None,
    attach_to_root: bool = True,
) -> RedactionFilter:
    """Attach a :class:`RedactionFilter` to the given loggers' handlers.

    This is the helper services call from their ``main.py`` entry
    point so every handler - including any created later by uvicorn /
    structlog / FastAPI - sees redacted records.

    Strategy
    --------

    1. Build a single :class:`RedactionFilter` instance.
    2. Attach it to every handler currently bound to the supplied
       *loggers* (iterating to preserve handler order). The filter is
       added at the *handler* level rather than the logger level so
       it survives :func:`logging.getLogger` ancestry traversal - a
       child logger that does not propagate to root still has its own
       handlers covered.
    3. Optionally attach to the root logger's handlers (default
       ``True``). ``logging.basicConfig()`` installs a single
       ``StreamHandler`` on the root logger, and uvicorn / FastAPI
       inherit from there, so the root coverage catches the common
       case with one call.
    4. Also add the filter directly to each logger object so log
       calls that reach a logger *without* handlers (eg. before
       ``basicConfig`` runs in tests) are still redacted before
       propagation to the root handler.

    Idempotency
    -----------

    Re-installing on the same logger / handler is harmless - the
    filter is added once per handler list because Python's
    ``logging.Filterer.addFilter`` performs an identity check on the
    filter list before adding. Repeated calls with *different*
    :class:`RedactionFilter` instances would stack but the redaction
    is itself idempotent so the stacked behaviour is still correct.

    Parameters
    ----------
    loggers:
        Loggers to attach the filter to. ``None`` (default) is
        equivalent to ``[]`` (only the root logger is touched, when
        ``attach_to_root`` is ``True``).
    attach_to_root:
        When ``True`` (default), attach the filter to
        :func:`logging.getLogger` (the root logger) and each of its
        handlers.

    Returns
    -------
    RedactionFilter
        The filter instance that was attached. Returned mainly so
        tests can introspect / detach it.
    """

    redactor = RedactionFilter()

    targets: list[logging.Logger] = list(loggers or [])
    if attach_to_root:
        targets.append(logging.getLogger())

    for logger in targets:
        # Attach to the logger itself so records produced *before* any
        # handler is installed are still redacted on propagation.
        if redactor not in logger.filters:
            logger.addFilter(redactor)
        # Attach to every currently-bound handler.
        for handler in logger.handlers:
            if redactor not in handler.filters:
                handler.addFilter(redactor)

    return redactor


__all__ = [
    "REDACTION_PATTERNS",
    "REDACTION_PLACEHOLDER",
    "RedactionFilter",
    "install_redaction_filter",
    "redact_text",
]
