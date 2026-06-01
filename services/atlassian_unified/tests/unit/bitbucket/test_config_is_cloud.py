"""Unit tests for Bitbucket Cloud/DC classification and ``from_env`` truth
table (Requirements 1.1-1.7, 2.2, 2.3, 3.3, 3.4, 23.1, 23.3).

Covers the URL-based mode detection in
:func:`mcp_atlassian.bitbucket.config.is_cloud_host`, the derived
:attr:`BitbucketConfig.is_cloud` property, and the Cloud-aware branches
of :meth:`BitbucketConfig.from_env` (auth truth-table rows I, J, K and
workspace resolution from the URL path).
"""

from __future__ import annotations

import pytest

from mcp_atlassian.bitbucket.config import BitbucketConfig, is_cloud_host


# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


# The full set of environment variables ``BitbucketConfig.from_env`` reads.
# Each test starts from a clean slate so a stray developer-machine variable
# does not leak into the truth table.
_BITBUCKET_ENV_VARS: tuple[str, ...] = (
    "BITBUCKET_URL",
    "BITBUCKET_USERNAME",
    "BITBUCKET_PASSWORD",
    "BITBUCKET_PERSONAL_TOKEN",
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
    # Fallback proxy/ssl envs consulted by ``from_env`` too:
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SOCKS_PROXY",
)


