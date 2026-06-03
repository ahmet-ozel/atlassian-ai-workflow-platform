"""Unit tests for :mod:`http_shared.redaction`.

The :class:`RedactionFilter` is the *log-call site* half of the
secret-hygiene story. ``test_log_redaction.py`` covers
the lifecycle-service log endpoint's ``KEY=<redacted>`` form for
arbitrary ``Sensitive_Env_Key`` values; this module pins down the
five concrete redaction patterns:

1. ``Authorization: Basic <...>`` (HTTP header echo).
2. ``Bearer <...>`` (OAuth / OIDC tokens).
3. ``api_token=<...>`` (Atlassian PAT echoes).
4. ``password=<...>`` (form bodies, exception messages).
5. ``secret=<...>`` (HMAC payloads, generic config dumps).

Each pattern is exercised through three surfaces:

* :func:`redact_text` — the pure helper; smoke-tests the regex set.
* :class:`RedactionFilter` mutating ``LogRecord.msg`` for f-string
  / pre-rendered log calls.
* :class:`RedactionFilter` mutating ``record.args`` for
  ``logger.info("got %s", token)`` style calls.

Plus auxiliary cases for idempotency, formatter integration, and
the :func:`install_redaction_filter` wiring helper.
"""

from __future__ import annotations

import io
import logging
from typing import Iterator

import pytest

