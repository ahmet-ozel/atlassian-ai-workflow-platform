"""Shared configuration for property-based tests.

This conftest registers two Hypothesis profiles and exposes common fixtures
and tool-argument strategies used across the PBT suite for the DC tool
parity feature.

Profiles
--------
* ``default`` — fast local feedback: ``max_examples=100``, no deadline,
  verbose on shrink, ``report_multiple_bugs`` disabled so CI output is
  focused on the first minimal counter-example.
* ``ci`` — deterministic reproducible runs: ``max_examples=200``,
  ``derandomize=True`` (seeded from the database), no deadline.

The active profile is selected by the ``HYPOTHESIS_PROFILE`` environment
variable (defaulting to ``default``).

Shared fixtures
---------------
* ``mock_requests_session`` — a ``MagicMock`` shaped like
  ``requests.Session`` with helper assertions for HTTP call counts.
* ``mock_httpx_client`` — the ``httpx.Client`` equivalent.

Tool-argument strategies are exported as module-level ``st.*`` constants so
individual property tests can compose them without re-declaring shared
bounds::

    from tests.unit.properties.conftest import (
        project_keys,
        space_keys,
        webhook_urls,
        jql_snippets,
        cql_fragments,
    )
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Hypothesis profile registration
# ---------------------------------------------------------------------------

# Suppress the "function-scoped fixture" health check so tests that combine
# pytest fixtures with `@given` decorators don't trip the default guard.
_COMMON_SUPPRESS = (HealthCheck.function_scoped_fixture,)

settings.register_profile(
    "default",
    max_examples=100,
    deadline=None,
    suppress_health_check=_COMMON_SUPPRESS,
)

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    print_blob=True,
    suppress_health_check=_COMMON_SUPPRESS,
)

_ACTIVE_PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "default")
settings.load_profile(_ACTIVE_PROFILE)


# ---------------------------------------------------------------------------
# Tool-argument strategies
# ---------------------------------------------------------------------------

# Project keys — Jira / Bitbucket convention: 2–10 uppercase ASCII letters.
# Kept intentionally narrow; project-filter logic uppercases before compare,
# so mixed-case variants are tested explicitly in a dedicated property.
project_keys: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("Z")),
    min_size=2,
    max_size=10,
)

# Space keys — Confluence convention: same shape as project keys.
space_keys: st.SearchStrategy[str] = project_keys

# A small, fixed pool of realistic webhook host names. Keeping the pool
# bounded avoids combinatorial explosion while still exercising path and
# query-component diversity.
_WEBHOOK_HOSTS: tuple[str, ...] = (
    "hooks.example.com",
    "ci.internal.corp",
    "build.atlassian.local",
    "events.example.org",
    "listener.test",
)

_WEBHOOK_PATHS: tuple[str, ...] = (
    "/webhook",
    "/bitbucket/events",
    "/hooks/incoming",
    "/api/v1/bb",
    "/events/push",
)


@st.composite
def _webhook_url(draw: st.DrawFn) -> str:
    host = draw(st.sampled_from(_WEBHOOK_HOSTS))
    path = draw(st.sampled_from(_WEBHOOK_PATHS))
    return f"https://{host}{path}"


webhook_urls: st.SearchStrategy[str] = _webhook_url()

# JQL snippets drawn from a fixed pool. Property tests over JQL focus on
# tool-layer validation (e.g. read_only, filter prechecks), not on JQL
# parsing itself, so a curated pool is sufficient.
jql_snippets: st.SearchStrategy[str] = st.sampled_from(
    (
        "project = TEST",
        "project = TEST AND status = Open",
        'assignee = currentUser() AND resolution is EMPTY',
        "created >= -7d",
        "labels in (bug, regression)",
        "status changed to Done during (-14d, now())",
        'text ~ "needle"',
    )
)

# CQL fragments drawn from a fixed pool, used primarily by the CQL order_by
# and space-filter properties.
cql_fragments: st.SearchStrategy[str] = st.sampled_from(
    (
        "space = DEV",
        "space = DEV AND type = page",
        'title ~ "release notes"',
        "lastmodified >= now('-7d')",
        "type = blogpost AND space in (DEV, OPS)",
        "creator = currentUser()",
        'label = "needs-review"',
    )
)


# ---------------------------------------------------------------------------
# Mock HTTP-session fixtures
# ---------------------------------------------------------------------------


class _CallCountingMock(MagicMock):
    """`MagicMock` subclass with ergonomic HTTP call-count helpers.

    Provides assertions that are specifically meaningful for the DC guard
    properties: e.g. "no outbound HTTP occurred when a precheck rejected
    the call".
    """

    _HTTP_METHODS: tuple[str, ...] = (
        "request",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "send",
    )

    def _http_call_count(self) -> int:
        total = 0
        for name in self._HTTP_METHODS:
            # MagicMock auto-creates child mocks on attribute access, so
            # `call_count` is always available (defaults to 0).
            total += getattr(self, name).call_count
        return total

    def assert_no_http_called(self) -> None:
        """Assert no HTTP-shaped method was called on this session."""
        count = self._http_call_count()
        if count != 0:
            offenders = {
                name: getattr(self, name).call_count
                for name in self._HTTP_METHODS
                if getattr(self, name).call_count
            }
            raise AssertionError(
                f"Expected zero HTTP calls, got {count}: {offenders!r}"
            )

    def assert_http_call_count(self, expected: int) -> None:
        """Assert the total number of HTTP calls equals ``expected``."""
        count = self._http_call_count()
        if count != expected:
            raise AssertionError(
                f"Expected {expected} HTTP calls, got {count}"
            )

    def assert_http_methods_called(self, methods: Iterable[str]) -> None:
        """Assert that exactly the given HTTP methods were invoked."""
        wanted = {m.lower() for m in methods}
        seen = {
            name
            for name in self._HTTP_METHODS
            if getattr(self, name).call_count
        }
        if seen != wanted:
            raise AssertionError(
                f"Expected HTTP methods {sorted(wanted)!r}, saw {sorted(seen)!r}"
            )


def _make_session_mock(spec_name: str) -> _CallCountingMock:
    mock = _CallCountingMock(name=spec_name)
    # Pre-seed an empty Response-ish return value so tests that *do* expect
    # an HTTP call can inspect `.status_code` / `.json()` without extra
    # setup. Tests that need different responses override these directly.
    response = MagicMock(name=f"{spec_name}.response")
    response.status_code = 200
    response.json.return_value = {}
    response.headers = {}
    for method in _CallCountingMock._HTTP_METHODS:
        getattr(mock, method).return_value = response
    return mock


@pytest.fixture
def mock_requests_session() -> _CallCountingMock:
    """Return a `requests.Session`-shaped mock with call-count helpers."""
    return _make_session_mock("requests.Session")


@pytest.fixture
def mock_httpx_client() -> _CallCountingMock:
    """Return an `httpx.Client`-shaped mock with call-count helpers."""
    return _make_session_mock("httpx.Client")


# ---------------------------------------------------------------------------
# Re-exports for explicit imports
# ---------------------------------------------------------------------------

__all__: tuple[str, ...] = (
    "project_keys",
    "space_keys",
    "webhook_urls",
    "jql_snippets",
    "cql_fragments",
    "mock_requests_session",
    "mock_httpx_client",
)


# Ensure type-only symbol usage remains importable for mypy without a
# runtime cost; `Any` is re-exported indirectly through the helpers above.
_ = Any