@pytest.fixture(autouse=True)
def _clear_bitbucket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every Bitbucket env var before each test runs."""
    for name in _BITBUCKET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ===========================================================================
# is_cloud_host / BitbucketConfig.is_cloud — Requirements 1.1-1.7
# ===========================================================================


class TestIsCloudHostClassifier:
    """URL hostname classifies Cloud vs DC per Req 1.2-1.5."""

    # --- Cloud hosts (Req 1.2, 1.3, 1.4) -----------------------------------

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.bitbucket.org",
            "https://api.bitbucket.org/",
            "https://api.bitbucket.org/2.0/repositories",
        ],
    )
    def test_api_bitbucket_org_is_cloud(self, url: str) -> None:
        """Req 1.2 — ``api.bitbucket.org`` is Cloud."""
        assert is_cloud_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://bitbucket.org",
            "https://bitbucket.org/",
            "https://bitbucket.org/my-team",
        ],
    )
    def test_bitbucket_org_is_cloud(self, url: str) -> None:
        """Req 1.3 — bare ``bitbucket.org`` is Cloud."""
        assert is_cloud_host(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://myteam.bitbucket.org",
            "https://myteam.bitbucket.org/",
            "https://staging.api.bitbucket.org/2.0/x",
            "https://a.b.c.bitbucket.org/path",
        ],
    )
    def test_subdomain_of_bitbucket_org_is_cloud(self, url: str) -> None:
        """Req 1.4 — any subdomain of ``bitbucket.org`` is Cloud."""
        assert is_cloud_host(url) is True

    # --- DC hosts (Req 1.5) -------------------------------------------------

    @pytest.mark.parametrize(
        "url",
        [
            "https://stash.corp.local",
            "https://stash.corp.local/bitbucket",
            "https://bitbucket.your-company.com",
            "https://bitbucket-internal.example.com",
        ],
    )
    def test_corporate_host_is_dc(self, url: str) -> None:
        """Req 1.5 — hostnames outside the Cloud set classify as DC."""
        assert is_cloud_host(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost",
            "http://localhost:7990",
            "https://localhost/bitbucket",
        ],
    )
    def test_localhost_is_dc(self, url: str) -> None:
        """Req 1.5 — ``localhost`` always classifies as DC."""
        assert is_cloud_host(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "http://192.0.2.10",
            "http://192.0.2.10:7990",
            "http://10.0.0.5/bitbucket",
            "http://[2001:db8::1]:7990",
        ],
    )
    def test_ip_literal_is_dc(self, url: str) -> None:
        """Req 1.5 — IP literals always classify as DC."""
        assert is_cloud_host(url) is False

    def test_name_that_ends_with_bitbucket_org_but_is_not_subdomain_is_dc(
        self,
    ) -> None:
        """Guard against naive ``endswith("bitbucket.org")`` without the dot.

        ``fakebitbucket.org`` ends with the literal string ``bitbucket.org``
        but is NOT a subdomain of ``bitbucket.org``. The classifier must
        only accept a true subdomain (prefixed by a dot).
        """
        assert is_cloud_host("https://fakebitbucket.org") is False
        assert is_cloud_host("https://notbitbucket.org/path") is False

    # --- Case-insensitivity (design rule) ----------------------------------

    @pytest.mark.parametrize(
        "url",
        [
            "https://API.BITBUCKET.ORG/2.0",
            "https://BitBucket.Org/my-team",
            "https://MyTeam.BitBucket.Org",
        ],
    )
    def test_hostname_comparison_is_case_insensitive(self, url: str) -> None:
        """Hostname matching is case-insensitive."""
        assert is_cloud_host(url) is True

    # --- Degenerate input (function must be total) -------------------------

    @pytest.mark.parametrize("url", ["", "not a url", "https://"])
    def test_empty_or_malformed_url_returns_false(self, url: str) -> None:
        """Unparseable URLs classify as DC rather than raising."""
        assert is_cloud_host(url) is False


class TestBitbucketConfigIsCloudProperty:
    """``BitbucketConfig.is_cloud`` is a pure function of ``url`` (Req 1.1, 1.6)."""

    def test_cloud_url_property_is_true(self) -> None:
        """Req 1.1, 1.6 — ``is_cloud`` reflects the current ``url``."""
        cfg = BitbucketConfig(
            url="https://api.bitbucket.org",
            auth_type="cloud_bearer",
            cloud_access_token="tok",
        )
        assert cfg.is_cloud is True

    def test_dc_url_property_is_false(self) -> None:
        """Req 1.1, 1.6 — DC URL ⇒ ``is_cloud`` False."""
        cfg = BitbucketConfig(
            url="https://stash.corp.local",
            auth_type="pat",
            personal_token="dc-pat",
        )
        assert cfg.is_cloud is False

    def test_is_cloud_recomputes_when_url_changes(self) -> None:
        """Req 1.6 — ``is_cloud`` is a property, not a cached attribute.

        Mutating ``url`` on the dataclass must flip the property value
        without requiring reconstruction.
        """
        cfg = BitbucketConfig(
            url="https://stash.corp.local",
            auth_type="pat",
            personal_token="dc-pat",
        )
        assert cfg.is_cloud is False

        cfg.url = "https://bitbucket.org"
        assert cfg.is_cloud is True

    def test_is_cloud_is_never_unconditional_false(self) -> None:
        """Req 1.7 — the classifier cannot be stuck at ``False`` for a Cloud URL.

        This is the guard against a literal ``return False`` implementation
        sneaking back in during a refactor.
        """
        cfg = BitbucketConfig(
            url="https://api.bitbucket.org",
            auth_type="cloud_bearer",
            cloud_access_token="tok",
        )
        assert cfg.is_cloud is True


# ===========================================================================
# from_env truth table — Requirements 3.3, 3.4, 23.1, 23.3
# ===========================================================================


class TestFromEnvMissingUrl:
    """``BITBUCKET_URL`` is mandatory for both Cloud and DC (Req 3.3, 3.5)."""

    def test_unset_url_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No URL ⇒ ``ValueError`` before any credential parsing runs."""
        with pytest.raises(ValueError, match="BITBUCKET_URL"):
            BitbucketConfig.from_env()


class TestFromEnvCloudRowI:
    """Row I — Cloud URL with NEITHER credential pair set ⇒ ``ValueError`` (Req 3.3)."""

    def test_cloud_url_with_no_creds_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")

        with pytest.raises(ValueError, match="Bitbucket Cloud authentication"):
            BitbucketConfig.from_env()

    def test_cloud_url_with_only_username_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Username alone is not a credential pair on Cloud."""
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/myteam")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")

        with pytest.raises(ValueError, match="Bitbucket Cloud authentication"):
            BitbucketConfig.from_env()

    def test_cloud_url_with_dc_pat_only_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BITBUCKET_PERSONAL_TOKEN`` is a DC-only credential and must not
        satisfy Cloud auth (Req 3.3, 23.3).
        """
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", "dc-pat")

        with pytest.raises(ValueError, match="Bitbucket Cloud authentication"):
            BitbucketConfig.from_env()

    def test_cloud_url_with_dc_basic_pair_only_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BITBUCKET_PASSWORD`` (DC) does not stand in for
        ``BITBUCKET_APP_PASSWORD`` (Cloud).
        """
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_PASSWORD", "dc-password")

        with pytest.raises(ValueError, match="Bitbucket Cloud authentication"):
            BitbucketConfig.from_env()


class TestFromEnvCloudRowJ:
    """Row J — ``BITBUCKET_APP_PASSWORD`` set without ``BITBUCKET_USERNAME`` (Req 3.4)."""

    def test_app_password_without_username_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/myteam")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")

        with pytest.raises(
            ValueError, match="BITBUCKET_USERNAME"
        ):
            BitbucketConfig.from_env()

    def test_app_password_without_username_raises_even_with_bearer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contradictory credentials (Basic half + bearer) still fail Row J.

        ``from_env`` validates the Basic pair independently of the bearer so
        a half-configured pair surfaces immediately instead of being silently
        masked by the bearer auth_type selection.
        """
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

        with pytest.raises(ValueError, match="BITBUCKET_USERNAME"):
            BitbucketConfig.from_env()


