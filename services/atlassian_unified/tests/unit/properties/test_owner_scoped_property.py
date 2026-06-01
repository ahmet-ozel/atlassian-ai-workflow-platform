"""Property test P5 — Owner-scoped delete resolves ownership before any DELETE.

Validates Requirements 15.3, 15.4, 46.1, 46.2 / design Property 5: the
owner-scoped delete path behind ``jira_delete_own_filter`` must resolve
the target filter's owner via a read endpoint, compare it to the
authenticated user through
:func:`mcp_atlassian.utils.dc_guards.require_owner`, and — on mismatch —
return a structured error with ``error_code == "not_filter_owner"`` while
issuing **zero** DELETE HTTP requests against
``/rest/api/2/filter/{filter_id}``.

Test shape
----------
The test exercises the mixin + guard composition directly rather than
the server tool: spinning up ``FastMCP`` + ``ConfluenceFetcher`` just to
observe the HTTP surface is heavy and adds bootstrap flake without
strengthening the invariant. The Python-level call path reproduced here
mirrors ``servers/jira.py::jira_delete_own_filter`` exactly:

    owner_name = jira.get_filter_owner_name(filter_id)
    owner_err  = dc_guards.require_owner(jira, owner_name)
    if owner_err is not None:
        return StructuredError(error_code="not_filter_owner", ...)
    jira.delete_filter(filter_id)

* **Property A (Hypothesis)** — for any ``(filter_owner,
  authenticated_user)`` pair where the two differ (case-insensitive,
  whitespace-stripped), the path yields a :class:`StructuredError` with
  ``error_code == "not_filter_owner"`` AND the mocked HTTP session
  records zero DELETE calls (exactly one GET for the owner lookup is
  expected).
* **Property B (smoke, parametrized)** — when the owner and the
  authenticated user match, the path issues exactly one DELETE (plus
  the one GET for owner resolution) and yields no structured error.

Mocking
-------
``FiltersMixin`` calls ``self.jira.get(path)`` (Atlassian Python client
wrapper — returns the JSON body directly) for owner resolution and
``self.jira.delete(path)`` for the DELETE. The shim wires both to the
shared ``mock_requests_session`` fixture so the conftest's
call-counting helpers (``assert_no_http_called``,
``assert_http_call_count``, ``assert_http_methods_called``) observe the
real tool surface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.jira.filters import FiltersMixin
from mcp_atlassian.utils import dc_guards


# ---------------------------------------------------------------------------
# Username strategy
# ---------------------------------------------------------------------------
#
# DC usernames are short, ASCII, and compared case-insensitively by
# ``require_owner`` (it strips whitespace and lower-cases both sides). We
# draw from a slightly wider alphabet than the canonical ``[A-Za-z0-9._-]``
# to exercise mixed-case + punctuation boundaries, but keep lengths
# bounded so Hypothesis shrinks stay tight.
_USERNAME_ALPHABET: str = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789._-"
)

usernames: st.SearchStrategy[str] = st.text(
    alphabet=_USERNAME_ALPHABET,
    min_size=1,
    max_size=24,
)


@st.composite
def _mismatched_username_pair(draw: st.DrawFn) -> tuple[str, str]:
    """Draw two usernames that differ under ``require_owner``'s compare.

    ``require_owner`` strips whitespace and lower-cases both sides before
    equality is tested, so we filter on that same normalization rather
    than raw equality. This ensures every generated pair actually
    triggers the mismatch branch (rather than coincidentally colliding
    after case-folding and producing a false negative).
    """
    filter_owner = draw(usernames)
    authenticated_user = draw(usernames)
    assume(filter_owner.strip().lower() != authenticated_user.strip().lower())
    # Also guard against the fail-closed ``not_owner`` path that fires
    # when either side is empty after stripping: those cases are still
    # valid mismatches but the property we care about here is the
    # ordinary "different user" mismatch, so keep both non-blank.
    assume(filter_owner.strip())
    assume(authenticated_user.strip())
    return filter_owner, authenticated_user


# ---------------------------------------------------------------------------
# Shim helpers
# ---------------------------------------------------------------------------


def _build_get_side_effect(filter_owner: str, filter_id: str) -> Any:
    """Return a side-effect for ``jira.get`` that serves the filter owner.

    ``FiltersMixin.get_filter_owner_name`` calls ``self.get_filter(...)``
    which in turn calls ``self.jira.get("rest/api/2/filter/{filter_id}")``
    and expects a dict body. We reproduce just enough of the DC schema
    (``{"owner": {"name": ..., "key": ...}}``) for the helper to return
    ``filter_owner`` verbatim.
    """

    expected_path = f"rest/api/2/filter/{filter_id}"

    def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Fail loudly if the mixin probes an unexpected endpoint so a
        # regression does not silently swallow the assertion.
        assert path == expected_path, (
            f"unexpected GET path: {path!r} (expected {expected_path!r})"
        )
        return {
            "id": filter_id,
            "owner": {"name": filter_owner, "key": filter_owner},
        }

    return _get


def _make_fake_self(
    mock_session: Any, filter_owner: str, authenticated_user: str, filter_id: str
) -> SimpleNamespace:
    """Build a ``self`` shim compatible with ``FiltersMixin`` + guards.

    Two contracts to satisfy:

    * ``FiltersMixin.get_filter`` / ``.delete_filter`` call
      ``self.jira.get`` / ``self.jira.delete``. We expose those as
      attributes on a ``SimpleNamespace`` wired to the conftest mock.
    * ``dc_guards.require_owner`` reads ``fetcher.config.username``. We
      expose a nested ``SimpleNamespace`` for that.

    The ``mock_session.get`` method is configured with a side-effect so
    the response shape depends on the input path; ``mock_session.delete``
    keeps its default ``MagicMock`` return value since the mixin ignores
    the body.
    """
    mock_session.get.side_effect = _build_get_side_effect(filter_owner, filter_id)

    fake_self = SimpleNamespace(
        jira=SimpleNamespace(
            get=mock_session.get,
            delete=mock_session.delete,
        ),
        config=SimpleNamespace(username=authenticated_user),
    )
    # ``get_filter_owner_name`` delegates to ``self.get_filter(...)`` (another
    # method on the same mixin). Since we're invoking the mixin as an unbound
    # function against a ``SimpleNamespace``, bind ``get_filter`` explicitly
    # so the inner call resolves against our shim rather than raising
    # ``AttributeError``.
    fake_self.get_filter = lambda fid: FiltersMixin.get_filter(fake_self, fid)
    return fake_self


def _run_owner_scoped_delete(
    fake_self: SimpleNamespace, filter_id: str
) -> dc_guards.StructuredError | None:
    """Reproduce ``servers/jira.py::jira_delete_own_filter`` in Python.

    Mirrors the exact call order used by the server tool so the
    "zero DELETE on mismatch" invariant is tested against the real
    guard composition rather than a simplified stand-in. Returns:

    * ``None`` on the happy path, after the DELETE has been issued.
    * A :class:`StructuredError` with ``error_code="not_filter_owner"``
      on the owner-mismatch branch, **without** issuing the DELETE.
    """
    # 1. Resolve filter owner (single read-side HTTP call).
    owner_name = FiltersMixin.get_filter_owner_name(fake_self, filter_id)

    # 2. Owner gate. Map ``not_owner`` → ``not_filter_owner`` exactly as
    # the server tool does, preserving the ``details`` payload.
    owner_err = dc_guards.require_owner(fake_self, owner_name)
    if owner_err is not None:
        return dc_guards.StructuredError(
            error_code="not_filter_owner",
            message=(
                f"Authenticated user is not the owner of filter "
                f"{filter_id!r}; DELETE blocked."
            ),
            details={**owner_err.details, "filter_id": filter_id},
        )

    # 3. Issue the DELETE only after the owner gate passes.
    FiltersMixin.delete_filter(fake_self, filter_id)
    return None


# ---------------------------------------------------------------------------
# Property A — mismatched owner yields not_filter_owner, zero DELETEs
# ---------------------------------------------------------------------------


@given(pair=_mismatched_username_pair(), filter_id=st.integers(min_value=1, max_value=999999).map(str))
def test_owner_mismatch_blocks_delete_with_not_filter_owner(
    mock_requests_session, pair: tuple[str, str], filter_id: str
) -> None:
    """P5.A: owner ≠ authenticated user → ``not_filter_owner`` + zero DELETE."""
    # Reset between Hypothesis examples so call-count assertions are
    # meaningful for *this* example rather than cumulative across the run.
    mock_requests_session.reset_mock(side_effect=True)

    filter_owner, authenticated_user = pair
    fake_self = _make_fake_self(
        mock_requests_session, filter_owner, authenticated_user, filter_id
    )

    result = _run_owner_scoped_delete(fake_self, filter_id)

    # Structured-error contract — the server tool maps to ``not_filter_owner``.
    assert isinstance(result, dc_guards.StructuredError), (
        f"expected StructuredError on owner mismatch, got {result!r}"
    )
    assert result.error_code == "not_filter_owner", (
        f"expected error_code='not_filter_owner', got {result.error_code!r}"
    )
    assert result.details.get("filter_id") == filter_id
    assert result.details.get("object_owner_id") == filter_owner
    assert result.details.get("authenticated_user") == authenticated_user

    # HTTP-surface contract — exactly one GET (owner lookup) and zero
    # DELETEs. The "zero DELETE" half is the core safety property.
    assert mock_requests_session.delete.call_count == 0, (
        f"expected zero DELETE calls on owner mismatch, got "
        f"{mock_requests_session.delete.call_count}"
    )
    mock_requests_session.assert_http_call_count(1)
    mock_requests_session.assert_http_methods_called({"get"})


# ---------------------------------------------------------------------------
# Property B (smoke) — matching owner proceeds to exactly one DELETE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "filter_id"),
    [
        ("alice", "10001"),
        # Case-insensitive match — ``require_owner`` lower-cases both sides,
        # so a ``BOB`` owner with ``bob`` authenticated must still pass.
        ("BOB", "42"),
    ],
)
def test_matching_owner_issues_exactly_one_delete(
    mock_requests_session, username: str, filter_id: str
) -> None:
    """P5.B: owner == authenticated user → one GET + one DELETE, no error."""
    # Use the matching username for both sides (mixed case in one example
    # to exercise the case-insensitive compare).
    fake_self = _make_fake_self(
        mock_requests_session, username, username.lower(), filter_id
    )

    result = _run_owner_scoped_delete(fake_self, filter_id)

    assert result is None, f"expected happy-path (no error), got {result!r}"

    # Exactly one GET (owner lookup) + exactly one DELETE (the deletion).
    assert mock_requests_session.get.call_count == 1
    assert mock_requests_session.delete.call_count == 1
    mock_requests_session.assert_http_call_count(2)
    mock_requests_session.assert_http_methods_called({"get", "delete"})

    # Sanity-check the DELETE targeted the expected endpoint.
    (delete_args, _delete_kwargs) = mock_requests_session.delete.call_args
    assert delete_args == (f"rest/api/2/filter/{filter_id}",)
