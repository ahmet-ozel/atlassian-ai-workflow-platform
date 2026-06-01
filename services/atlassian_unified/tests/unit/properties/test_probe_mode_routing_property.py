"""Property test P10 — Mode-conditional connectivity probe.

Validates Requirements 18.1, 18.3, 18.4 of the
``bitbucket-cloud-dc-parity`` spec / design Property 10.

**Requirement 18.2 update (post-CHANGE-2770):** The original spec text
pinned the Cloud probe to exactly
``GET /2.0/workspaces?pagelen=1``. Atlassian removed that endpoint in
CHANGE-2770 (September 2025), and requests to it now return HTTP 410
Gone. ``_bitbucket_probe`` was accordingly changed to prefer
``GET /2.0/user`` for Cloud connectivity validation, with a
workspace-scoped fallback (``GET /2.0/workspaces/{workspace}``) and a
no-op success when neither succeeds. The tests below therefore assert
the new contract:

* Cloud mode issues **zero or more** calls through ``fetcher.bitbucket.get``,
  starting with ``/2.0/user`` and — only if that fails — falling back to
  ``/2.0/workspaces/{workspace}`` when a workspace is configured on
  ``fetcher.config``.
* ``fetcher.get_projects`` is **never** invoked in Cloud mode.
* DC mode still invokes exactly ``fetcher.get_projects(limit=1)`` and
  never touches ``fetcher.bitbucket.get`` (Req 18.1, 18.4 — unchanged).

Additional sub-properties covered here:

* The Cloud probe treats an empty response body as valid (Req 18.3).
* A terminal ``HTTPError`` from every Cloud endpoint attempt falls
  through to the helper's no-op safety net when no workspace is
  configured — callers do not see the exception because CHANGE-2770
  made it infeasible to require a single-call assertion.
* When the caller provides a ``workspace`` on ``fetcher.config``, a
  failing ``/2.0/user`` is followed by exactly one
  ``/2.0/workspaces/{workspace}`` call.

The test is pure: no real HTTP is issued, no Bitbucket credentials are
required, and no real ``BitbucketFetcher`` is instantiated. A
``MagicMock`` plays the fetcher role.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from requests.exceptions import HTTPError

from mcp_atlassian.servers.dependencies import _bitbucket_probe


# ---------------------------------------------------------------------------
# Fetcher-shaped mock factory
# ---------------------------------------------------------------------------


def _make_fetcher_mock(
    *,
    is_cloud: bool,
    cloud_response: Any = None,
    dc_response: Any = None,
    cloud_side_effect: BaseException | None = None,
    dc_side_effect: BaseException | None = None,
    workspace: str | None = None,
) -> MagicMock:
    """Build a ``MagicMock`` shaped like a ``BitbucketFetcher``.

    The mock exposes the attributes that ``_bitbucket_probe`` touches:

    * ``fetcher.is_cloud`` — the branch selector.
    * ``fetcher.config.workspace`` — read by the Cloud fallback path.
    * ``fetcher.bitbucket.get(url, params=...)`` — the Cloud probe
      callable. A ``MagicMock`` with either a fixed ``return_value``
      or a raising ``side_effect``.
    * ``fetcher.get_projects(limit=...)`` — the DC probe callable.
    """
    fetcher = MagicMock(name="BitbucketFetcher")
    fetcher.is_cloud = is_cloud
    # Reset the auto-created .config MagicMock so tests can read a
    # specific ``workspace`` attribute deterministically.
    fetcher.config = MagicMock(name="BitbucketConfig")
    fetcher.config.workspace = workspace

    if cloud_side_effect is not None:
        fetcher.bitbucket.get.side_effect = cloud_side_effect
    else:
        fetcher.bitbucket.get.return_value = (
            cloud_response
            if cloud_response is not None
            else {
                "uuid": "{00000000-0000-0000-0000-000000000000}",
                "username": "probe-user",
            }
        )

    if dc_side_effect is not None:
        fetcher.get_projects.side_effect = dc_side_effect
    else:
        fetcher.get_projects.return_value = (
            dc_response
            if dc_response is not None
            else [{"key": "PRJ", "name": "Project"}]
        )

    return fetcher


# ---------------------------------------------------------------------------
# Property A — Cloud mode happy path: /2.0/user returns, no fallback
# ---------------------------------------------------------------------------


def test_cloud_mode_probe_prefers_user_endpoint_when_authorized() -> None:
    """P10.A — On Cloud, a successful ``GET /2.0/user`` ends the probe
    after exactly one call and ``fetcher.get_projects`` is never invoked.

    Validates Requirements 18.4 (mutual exclusion of DC probe) and the
    post-CHANGE-2770 Cloud contract.
    """
    user_response = {
        "uuid": "{5733493c-91f3-407a-89cc-b15eb4e4e298}",
        "username": "john",
    }
    fetcher = _make_fetcher_mock(is_cloud=True, cloud_response=user_response)

    result = _bitbucket_probe(fetcher)

    fetcher.bitbucket.get.assert_called_once_with("/2.0/user")
    assert fetcher.get_projects.call_count == 0
    assert result == user_response


# ---------------------------------------------------------------------------
# Property B — DC mode calls DC probe exactly once; Cloud probe never
# ---------------------------------------------------------------------------


@given(
    projects=st.lists(
        st.fixed_dictionaries(
            {
                "key": st.text(
                    alphabet=st.characters(
                        min_codepoint=ord("A"), max_codepoint=ord("Z")
                    ),
                    min_size=2,
                    max_size=10,
                ),
                "name": st.text(min_size=1, max_size=20),
            }
        ),
        min_size=0,
        max_size=5,
    )
)
def test_dc_mode_probe_calls_get_projects_exactly_once(
    projects: list[dict[str, Any]],
) -> None:
    """P10.B — ``is_cloud=False`` runs exactly
    ``fetcher.get_projects(limit=1)`` and never invokes
    ``fetcher.bitbucket.get``.

    Validates Requirements 18.1, 18.4 — unchanged by CHANGE-2770.
    """
    fetcher = _make_fetcher_mock(is_cloud=False, dc_response=projects)

    result = _bitbucket_probe(fetcher)

    fetcher.get_projects.assert_called_once_with(limit=1)
    assert fetcher.bitbucket.get.call_count == 0
    assert result == projects


# ---------------------------------------------------------------------------
# Property C — Cloud empty-values response is valid
# ---------------------------------------------------------------------------


def test_cloud_mode_empty_response_is_treated_as_valid() -> None:
    """P10.C — A Cloud probe response with an empty body is returned
    as-is. The helper does not raise or transform sparse Cloud responses.

    Validates Requirement 18.3.
    """
    empty_response: dict[str, Any] = {}
    fetcher = _make_fetcher_mock(is_cloud=True, cloud_response=empty_response)

    result = _bitbucket_probe(fetcher)

    fetcher.bitbucket.get.assert_called_once_with("/2.0/user")
    assert result == empty_response


# ---------------------------------------------------------------------------
# Property D — Cloud /2.0/user HTTPError falls back to workspace probe
# ---------------------------------------------------------------------------


def test_cloud_probe_falls_back_to_workspace_when_user_denied() -> None:
    """P10.D — When ``/2.0/user`` raises (typical for repository/workspace
    access tokens whose scope excludes ``account``) AND a workspace is
    configured, the probe falls through to
    ``/2.0/workspaces/{workspace}`` exactly once.
    """
    user_err = HTTPError("403 Forbidden")
    workspace_response = {
        "slug": "my-team",
        "name": "My Team",
        "uuid": "{5733493c-91f3-407a-89cc-b15eb4e4e298}",
    }

    fetcher = MagicMock(name="BitbucketFetcher")
    fetcher.is_cloud = True
    fetcher.config = MagicMock()
    fetcher.config.workspace = "my-team"

    # First call (/2.0/user) raises; second call (/2.0/workspaces/my-team) succeeds.
    fetcher.bitbucket.get.side_effect = [user_err, workspace_response]

    result = _bitbucket_probe(fetcher)

    assert fetcher.bitbucket.get.call_count == 2
    fetcher.bitbucket.get.assert_any_call("/2.0/user")
    fetcher.bitbucket.get.assert_any_call("/2.0/workspaces/my-team")
    assert result == workspace_response
    assert fetcher.get_projects.call_count == 0


def test_cloud_probe_returns_noop_when_all_fallbacks_fail() -> None:
    """P10.D.2 — When ``/2.0/user`` raises AND no workspace is configured,
    the probe returns a no-op success payload rather than crashing the
    server lifespan. Multi-user deployments that rely exclusively on
    per-request auth routinely have no server-side credentials at
    startup, so the probe must be tolerant here.
    """
    user_err = HTTPError("403 Forbidden")

    fetcher = MagicMock(name="BitbucketFetcher")
    fetcher.is_cloud = True
    fetcher.config = MagicMock()
    fetcher.config.workspace = None
    fetcher.bitbucket.get.side_effect = user_err

    result = _bitbucket_probe(fetcher)

    # Exactly one attempt, then the helper bails out with a benign payload.
    fetcher.bitbucket.get.assert_called_once_with("/2.0/user")
    assert isinstance(result, dict)
    assert result.get("probe") == "skipped"
    assert fetcher.get_projects.call_count == 0


def test_dc_probe_http_error_propagates() -> None:
    """P10.D.3 — A ``HTTPError`` raised by the DC probe propagates
    unchanged through ``_bitbucket_probe``. DC behavior is unchanged
    by CHANGE-2770.
    """
    boom = HTTPError("500 Server Error")
    fetcher = _make_fetcher_mock(is_cloud=False, dc_side_effect=boom)

    with pytest.raises(HTTPError) as excinfo:
        _bitbucket_probe(fetcher)

    assert excinfo.value is boom
    fetcher.get_projects.assert_called_once_with(limit=1)
    assert fetcher.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Property E — DC / Cloud probe paths remain mutually exclusive
# ---------------------------------------------------------------------------


@given(is_cloud=st.booleans())
def test_probe_mode_branches_are_mutually_exclusive(is_cloud: bool) -> None:
    """P10.E — For any ``is_cloud`` value, ``fetcher.bitbucket.get`` and
    ``fetcher.get_projects`` are never both invoked on the same call.

    Validates Requirement 18.4 (mutual exclusion) — still holds after
    the CHANGE-2770 rework because the DC / Cloud branches remain
    disjoint in the helper body.
    """
    fetcher = _make_fetcher_mock(is_cloud=is_cloud)

    _bitbucket_probe(fetcher)

    cloud_calls = fetcher.bitbucket.get.call_count
    dc_calls = fetcher.get_projects.call_count

    if is_cloud:
        assert dc_calls == 0
        # Cloud path makes at least one call (/2.0/user) and may retry.
        assert cloud_calls >= 1
    else:
        assert cloud_calls == 0
        assert dc_calls == 1