class TestFromEnvCloudRowK:
    """Row K — Cloud bearer env var + DC URL ⇒ Cloud env vars ignored,
    DC parsing preserved (Req 3.5, 23.1, 23.3).
    """

    def test_cloud_bearer_with_dc_url_and_dc_pat_uses_dc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Cloud bearer set on a DC URL must not hijack the DC auth_type."""
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local")
        monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", "dc-pat")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.auth_type == "pat"
        assert cfg.personal_token == "dc-pat"
        # Cloud-only fields remain unset on DC URLs.
        assert cfg.cloud_access_token is None
        assert cfg.app_password is None
        assert cfg.workspace is None

    def test_cloud_bearer_with_dc_url_and_dc_basic_uses_dc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DC Basic credentials still apply when a Cloud bearer is also set."""
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.your-company.com")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_PASSWORD", "dc-password")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.auth_type == "basic"
        assert cfg.username == "alice"
        assert cfg.password == "dc-password"
        assert cfg.cloud_access_token is None
        assert cfg.app_password is None

    def test_cloud_app_password_with_dc_url_and_dc_pat_uses_dc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BITBUCKET_APP_PASSWORD`` on a DC URL is also ignored (Req 23.3).

        The important guard: Row J's validation (``BITBUCKET_APP_PASSWORD``
        without ``BITBUCKET_USERNAME``) is Cloud-only. On a DC URL the
        Cloud env vars are never inspected, so the DC branch succeeds.
        """
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local")
        monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", "dc-pat")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.auth_type == "pat"
        assert cfg.personal_token == "dc-pat"
        assert cfg.app_password is None


class TestFromEnvDcParsingUnchanged:
    """DC env parsing stays byte-for-byte identical when Cloud vars are unset (Req 23.1, 23.3)."""

    def test_dc_pat_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DC PAT path (Req 3.5)."""
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local")
        monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", "dc-pat")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.auth_type == "pat"
        assert cfg.url == "https://stash.corp.local"
        assert cfg.personal_token == "dc-pat"
        assert cfg.username is None
        assert cfg.password is None
        assert cfg.cloud_access_token is None
        assert cfg.app_password is None
        assert cfg.workspace is None

    def test_dc_basic_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DC Basic path (Req 3.5)."""
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.your-company.com")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_PASSWORD", "dc-password")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.auth_type == "basic"
        assert cfg.username == "alice"
        assert cfg.password == "dc-password"
        assert cfg.personal_token is None
        # Cloud-only fields untouched on DC URLs.
        assert cfg.cloud_access_token is None
        assert cfg.app_password is None
        assert cfg.workspace is None

    def test_dc_missing_creds_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DC URL with no credentials still raises the DC-specific error."""
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local")

        with pytest.raises(
            ValueError,
            match="Bitbucket Server/Data Center authentication",
        ):
            BitbucketConfig.from_env()

    def test_dc_incomplete_basic_pair_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DC username without password is not a valid DC Basic pair."""
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")

        with pytest.raises(
            ValueError,
            match="Bitbucket Server/Data Center authentication",
        ):
            BitbucketConfig.from_env()


# ===========================================================================
# from_env Cloud happy paths + workspace resolution (Req 2.2, 2.3, 3.1, 3.2)
# ===========================================================================


class TestFromEnvCloudBearerHappyPath:
    """``BITBUCKET_CLOUD_ACCESS_TOKEN`` on a Cloud URL picks ``cloud_bearer``."""

    def test_bearer_token_sets_cloud_bearer_auth_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is True
        assert cfg.auth_type == "cloud_bearer"
        assert cfg.cloud_access_token == "bearer-tok"
        # DC credential fields must stay unset.
        assert cfg.personal_token is None
        assert cfg.password is None

    def test_bearer_token_takes_precedence_over_basic_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both credential shapes are present, bearer wins (Req 3.2)."""
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "bearer-tok")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")

        cfg = BitbucketConfig.from_env()

        assert cfg.auth_type == "cloud_bearer"
        assert cfg.cloud_access_token == "bearer-tok"
        # App-password is still captured on the config for completeness.
        assert cfg.app_password == "app-pass"
        assert cfg.username == "alice"


