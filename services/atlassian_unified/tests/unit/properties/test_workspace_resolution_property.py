"""Property test P2 — Workspace resolution invariants.

Validates Requirements 2.1, 2.3, 2.4, 2.5, 2.6 of the
``bitbucket-cloud-dc-parity`` spec / design Property 2.

Two resolution surfaces are exercised here:

1. The ``_resolve_workspace(project_key, config_workspace)`` helper
   (imported from :mod:`mcp_atlassian.bitbucket.branches`, but every
   mode-branching mixin defines an identical copy) that picks the Cloud
   workspace for a per-request HTTP call:

       * A **non-empty** ``project_key`` argument always wins
         (Req 2.4).
       * Otherwise a **non-empty** ``config_workspace`` falls through
         (Req 2.5).
       * When **both** are empty/``None`` the helper raises
         :class:`ValueError` whose message starts with
         ``"filtered_out:"`` so the server layer can map it onto a
         structured ``filtered_out`` error before any outbound HTTP
         (Req 2.6).

2. :meth:`BitbucketConfig.from_env` URL-path parsing — when
   ``BITBUCKET_WORKSPACE`` is unset and ``BITBUCKET_URL`` points at a
   tenant-rooted Cloud host (``bitbucket.org`` or a subdomain of
   ``.bitbucket.org``), the first non-empty path segment populates
   :attr:`BitbucketConfig.workspace` (Req 2.1, 2.3). The
   ``api.bitbucket.org`` host carries REST paths — not workspace slugs
   — so the workspace stays ``None`` there.

The test is pure: no HTTP is issued, no ``BitbucketClient`` is
constructed. Env-var isolation is handled through pytest's
``monkeypatch`` fixture so developer-machine env vars cannot leak into
the truth table.

**Validates: Requirements 2.1, 2.3, 2.4, 2.5, 2.6**
"""

from __future__ import annotations

import string

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.branches import _resolve_workspace
from mcp_atlassian.bitbucket.config import BitbucketConfig


# ---------------------------------------------------------------------------
# Env isolation — mirror tests/unit/bitbucket/test_config_is_cloud.py
# ---------------------------------------------------------------------------


# ``BitbucketConfig.from_env`` reads this set of Bitbucket env vars. We
# clear every one of them before each Hypothesis example runs so a stray
# developer-machine value cannot contaminate the property. Fallback
# ``HTTP_PROXY`` / ``HTTPS_PROXY`` style vars are also cleared since
# ``from_env`` consults them.
_BITBUCKET_ENV_VARS: tuple[str, ...] = (
    "BITBUCKET_URL",
    "BITBUCKET_USERNAME",
    "BITBUCKET_PASSWORD",
    "BITBUCKET_PERSONAL_TOKEN",
    "BITBUCKET_API_TOKEN",
    "BITBUCKET_APP_PASSWORD",
    "BITBUCKET_CLOUD_ACCESS_TOKEN",
    "BITBUCKET_WORKSPACE",
    "BITBUCKET_SSL_VERIFY",
    "BITBUCKET_PROJECTS_FILTER",
    "BITBUCKET_HTTP_PROXY",
    "BITBUCKET_HTTPS_PROXY",
    "BITBUCKET_NO_PROXY",
    "BITBUCKET_SOCKS_PROXY",
    "BITBUCKET_CUSTOM_HEADERS",
    "BITBUCKET_CLIENT_CERT",
    "BITBUCKET_CLIENT_KEY",
    "BITBUCKET_CLIENT_KEY_PASSWORD",
    "BITBUCKET_TIMEOUT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SOCKS_PROXY",
)