from http_shared.redaction import (
    REDACTION_PATTERNS,
    REDACTION_PLACEHOLDER,
    RedactionFilter,
    install_redaction_filter,
    redact_text,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_logger(request: pytest.FixtureRequest) -> Iterator[logging.Logger]:
    """Fresh logger with a single :class:`io.StringIO` handler.

    Each test gets its own logger name (derived from the test's
    nodeid) so handlers / filters never bleed across tests. The
    logger is ``propagate=False`` so the root logger's handlers
    don't double-emit — we want to assert *exactly* what the test
    handler captures.
    """
    logger = logging.getLogger(f"test.redaction.{request.node.name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # Strip any handlers / filters left over from a prior run.
    logger.handlers.clear()
    logger.filters.clear()
    yield logger
    logger.handlers.clear()
    logger.filters.clear()


def _attach_capture(
    logger: logging.Logger, *, with_filter: bool = True
) -> io.StringIO:
    """Bind a :class:`StringIO` handler with a minimal ``%(message)s`` formatter."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    if with_filter:
        handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return buf


# ---------------------------------------------------------------------------
# redact_text — pure helper
# ---------------------------------------------------------------------------


class TestRedactText:
    """Direct exercise of :func:`redact_text` against the 5 patterns."""

    def test_authorization_basic_header(self) -> None:
        assert (
            redact_text("Authorization: Basic dXNlcjpwYXNz")
            == REDACTION_PLACEHOLDER
        )

    def test_authorization_basic_inline_in_prose(self) -> None:
        out = redact_text(
            "got 401 with Authorization: Basic abc123== from upstream"
        )
        assert "abc123" not in out
        assert "Basic" not in out  # whole "Basic <blob>" run is masked
        assert REDACTION_PLACEHOLDER in out
        # Prose context survives.
        assert out.startswith("got 401 with ")
        assert out.endswith(" from upstream")

    def test_authorization_basic_case_insensitive(self) -> None:
        # Some HTTP libraries title-case headers, others lowercase them.
        out = redact_text("authorization: basic deadbeef")
        assert "deadbeef" not in out
        assert REDACTION_PLACEHOLDER in out

    def test_bearer_token_standalone(self) -> None:
        assert redact_text("Bearer eyJhbGciOiJIUzI1NiJ9.foo.bar") == (
            REDACTION_PLACEHOLDER
        )

    def test_bearer_token_in_authorization_header(self) -> None:
        # Both the ``Authorization`` line *and* the bare ``Bearer`` form
        # collapse to a single sentinel — we don't care which pattern
        # matched first as long as the token bytes are gone.
        out = redact_text("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in out
        assert REDACTION_PLACEHOLDER in out

    def test_bearer_token_lowercase(self) -> None:
        out = redact_text("authorization: bearer xyz")
        assert "xyz" not in out

    def test_api_token_kv(self) -> None:
        out = redact_text("api_token=ATATT3xFfGF0secretvalue")
        assert "ATATT3xFfGF0secretvalue" not in out
        # Key is preserved (operator can see *which* credential masked).
        assert out.startswith("api_token=")
        assert out.endswith(REDACTION_PLACEHOLDER)

    def test_api_token_case_insensitive(self) -> None:
        out = redact_text("API_TOKEN=somevalue")
        assert "somevalue" not in out
        assert out.startswith("API_TOKEN=")
        assert REDACTION_PLACEHOLDER in out

    def test_password_kv(self) -> None:
        out = redact_text("password=hunter2")
        assert "hunter2" not in out
        assert out == f"password={REDACTION_PLACEHOLDER}"

    def test_password_kv_inline_with_other_kv(self) -> None:
        out = redact_text(
            "user=alice password=hunter2 host=db.local"
        )
        # Surrounding non-sensitive ``KEY=value`` tokens are pass-through.
        assert "user=alice" in out
        assert "host=db.local" in out
        # Password value is gone.
        assert "hunter2" not in out
        assert f"password={REDACTION_PLACEHOLDER}" in out

    def test_secret_kv(self) -> None:
        out = redact_text("secret=topsecret123")
        assert "topsecret123" not in out
        assert out == f"secret={REDACTION_PLACEHOLDER}"

    def test_empty_string_returns_empty(self) -> None:
        assert redact_text("") == ""

    def test_no_match_passes_through_unchanged(self) -> None:
        text = "user=alice host=db.local request_id=abc-123"
        assert redact_text(text) == text

    def test_idempotent(self) -> None:
        """Applying the redactor twice yields the same output as once."""
        text = (
            "Authorization: Basic deadbeef "
            "Bearer eyJhbGc.foo.bar "
            "api_token=ATATT123 "
            "password=hunter2 "
            "secret=topsecret"
        )
        once = redact_text(text)
        twice = redact_text(once)
        assert once == twice
        # And the placeholder itself is opaque to every pattern.
        assert (
            redact_text(REDACTION_PLACEHOLDER) == REDACTION_PLACEHOLDER
        )

    def test_all_five_patterns_in_one_line(self) -> None:
        out = redact_text(
            "headers={'Authorization: Basic AAA', 'X': 'Bearer BBB'} "
            "body='api_token=CCC&password=DDD&secret=EEE'"
        )
        for leaked in ("AAA", "BBB", "CCC", "DDD", "EEE"):
            assert leaked not in out, (
                f"{leaked!r} leaked through redactor; output={out!r}"
            )
        # Each KEY= form preserves its key name.
        assert "api_token=" in out
        assert "password=" in out
        assert "secret=" in out

    def test_pattern_count_matches_redaction_surface(self) -> None:
        """Pattern set expanded beyond the original 5: now 11 covers
        Bearer/Basic auth + KEY=VALUE forms (api_token/password/secret)
        + provider key shapes (sk-ant-*, ghp_*, AKIA*, etc.)."""
        assert len(REDACTION_PATTERNS) == 11


# ---------------------------------------------------------------------------
# RedactionFilter — record.msg path (no args)
# ---------------------------------------------------------------------------


class TestRedactionFilterMsgOnly:
    """``logger.info(f"...{token}")`` — secret arrives via ``record.msg``."""

    def test_msg_is_redacted_in_place(
        self, isolated_logger: logging.Logger
    ) -> None:
        buf = _attach_capture(isolated_logger)
        isolated_logger.info(
            "request failed with Authorization: Basic abc123== "
            "(api_token=tok-xyz, password=hunter2, secret=h)"
        )
        out = buf.getvalue()
        for leaked in ("abc123", "tok-xyz", "hunter2"):
            assert leaked not in out
        # ``secret=`` value 'h' is one char but still must be masked —
        # this is the regression case for ``\S+`` greedy matching.
        assert "secret=h\n" not in out
        assert REDACTION_PLACEHOLDER in out

    def test_non_secret_msg_passes_through(
        self, isolated_logger: logging.Logger
    ) -> None:
        buf = _attach_capture(isolated_logger)
        isolated_logger.info("ready: port=8080 worker_id=alice")
        assert buf.getvalue().rstrip("\n") == (
            "ready: port=8080 worker_id=alice"
        )

    def test_filter_returns_true(self) -> None:
        """A filter that drops records would silently lose log lines."""
        flt = RedactionFilter()
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="password=hunter2",
            args=None,
            exc_info=None,
        )
        assert flt.filter(record) is True
        # And the record was mutated in place.
        assert record.getMessage() == f"password={REDACTION_PLACEHOLDER}"


# ---------------------------------------------------------------------------
# RedactionFilter — record.args path (% formatting)
# ---------------------------------------------------------------------------


class TestRedactionFilterArgs:
    """``logger.info("got %s", token)`` — secret arrives via ``record.args``."""

    def test_string_arg_is_redacted(
        self, isolated_logger: logging.Logger
    ) -> None:
        buf = _attach_capture(isolated_logger)
        isolated_logger.info(
            "set header %s",
            "Authorization: Basic ZGVhZGJlZWY=",
        )
        out = buf.getvalue()
        assert "ZGVhZGJlZWY" not in out
        assert REDACTION_PLACEHOLDER in out
        assert out.startswith("set header ")

    def test_dict_args_redacted(
        self, isolated_logger: logging.Logger
    ) -> None:
        buf = _attach_capture(isolated_logger)
        # ``%(key)s`` style log call.
        isolated_logger.info(
            "user=%(user)s password=%(pw)s",
            {"user": "alice", "pw": "hunter2"},
        )
        out = buf.getvalue()
        # ``user=alice`` is a non-sensitive ``KEY=value`` token so it
        # is pass-through; ``password=hunter2`` is masked. We check
        # the rendered-string form because args were collapsed.
        assert "user=alice" in out
        assert "hunter2" not in out
        assert f"password={REDACTION_PLACEHOLDER}" in out

    def test_msg_template_and_arg_combined(
        self, isolated_logger: logging.Logger
    ) -> None:
        """Pattern that spans the template / arg boundary still redacts.

        The template carries the literal ``Authorization: Basic`` prefix
        and the arg carries the secret blob. Post-render the full
        ``Authorization: Basic <blob>`` run is matched by the
        ``Authorization`` pattern.
        """
        buf = _attach_capture(isolated_logger)
        isolated_logger.info("Authorization: Basic %s", "abc123==")
        out = buf.getvalue()
        assert "abc123" not in out
        assert REDACTION_PLACEHOLDER in out

    def test_non_string_args_pass_through(
        self, isolated_logger: logging.Logger
    ) -> None:
        buf = _attach_capture(isolated_logger)
        isolated_logger.info("processed %d items in %.2fs", 42, 1.234)
        out = buf.getvalue().rstrip("\n")
        assert out == "processed 42 items in 1.23s"

    def test_args_cleared_after_redaction(self) -> None:
        """The filter collapses args into msg so formatters don't re-substitute."""
        flt = RedactionFilter()
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="set %s",
            args=("password=hunter2",),
            exc_info=None,
        )
        flt.filter(record)
        # ``args`` is cleared so a downstream ``record.getMessage()``
        # call doesn't try to re-substitute (which would either fail or
        # leave a ``%s`` placeholder in the output).
        assert record.args is None
        assert "hunter2" not in record.getMessage()
        assert REDACTION_PLACEHOLDER in record.getMessage()


# ---------------------------------------------------------------------------
# install_redaction_filter — wiring helper
# ---------------------------------------------------------------------------


class TestInstallRedactionFilter:
    """The convenience helper that services call from ``main.py``."""

    def test_attaches_to_supplied_logger_handlers(
        self, isolated_logger: logging.Logger
    ) -> None:
        # Bind a capture handler *without* a pre-attached filter so we
        # can verify ``install_redaction_filter`` covers it.
        buf = _attach_capture(isolated_logger, with_filter=False)
        install_redaction_filter(
            loggers=[isolated_logger], attach_to_root=False
        )

        isolated_logger.info("password=hunter2 user=alice")
        out = buf.getvalue()
        assert "hunter2" not in out
        assert "user=alice" in out
        assert REDACTION_PLACEHOLDER in out

    def test_idempotent_install(
        self, isolated_logger: logging.Logger
    ) -> None:
        """Calling ``install_*`` twice with the same instance doesn't double-attach."""
        buf = _attach_capture(isolated_logger, with_filter=False)
        flt = install_redaction_filter(
            loggers=[isolated_logger], attach_to_root=False
        )
        # Second call with a *different* filter instance still works
        # — both filters are idempotent so the redaction is unchanged.
        install_redaction_filter(
            loggers=[isolated_logger], attach_to_root=False
        )
        # First install's handle is still attached.
        assert flt in isolated_logger.handlers[0].filters

        isolated_logger.info("password=hunter2")
        out = buf.getvalue()
        # Even with two filters stacked the output is a single redacted
        # line — the placeholder is opaque to every pattern.
        assert out.count(REDACTION_PLACEHOLDER) == 1
        assert "hunter2" not in out

    def test_returns_filter_instance(self) -> None:
        """Returned handle is the attached :class:`RedactionFilter`."""
        flt = install_redaction_filter(
            loggers=[], attach_to_root=False
        )
        assert isinstance(flt, RedactionFilter)


# ---------------------------------------------------------------------------
# Negative / robustness cases
# ---------------------------------------------------------------------------


class TestRobustness:
    """Filter must not raise, even on weird inputs."""

    def test_filter_does_not_raise_on_non_string_msg(self) -> None:
        flt = RedactionFilter()

        class _Weird:
            def __str__(self) -> str:
                return "password=leaked"

        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=_Weird(),  # type: ignore[arg-type]
            args=None,
            exc_info=None,
        )
        # The filter coerces non-string ``msg`` to string and redacts.
        assert flt.filter(record) is True
        assert "leaked" not in record.getMessage()

    def test_filter_does_not_raise_on_misformatted_log_call(self) -> None:
        """A log call with bad ``%`` substitution must still emit, redacted."""
        flt = RedactionFilter()
        # Two ``%s`` placeholders but only one positional arg → render
        # would normally raise ``TypeError``. The filter falls back to
        # appending the args repr and still redacts.
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="got %s and %s",
            args=("password=hunter2",),
            exc_info=None,
        )
        assert flt.filter(record) is True
        rendered = record.getMessage()
        assert "hunter2" not in rendered

    def test_filter_silently_passes_record_through_on_internal_error(
        self,
    ) -> None:
        """If the redactor itself blows up the record still gets emitted."""

        class BoomFilter(RedactionFilter):
            @staticmethod
            def _redact_record(record: logging.LogRecord) -> None:  # type: ignore[override]
                raise RuntimeError("boom")

        flt = BoomFilter()
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="anything",
            args=None,
            exc_info=None,
        )
        # Filter swallows the error and returns True so logging keeps
        # working. (The original record is unredacted but emitted.)
        assert flt.filter(record) is True