class TestFromEnvCloudBasicHappyPath:
    """``BITBUCKET_USERNAME`` + ``BITBUCKET_APP_PASSWORD`` picks ``basic`` (Req 3.1)."""

    def test_basic_cloud_pair_sets_basic_auth_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/myteam")
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is True
        assert cfg.auth_type == "basic"
        assert cfg.username == "alice"
        assert cfg.app_password == "app-pass"
        # DC password field stays unset under Cloud.
        assert cfg.password is None
        assert cfg.personal_token is None

    def test_browser_repo_url_is_normalised_to_cloud_api_base(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "BITBUCKET_URL", "https://bitbucket.org/myteam/repo-a"
        )
        monkeypatch.setenv("BITBUCKET_USERNAME", "alice")
        monkeypatch.setenv("BITBUCKET_APP_PASSWORD", "app-pass")

        cfg = BitbucketConfig.from_env()

        assert cfg.url == "https://api.bitbucket.org"
        assert cfg.workspace == "myteam"


class TestFromEnvWorkspaceResolution:
    """Workspace precedence: env var wins, else parsed from URL path (Req 2.2, 2.3)."""

    def test_workspace_env_var_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Req 2.2 — ``BITBUCKET_WORKSPACE`` populates ``config.workspace``."""
        monkeypatch.setenv("BITBUCKET_URL", "https://api.bitbucket.org")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("BITBUCKET_WORKSPACE", "env-team")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace == "env-team"

    def test_workspace_env_var_wins_over_url_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit env var overrides URL-path parsing."""
        monkeypatch.setenv(
            "BITBUCKET_URL", "https://bitbucket.org/url-team/repo"
        )
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("BITBUCKET_WORKSPACE", "env-team")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace == "env-team"

    def test_workspace_parsed_from_url_path_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Req 2.3 — URL path's first segment populates workspace when
        ``BITBUCKET_WORKSPACE`` is unset.
        """
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/my-team")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace == "my-team"

    def test_workspace_parsed_from_url_path_with_trailing_slash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org/my-team/")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace == "my-team"

    def test_workspace_parsed_from_subdomain_host_url_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path parsing also applies to tenant subdomain hosts."""
        monkeypatch.setenv(
            "BITBUCKET_URL", "https://myteam.bitbucket.org/another-team/repo"
        )
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace == "another-team"

    def test_workspace_none_when_api_host_and_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``api.bitbucket.org`` URLs carry REST paths, not workspace slugs."""
        monkeypatch.setenv(
            "BITBUCKET_URL", "https://api.bitbucket.org/2.0/repositories"
        )
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace is None

    def test_workspace_none_when_cloud_url_has_no_path_segment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud URL with empty path ⇒ no workspace inferred."""
        monkeypatch.setenv("BITBUCKET_URL", "https://bitbucket.org")
        monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", "tok")

        cfg = BitbucketConfig.from_env()

        assert cfg.workspace is None

    def test_workspace_none_on_dc_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DC URLs never populate ``workspace`` (Req 23.3)."""
        monkeypatch.setenv("BITBUCKET_URL", "https://stash.corp.local/bitbucket")
        monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", "dc-pat")
        monkeypatch.setenv("BITBUCKET_WORKSPACE", "should-be-ignored")

        cfg = BitbucketConfig.from_env()

        assert cfg.is_cloud is False
        assert cfg.workspace is None