@pytest.fixture(autouse=True)
def _clear_bitbucket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every Bitbucket env var before each Hypothesis example runs."""
    for name in _BITBUCKET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Shared primitive strategies
# ---------------------------------------------------------------------------


# Workspace and project-key slugs follow a narrow alphabet that matches
# Bitbucket's conventions (lowercase ASCII + digits + ``-._``) while
# excluding ``/`` so the URL path-parsing tests can safely embed them
# in ``BITBUCKET_URL`` without changing segment count.
_SLUG_ALPHABET = string.ascii_lowercase + string.digits + "-._"

# Non-empty workspace / project-key slugs. ``min_size=1`` ensures the
# "non-empty" truthiness branch in ``_resolve_workspace`` is exercised.
non_empty_slugs: st.SearchStrategy[str] = st.text(
    alphabet=_SLUG_ALPHABET,
    min_size=1,
    max_size=20,
)

# Values that must be treated as "empty" by the falsy-check in
# ``_resolve_workspace`` — Python considers all of these falsy, so the
# helper should skip them regardless of which parameter they occupy.
empty_values: st.SearchStrategy[str | None] = st.sampled_from(("", None))

# Cloud URL hosts that carry a workspace slug as their first path
# segment. ``api.bitbucket.org`` is EXCLUDED because its path is a REST
# API path, not a workspace slug (Req 2.3 path-parsing exemption).
_TENANT_ROOTED_HOSTS: tuple[str, ...] = (
    "bitbucket.org",
    "www.bitbucket.org",
    "myteam.bitbucket.org",
    "staging.bitbucket.org",
)


# ---------------------------------------------------------------------------
# Property A — Non-empty project_key always wins  (Requirement 2.4)
# ---------------------------------------------------------------------------


@given(
    project_key=non_empty_slugs,
    config_workspace=st.one_of(non_empty_slugs, empty_values),
)
def test_non_empty_project_key_always_wins(
    project_key: str,
    config_workspace: str | None,
) -> None:
    """P2.A — A non-empty ``project_key`` argument is returned verbatim
    regardless of whether ``config_workspace`` is set.

    Validates: Requirement 2.4.
    """
    result = _resolve_workspace(project_key, config_workspace)

    assert result == project_key


# ---------------------------------------------------------------------------
# Property B — Empty project_key falls through to config_workspace  (Req 2.5)
# ---------------------------------------------------------------------------


@given(
    project_key=empty_values,
    config_workspace=non_empty_slugs,
)
def test_empty_project_key_falls_through_to_config_workspace(
    project_key: str | None,
    config_workspace: str,
) -> None:
    """P2.B — An empty/``None`` ``project_key`` yields the
    ``config_workspace`` value.

    Validates: Requirement 2.5.
    """
    result = _resolve_workspace(project_key, config_workspace)

    assert result == config_workspace


# ---------------------------------------------------------------------------
# Property C — Both empty raises filtered_out ValueError  (Requirement 2.6)
# ---------------------------------------------------------------------------


@given(
    project_key=empty_values,
    config_workspace=empty_values,
)
def test_both_empty_raises_filtered_out_value_error(
    project_key: str | None,
    config_workspace: str | None,
) -> None:
    """P2.C — Both arguments empty ⇒ ``ValueError`` whose message starts
    with ``filtered_out:`` so the server layer can map it to a structured
    ``filtered_out`` error before any outbound HTTP call.

    Validates: Requirement 2.6.
    """
    with pytest.raises(ValueError) as excinfo:
        _resolve_workspace(project_key, config_workspace)

    # The prefix is the contract between this helper and the server
    # layer's error-mapping code; pin it explicitly.
    assert str(excinfo.value).startswith("filtered_out:")


# ---------------------------------------------------------------------------
# Property D — URL-path parsing populates workspace when env unset  (Req 2.1, 2.3)
# ---------------------------------------------------------------------------


@given(
    host=st.sampled_from(_TENANT_ROOTED_HOSTS),
    workspace_slug=non_empty_slugs,
    extra_path=st.sampled_from(("", "/", "/repo", "/repo/src/main/README.md")),
)
def test_url_path_parsing_populates_workspace_when_env_unset(
    host: str,
    workspace_slug: str,
    extra_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.D — ``from_env`` extracts the first URL path segment into
    :attr:`BitbucketConfig.workspace` when ``BITBUCKET_WORKSPACE`` is
    unset and the host is tenant-rooted (``bitbucket.org`` or a
    ``.bitbucket.org`` subdomain).

    Validates: Requirements 2.1, 2.3.
    """
    # ``urllib.parse.urlparse`` splits path on ``/``; our slug alphabet
    # excludes ``/`` already, so the workspace segment stays intact.
    # We assume the slug is not a URL-reserved token that the parser
    # would reinterpret — the slug alphabet guarantees this, but we add
    # an explicit assume() for safety so Hypothesis can shrink cleanly.
    assume("/" not in workspace_slug)

    url = f"https://{host}/{workspace_slug}{extra_path}"
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")
    # BITBUCKET_WORKSPACE intentionally left unset so path parsing runs.

    cfg = BitbucketConfig.from_env()

    assert cfg.is_cloud is True
    # The first path segment after the host populates the workspace
    # attribute (Req 2.3).
    assert cfg.workspace == workspace_slug
    # Req 2.1 — ``workspace`` is exposed as an attribute on the config.
    assert hasattr(cfg, "workspace")


# ---------------------------------------------------------------------------
# Property E — api.bitbucket.org URLs never populate workspace from path
# ---------------------------------------------------------------------------
#
# ``api.bitbucket.org`` URLs carry REST API paths (e.g.
# ``/2.0/repositories``), not workspace slugs, so the path-parsing
# branch must NOT fire on that host even when the env var is unset
# (design Section "Workspace resolution", Req 2.3 exemption).


@given(
    path=st.sampled_from(
        (
            "",
            "/",
            "/2.0",
            "/2.0/workspaces",
            "/2.0/repositories",
            "/2.0/repositories/some-team",
        )
    ),
)
def test_api_host_never_populates_workspace_from_path(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.E — ``api.bitbucket.org`` URLs never populate ``workspace``
    from the URL path even when ``BITBUCKET_WORKSPACE`` is unset.

    The first path segment on ``api.bitbucket.org`` is a REST API
    version (``2.0``), not a workspace slug; treating it as a workspace
    would produce nonsensical outbound URLs.

    Validates: Requirement 2.3 (path-parsing exemption).
    """
    url = f"https://api.bitbucket.org{path}"
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

    cfg = BitbucketConfig.from_env()

    assert cfg.is_cloud is True
    assert cfg.workspace is None


# ---------------------------------------------------------------------------
# Property F — BITBUCKET_WORKSPACE env var always wins over URL-path parsing
# ---------------------------------------------------------------------------


@given(
    host=st.sampled_from(_TENANT_ROOTED_HOSTS),
    url_workspace=non_empty_slugs,
    env_workspace=non_empty_slugs,
)
def test_env_workspace_wins_over_url_path(
    host: str,
    url_workspace: str,
    env_workspace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.F — When both ``BITBUCKET_WORKSPACE`` and a URL path segment
    could supply the workspace, the env var wins.

    Validates: Requirements 2.1, 2.3 (precedence).
    """
    assume(url_workspace != env_workspace)

    monkeypatch.setenv("BITBUCKET_URL", f"https://{host}/{url_workspace}/repo")
    monkeypatch.setenv("BITBUCKET_WORKSPACE", env_workspace)
    monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

    cfg = BitbucketConfig.from_env()

    assert cfg.workspace == env_workspace
