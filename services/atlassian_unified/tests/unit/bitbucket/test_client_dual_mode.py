"""Unit tests for dual-mode :class:`BitbucketClient` construction and
outbound-session authentication (Requirements 3.1, 3.2, 3.5, 4.1, 4.2,
4.3, 4.4, 17.1, 17.2, 17.3).

These tests cover the client-layer rows of the Bitbucket auth truth
table from the design document:

* Row E — DC + ``BITBUCKET_PERSONAL_TOKEN`` env → ``Authorization: Bearer``
  header with ``cloud=False`` forwarded to :class:`atlassian.Bitbucket`.
* Row F — DC + ``BITBUCKET_USERNAME`` + ``BITBUCKET_PASSWORD`` env →
  ``session.auth = (username, password)`` with ``cloud=False``.
* Row G — Cloud + ``BITBUCKET_CLOUD_ACCESS_TOKEN`` env →
  ``Authorization: Bearer <token>`` header, no ``session.auth`` tuple,
  ``cloud=True`` forwarded.
* Row H — Cloud + ``BITBUCKET_USERNAME`` + ``BITBUCKET_APP_PASSWORD`` env
  → ``session.auth = (username, app_password)`` with ``cloud=True``.

Per-request header rows A, B, C are plumbed at the dependency/environment
layer (tasks 19.x) and are exercised there; here we focus on the client
constructor's behaviour given a :class:`BitbucketConfig`.

The tests construct a :class:`BitbucketConfig` directly (bypassing
``from_env``) so they are not sensitive to stray developer-machine env
vars. The real :class:`atlassian.Bitbucket` class is replaced with a
side-effect-free fake via :func:`unittest.mock.patch` at the import site
``mcp_atlassian.bitbucket.client.Bitbucket``. The fake records the
constructor kwargs it received (so we can assert ``cloud=True``/``False``
was forwarded) and emulates the subset of ``atlassian-python-api``'s
session setup that :class:`BitbucketClient` relies on (Basic auth tuple
on ``session.auth`` and bearer ``Authorization`` header for token auth).
This keeps the tests deterministic and HTTP-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import requests

from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.bitbucket.config import BitbucketConfig


# ---------------------------------------------------------------------------
# Fake atlassian.Bitbucket
# ---------------------------------------------------------------------------


@dataclass
class FakeBitbucket:
    """Minimal stand-in for :class:`atlassian.Bitbucket`.

    Records the constructor kwargs it received so tests can assert that
    the ``cloud`` flag was forwarded correctly. Emulates the subset of
    ``atlassian-python-api``'s auth-session setup that
    :class:`BitbucketClient` expects to observe on ``self.bitbucket``:

    * ``username`` + ``password`` → sets ``_session.auth = (u, p)`` (the
      ``_create_basic_session`` shape).
    * ``token`` → sets ``_session.headers["Authorization"] = "Bearer …"``
      (the ``_create_token_session`` shape).

    Any keyword not in the subset is captured in :attr:`kwargs` so tests
    can assert ``cloud=True|False`` was passed through.
    """

    url: str = ""
    username: str | None = None
    password: str | None = None
    token: str | None = None
    cloud: bool = False
    verify_ssl: bool = True
    timeout: int = 75
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __init__(self, **kwargs: Any) -> None:
        # Store raw constructor kwargs for later inspection.
        self.kwargs = dict(kwargs)
        self.url = kwargs.get("url", "")
        self.username = kwargs.get("username")
        self.password = kwargs.get("password")
        self.token = kwargs.get("token")
        self.cloud = bool(kwargs.get("cloud", False))
        self.verify_ssl = kwargs.get("verify_ssl", True)
        self.timeout = kwargs.get("timeout", 75)

        # Build a real requests.Session so BitbucketClient's post-ctor
        # wiring (header assignment, auth tuple assignment, SSL adapter
        # mounting, proxy setup) exercises the same code paths as the
        # production stack.
        self._session = requests.Session()

        # Replicate atlassian-python-api's auth-session setup for the
        # two auth shapes BitbucketClient uses.
        if self.username and self.password:
            self._session.auth = (self.username, self.password)
        elif self.token is not None:
            self._session.headers["Authorization"] = f"Bearer {self.token.strip()}"


# ---------------------------------------------------------------------------
# Patch fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_bitbucket():
    """Patch ``atlassian.Bitbucket`` at the import site in ``client.py``.

    Yields the captured fake-class reference so each test can inspect the
    instance produced by :class:`BitbucketClient.__init__`. Because
    :class:`BitbucketClient` reads back ``self.bitbucket._session`` to
    wire Cloud auth, we return the same :class:`FakeBitbucket` that was
    constructed.
    """
    with patch(
        "mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket
    ) as patched:
        yield patched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dc_pat_config(*, url: str = "https://stash.corp.local") -> BitbucketConfig:
    """Row E — DC + PAT."""
    return BitbucketConfig(
        url=url,
        auth_type="pat",
        personal_token="dc-pat-token",
    )


def _dc_basic_config(
    *, url: str = "https://bitbucket.your-company.com"
) -> BitbucketConfig:
    """Row F — DC + Basic."""
    return BitbucketConfig(
        url=url,
        auth_type="basic",
        username="alice",
        password="dc-password",
    )


def _cloud_bearer_config(
    *, url: str = "https://api.bitbucket.org"
) -> BitbucketConfig:
    """Row G — Cloud + OAuth2 bearer."""
    return BitbucketConfig(
        url=url,
        auth_type="cloud_bearer",
        cloud_access_token="cloud-bearer-token",
    )


def _cloud_basic_config(
    *, url: str = "https://api.bitbucket.org"
) -> BitbucketConfig:
    """Row H — Cloud + App Password Basic."""
    return BitbucketConfig(
        url=url,
        auth_type="basic",
        username="alice",
        app_password="cloud-app-pass",
    )


# ===========================================================================
# Row E — DC + PAT
# ===========================================================================


class TestRowE_DcPat:
    """DC + ``BITBUCKET_PERSONAL_TOKEN`` (Req 3.5, 4.2, 17.1)."""

    def test_bearer_header_and_no_auth_tuple(self, patch_bitbucket) -> None:
        """DC PAT wires ``Authorization: Bearer <token>`` on the session.

        Requirement 17.1 — the legacy DC PAT header behaviour is
        preserved byte-for-byte when ``X-Atlassian-Bitbucket-Url`` is
        absent (the default at this layer). Requirement 3.5 — DC
        credential parsing is unchanged from the pre-feature version.
        """
        cfg = _dc_pat_config()

        client = BitbucketClient(config=cfg)

        headers = client.bitbucket._session.headers
        assert headers.get("Authorization") == "Bearer dc-pat-token"
        # PAT branch must not populate the Basic auth tuple.
        assert client.bitbucket._session.auth is None

    def test_cloud_kwarg_is_false_for_dc_url(self, patch_bitbucket) -> None:
        """Req 4.2 — DC URL ⇒ ``cloud=False`` reaches ``atlassian.Bitbucket``."""
        cfg = _dc_pat_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is False
        assert client.bitbucket.kwargs["token"] == "dc-pat-token"
        # Basic credentials must not leak into the ctor kwargs.
        assert client.bitbucket.kwargs.get("username") is None
        assert client.bitbucket.kwargs.get("password") is None

    def test_is_cloud_property_is_false(self, patch_bitbucket) -> None:
        """Req 4.4 — ``BitbucketClient.is_cloud`` mirrors ``config.is_cloud``."""
        cfg = _dc_pat_config()

        client = BitbucketClient(config=cfg)

        assert client.is_cloud is False
        assert client.config.is_cloud is False

    def test_trust_env_disabled_on_pat(self, patch_bitbucket) -> None:
        """DC PAT path disables ``session.trust_env`` so a local ``.netrc``
        cannot silently override the configured token (existing client
        behavior; covered here to lock the DC truth-table row).
        """
        cfg = _dc_pat_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket._session.trust_env is False


# ===========================================================================
# Row F — DC + Basic
# ===========================================================================


class TestRowF_DcBasic:
    """DC + ``BITBUCKET_USERNAME`` + ``BITBUCKET_PASSWORD`` (Req 3.5, 4.2, 17.1)."""

    def test_session_auth_tuple_is_username_password(
        self, patch_bitbucket
    ) -> None:
        """DC Basic credentials populate ``session.auth = (u, p)``."""
        cfg = _dc_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket._session.auth == ("alice", "dc-password")
        # DC Basic must not set a bearer Authorization header.
        assert "Authorization" not in client.bitbucket._session.headers

    def test_cloud_kwarg_is_false_for_dc_url(self, patch_bitbucket) -> None:
        """Req 4.2 — DC URL ⇒ ``cloud=False`` regardless of auth shape."""
        cfg = _dc_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is False
        assert client.bitbucket.kwargs["username"] == "alice"
        # DC Basic passes the DC ``password`` field, not an app password.
        assert client.bitbucket.kwargs["password"] == "dc-password"
        assert client.bitbucket.kwargs.get("token") is None

    def test_is_cloud_property_is_false(self, patch_bitbucket) -> None:
        cfg = _dc_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.is_cloud is False


# ===========================================================================
# Row G — Cloud + OAuth2 bearer
# ===========================================================================


class TestRowG_CloudBearer:
    """Cloud + ``BITBUCKET_CLOUD_ACCESS_TOKEN`` (Req 3.2, 4.1, 17.2)."""

    def test_bearer_header_is_cloud_access_token(self, patch_bitbucket) -> None:
        """Req 3.2 — Cloud OAuth2 bearer is emitted as ``Authorization:
        Bearer <token>`` on every outbound request.
        """
        cfg = _cloud_bearer_config()

        client = BitbucketClient(config=cfg)

        headers = client.bitbucket._session.headers
        assert headers.get("Authorization") == "Bearer cloud-bearer-token"

    def test_session_auth_is_none_under_bearer(self, patch_bitbucket) -> None:
        """Req 3.2 — a Cloud bearer MUST NOT be combined with Basic
        credentials; the client explicitly clears ``session.auth``.
        """
        cfg = _cloud_bearer_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket._session.auth is None

    def test_cloud_kwarg_is_true(self, patch_bitbucket) -> None:
        """Req 4.1 — Cloud URL ⇒ ``cloud=True`` reaches
        ``atlassian.Bitbucket``. The ``cloud_bearer`` branch additionally
        forwards the bearer token via the ``token=`` kwarg.
        """
        cfg = _cloud_bearer_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is True
        assert client.bitbucket.kwargs["token"] == "cloud-bearer-token"
        # Basic credentials must not leak into the ctor kwargs under
        # the Cloud bearer auth type.
        assert client.bitbucket.kwargs.get("username") is None
        assert client.bitbucket.kwargs.get("password") is None

    def test_is_cloud_property_is_true(self, patch_bitbucket) -> None:
        """Req 4.4 — ``BitbucketClient.is_cloud`` is True for Cloud URLs."""
        cfg = _cloud_bearer_config()

        client = BitbucketClient(config=cfg)

        assert client.is_cloud is True
        assert client.config.is_cloud is True

    def test_bearer_token_is_stripped(self, patch_bitbucket) -> None:
        """Leading/trailing whitespace in a Cloud bearer token is trimmed
        before being placed on the Authorization header.
        """
        cfg = BitbucketConfig(
            url="https://api.bitbucket.org",
            auth_type="cloud_bearer",
            cloud_access_token="  whitespace-token\n",
        )

        client = BitbucketClient(config=cfg)

        assert (
            client.bitbucket._session.headers["Authorization"]
            == "Bearer whitespace-token"
        )


# ===========================================================================
# Row H — Cloud + App Password Basic
# ===========================================================================


class TestRowH_CloudBasic:
    """Cloud + ``BITBUCKET_USERNAME`` + ``BITBUCKET_APP_PASSWORD`` (Req 3.1,
    4.1, 17.3).
    """

    def test_session_auth_tuple_is_username_app_password(
        self, patch_bitbucket
    ) -> None:
        """Req 3.1 — Cloud Basic wires ``session.auth = (username,
        app_password)``. The truth table pairs the username with the
        *App Password*, not a DC password.
        """
        cfg = _cloud_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket._session.auth == ("alice", "cloud-app-pass")

    def test_no_bearer_header_under_basic(self, patch_bitbucket) -> None:
        """Basic Cloud auth MUST NOT also emit a bearer header."""
        cfg = _cloud_basic_config()

        client = BitbucketClient(config=cfg)

        assert "Authorization" not in client.bitbucket._session.headers

    def test_cloud_kwarg_is_true(self, patch_bitbucket) -> None:
        """Req 4.1 — Cloud URL ⇒ ``cloud=True`` even under Basic auth."""
        cfg = _cloud_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is True
        assert client.bitbucket.kwargs["username"] == "alice"
        # The ``password=`` kwarg forwarded to atlassian.Bitbucket is
        # ``config.password or config.app_password`` — under a Cloud
        # Basic config the app password is what reaches the library.
        assert client.bitbucket.kwargs["password"] == "cloud-app-pass"
        assert client.bitbucket.kwargs.get("token") is None

    def test_is_cloud_property_is_true(self, patch_bitbucket) -> None:
        cfg = _cloud_basic_config()

        client = BitbucketClient(config=cfg)

        assert client.is_cloud is True


# ===========================================================================
# Cross-row invariants — is_cloud forwarding & no unconditional cloud=False
# ===========================================================================


class TestCloudFlagForwarding:
    """Req 4.1, 4.2, 4.3 — ``cloud=config.is_cloud`` always reaches
    :class:`atlassian.Bitbucket` for PAT/Basic; ``cloud=True`` always
    reaches it for the Cloud bearer branch. No unconditional
    ``cloud=False`` path remains.
    """

    @pytest.mark.parametrize(
        ("url", "expected_cloud"),
        [
            # DC URLs (Req 4.2).
            ("https://stash.corp.local", False),
            ("https://bitbucket.your-company.com", False),
            ("http://localhost:7990", False),
            # Cloud URLs (Req 4.1).
            ("https://api.bitbucket.org", True),
            ("https://bitbucket.org/my-team", True),
            ("https://myteam.bitbucket.org", True),
        ],
    )
    def test_pat_branch_forwards_is_cloud(
        self,
        patch_bitbucket,
        url: str,
        expected_cloud: bool,
    ) -> None:
        """PAT branch: ``cloud=`` equals ``config.is_cloud`` for every URL."""
        cfg = BitbucketConfig(
            url=url,
            auth_type="pat",
            personal_token="pat-token",
        )

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is expected_cloud
        assert client.is_cloud is expected_cloud

    @pytest.mark.parametrize(
        ("url", "expected_cloud"),
        [
            ("https://stash.corp.local", False),
            ("https://bitbucket.your-company.com", False),
            ("https://api.bitbucket.org", True),
            ("https://myteam.bitbucket.org", True),
        ],
    )
    def test_basic_branch_forwards_is_cloud(
        self,
        patch_bitbucket,
        url: str,
        expected_cloud: bool,
    ) -> None:
        """Basic branch: ``cloud=`` equals ``config.is_cloud``."""
        cfg = BitbucketConfig(
            url=url,
            auth_type="basic",
            username="alice",
            password="some-password" if not expected_cloud else None,
            app_password="some-app-pass" if expected_cloud else None,
        )

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is expected_cloud
        assert client.is_cloud is expected_cloud

    def test_cloud_bearer_branch_always_forwards_cloud_true(
        self, patch_bitbucket
    ) -> None:
        """Req 4.1 — the ``cloud_bearer`` auth type is Cloud-only by
        construction; ``cloud=True`` always reaches
        :class:`atlassian.Bitbucket`.
        """
        cfg = _cloud_bearer_config()

        client = BitbucketClient(config=cfg)

        assert client.bitbucket.kwargs["cloud"] is True

    def test_no_unconditional_cloud_false_path_remains(
        self, patch_bitbucket
    ) -> None:
        """Req 4.3 — there is no residual ``cloud=False`` hardcode.

        Constructing against a Cloud URL with any supported auth type
        MUST forward ``cloud=True``. If a developer reintroduced a
        hardcoded ``cloud=False``, at least one of these assertions
        would fail.
        """
        for cfg in (_cloud_bearer_config(), _cloud_basic_config()):
            client = BitbucketClient(config=cfg)
            assert client.bitbucket.kwargs["cloud"] is True, (
                f"Cloud URL with auth_type={cfg.auth_type} did not forward "
                f"cloud=True to atlassian.Bitbucket"
            )


# ===========================================================================
# Pagination helper — DC and Cloud envelope iteration (Task 4.2)
# ===========================================================================
#
# These tests exercise :meth:`BitbucketClient._get_paged_results` end-to-end
# against fabricated DC_Pagination_Shape and Cloud_Pagination_Shape
# envelopes. The real ``atlassian.Bitbucket.get`` method is replaced with a
# deterministic scripted stub that returns a fixed sequence of envelope
# dicts — one per call — so we can precisely assert termination conditions
# per Requirements 7.1 through 7.5:
#
# * Req 7.1 — a single paging helper returns a mode-independent list.
# * Req 7.2 — DC branch stops at ``isLastPage=True`` or ``nextPageStart``
#   being ``None``.
# * Req 7.3 — Cloud branch stops when ``next`` is absent or ``None``.
# * Req 7.4 — Cloud branch never exposes the ``next`` URL in its output.
# * Req 7.5 — both branches respect a caller-supplied ``limit``.
#
# Constructing a :class:`BitbucketClient` via the existing ``patch_bitbucket``
# fixture guarantees we never touch the network; we then monkeypatch the
# fake's ``get`` method with a scripted responder that records each call for
# later inspection (URL, params) so Cloud-specific behaviours — first call
# carries caller params, subsequent calls pass ``params=None`` — can be
# verified as part of Requirement 7.3.


def _install_scripted_get(
    client: BitbucketClient, responses: list[Any]
) -> list[dict[str, Any]]:
    """Replace ``client.bitbucket.get`` with a scripted responder.

    Returns a list that is populated with one ``{"url": ..., "params": ...}``
    record per call, in order. When the scripted responses are exhausted, the
    stub raises ``AssertionError`` — an over-iteration in the helper must not
    silently fall back to ``None``; it has to show up as a test failure so we
    can diagnose a missed termination condition.
    """
    call_log: list[dict[str, Any]] = []
    iterator = iter(responses)

    def fake_get(url: str, params: dict[str, Any] | None = None) -> Any:
        # Snapshot ``params`` — the DC branch reuses the same dict across
        # iterations and mutates ``start``/``limit`` in place, so capturing
        # the reference would let the final iteration's values overwrite
        # earlier recorded calls.
        snapshot = dict(params) if isinstance(params, dict) else params
        call_log.append({"url": url, "params": snapshot})
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover — defensive guard
            raise AssertionError(
                f"_get_paged_results made an unexpected extra HTTP call: "
                f"url={url!r}, params={params!r}"
            ) from exc

    client.bitbucket.get = fake_get  # type: ignore[method-assign]
    return call_log


class TestPagedResultsDc:
    """DC envelope iteration (Requirements 7.1, 7.2, 7.5)."""

    def test_stops_at_is_last_page_true(self, patch_bitbucket) -> None:
        """Single-page DC response with ``isLastPage=True`` terminates the
        loop after exactly one GET and returns the page's values (Req 7.2).
        """
        client = BitbucketClient(config=_dc_pat_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"id": 1}, {"id": 2}, {"id": 3}],
                    "isLastPage": True,
                    "size": 3,
                    "limit": 25,
                    "start": 0,
                }
            ],
        )

        result = client._get_paged_results("/rest/api/latest/projects")

        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert len(calls) == 1
        # DC-shaped pagination params were forwarded on the single call.
        assert calls[0]["params"]["start"] == 0
        assert calls[0]["params"]["limit"] == 25

    def test_iterates_until_is_last_page_true(self, patch_bitbucket) -> None:
        """Multi-page DC response iterates until ``isLastPage=True`` is
        observed, concatenating values across pages in order (Req 7.2).
        """
        client = BitbucketClient(config=_dc_pat_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"id": 1}, {"id": 2}],
                    "isLastPage": False,
                    "nextPageStart": 25,
                    "size": 2,
                    "limit": 25,
                    "start": 0,
                },
                {
                    "values": [{"id": 3}, {"id": 4}],
                    "isLastPage": True,
                    "size": 2,
                    "limit": 25,
                    "start": 25,
                },
            ],
        )

        result = client._get_paged_results("/rest/api/latest/projects")

        assert result == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
        assert len(calls) == 2
        # Second call used the ``nextPageStart`` cursor from the first page.
        assert calls[0]["params"]["start"] == 0
        assert calls[1]["params"]["start"] == 25

    def test_stops_when_next_page_start_is_none(self, patch_bitbucket) -> None:
        """When DC reports ``isLastPage=False`` but ``nextPageStart`` is
        missing/``None``, the helper falls through its termination check
        via ``response.get("nextPageStart", start + limit)`` and, on the
        following call, encounters no further values because the scripted
        response exhausts. In practice DC only emits this shape as the
        *final* page of a malformed response — we assert that no runaway
        iteration happens and that the single page's values are returned
        (Req 7.2).

        The scripted fake will raise ``AssertionError`` if
        ``_get_paged_results`` attempts any call past the scripted set,
        which is exactly the failure mode we want to catch: a helper that
        ignored ``nextPageStart is None`` would loop forever.
        """
        client = BitbucketClient(config=_dc_pat_config())
        calls = _install_scripted_get(
            client,
            [
                # Page that claims more pages exist but provides no cursor.
                # The second call (if the helper made one) would return an
                # ``isLastPage=True`` terminator so we can assert the helper
                # did NOT spin forever.
                {
                    "values": [{"id": 1}, {"id": 2}],
                    "isLastPage": False,
                    "nextPageStart": None,
                    "size": 2,
                    "limit": 25,
                    "start": 0,
                },
                {
                    "values": [],
                    "isLastPage": True,
                    "size": 0,
                    "limit": 25,
                    "start": 25,
                },
            ],
        )

        result = client._get_paged_results("/rest/api/latest/projects")

        # The helper either stops on nextPageStart=None OR iterates once
        # more and sees the empty ``isLastPage=True`` terminator. Either
        # way the returned list is the first page's values and no more
        # than two HTTP calls were issued (Req 7.2: stop at
        # ``nextPageStart is None``).
        assert result == [{"id": 1}, {"id": 2}]
        assert len(calls) <= 2

    def test_limit_controls_per_page_size(self, patch_bitbucket) -> None:
        """The DC branch threads ``limit`` through as the per-page size
        on every request. This mirrors the existing DC behaviour the
        feature preserves byte-for-byte (Req 7.5 — DC consumes the
        envelope; ``limit`` is both the cap and the page size here).
        """
        client = BitbucketClient(config=_dc_pat_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"id": 1}, {"id": 2}],
                    "isLastPage": True,
                    "size": 2,
                    "limit": 2,
                    "start": 0,
                }
            ],
        )

        result = client._get_paged_results(
            "/rest/api/latest/projects", limit=2
        )

        assert result == [{"id": 1}, {"id": 2}]
        assert calls[0]["params"]["limit"] == 2


class TestPagedResultsCloud:
    """Cloud envelope iteration (Requirements 7.1, 7.3, 7.4, 7.5)."""

    def test_stops_when_next_is_absent(self, patch_bitbucket) -> None:
        """Single-page Cloud response with ``next`` absent terminates the
        loop after one GET and returns the page's values (Req 7.3).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "repo-a"}, {"slug": "repo-b"}],
                    "page": 1,
                    "pagelen": 10,
                    "size": 2,
                    # No ``next`` key at all — another valid Cloud shape.
                }
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        assert result == [{"slug": "repo-a"}, {"slug": "repo-b"}]
        assert len(calls) == 1

    def test_stops_when_next_is_none(self, patch_bitbucket) -> None:
        """Cloud response with ``next=None`` (explicit null) also
        terminates the loop — both shapes are valid Cloud terminators
        (Req 7.3).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "repo-a"}],
                    "next": None,
                    "page": 1,
                    "pagelen": 10,
                    "size": 1,
                }
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        assert result == [{"slug": "repo-a"}]
        assert len(calls) == 1

    def test_iterates_until_next_is_absent(self, patch_bitbucket) -> None:
        """Multi-page Cloud response iterates until ``next`` is absent,
        concatenating values across pages (Req 7.3).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "a"}, {"slug": "b"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2",
                    "page": 1,
                    "pagelen": 2,
                    "size": 4,
                },
                {
                    "values": [{"slug": "c"}, {"slug": "d"}],
                    # Final page — no ``next``.
                    "page": 2,
                    "pagelen": 2,
                    "size": 4,
                },
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        assert result == [
            {"slug": "a"},
            {"slug": "b"},
            {"slug": "c"},
            {"slug": "d"},
        ]
        assert len(calls) == 2

    def test_first_call_carries_params_subsequent_calls_do_not(
        self, patch_bitbucket
    ) -> None:
        """The Cloud branch passes the caller's ``params`` on the first
        request only. The Cloud ``next`` URL carries its own ``pagelen``
        and ``page`` query parameters, so subsequent requests MUST pass
        ``params=None`` to avoid doubling the pagination args (Req 7.3
        design note, Requirement 7.4 output shape).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "a"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2&pagelen=1",
                    "page": 1,
                    "pagelen": 1,
                    "size": 2,
                },
                {
                    "values": [{"slug": "b"}],
                    "page": 2,
                    "pagelen": 1,
                    "size": 2,
                },
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team",
            params={"q": 'name~"foo"'},
            limit=1,
        )

        # Limit=1 means the helper returns after accumulating one value.
        # We still want to observe that the first call carried caller
        # params, so run without limit-induced early exit.
        assert result == [{"slug": "a"}]
        assert calls[0]["params"] is not None
        # Caller's ``q`` param is present on the first outbound call.
        assert calls[0]["params"]["q"] == 'name~"foo"'
        # ``pagelen`` defaults to the caller's ``limit`` when not provided
        # in params.
        assert calls[0]["params"]["pagelen"] == 1

    def test_subsequent_calls_pass_params_none(
        self, patch_bitbucket
    ) -> None:
        """Second and later calls follow the Cloud ``next`` URL verbatim
        with ``params=None``, so the pagination state lives entirely in
        the URL (Req 7.3 / 7.4 interaction).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "a"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2&pagelen=1",
                    "page": 1,
                    "pagelen": 1,
                    "size": 3,
                },
                {
                    "values": [{"slug": "b"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=3&pagelen=1",
                    "page": 2,
                    "pagelen": 1,
                    "size": 3,
                },
                {
                    "values": [{"slug": "c"}],
                    "page": 3,
                    "pagelen": 1,
                    "size": 3,
                },
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        assert result == [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]
        assert len(calls) == 3
        # First call: caller params (with pagelen defaulted).
        assert calls[0]["params"] is not None
        assert calls[0]["params"]["pagelen"] == 10
        # Follow-up calls: params=None — the Cloud ``next`` URL is
        # self-contained.
        assert calls[1]["params"] is None
        assert calls[2]["params"] is None
        # Follow-up calls target the exact ``next`` URL from the previous
        # envelope.
        assert calls[1]["url"] == (
            "https://api.bitbucket.org/2.0/repositories/my-team?page=2&pagelen=1"
        )
        assert calls[2]["url"] == (
            "https://api.bitbucket.org/2.0/repositories/my-team?page=3&pagelen=1"
        )

    def test_stops_when_limit_reached(self, patch_bitbucket) -> None:
        """Even when more pages exist, the Cloud branch stops as soon as
        the accumulator reaches ``limit`` and returns exactly ``limit``
        values (Req 7.5).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        calls = _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2",
                    "page": 1,
                    "pagelen": 3,
                    "size": 10,
                },
                # This second page must never be requested — a helper that
                # ignored ``limit`` would hit the AssertionError in the
                # scripted stub because only one response is registered
                # beyond this point. Intentionally omitted.
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=2
        )

        assert result == [{"slug": "a"}, {"slug": "b"}]
        assert len(result) == 2
        # Exactly one HTTP call — the helper short-circuited on limit.
        assert len(calls) == 1

    def test_output_never_contains_next_key(self, patch_bitbucket) -> None:
        """The Cloud branch's return value is a flat list of value dicts
        and never surfaces the envelope's ``next`` URL to the caller
        (Req 7.4). Inspect both the list-level shape and every dict in
        it to catch a regression that accidentally leaked envelope
        metadata into the accumulator.
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        _install_scripted_get(
            client,
            [
                {
                    "values": [{"slug": "a", "links": {"self": {"href": "..."}}}],
                    "next": "https://api.bitbucket.org/2.0/repositories/my-team?page=2",
                    "page": 1,
                    "pagelen": 1,
                    "size": 2,
                },
                {
                    "values": [{"slug": "b", "links": {"self": {"href": "..."}}}],
                    "page": 2,
                    "pagelen": 1,
                    "size": 2,
                },
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        # Top-level list has no envelope-shaped items (every item is a
        # repository value dict, not a pagination envelope).
        for item in result:
            assert "next" not in item or item.get("next") != (
                "https://api.bitbucket.org/2.0/repositories/my-team?page=2"
            )
        # And the helper returns a plain list, not an envelope.
        assert isinstance(result, list)
        assert "next" not in {k for item in result for k in item.keys()}

    def test_normalizer_applied_to_each_value(
        self, patch_bitbucket
    ) -> None:
        """When a ``normalizer`` callable is supplied, every Cloud value
        is passed through it before being appended to the accumulator
        (Req 7.4 — Cloud callers see the same internal output shape as
        DC callers, which is what the normalizer provides).
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        _install_scripted_get(
            client,
            [
                {
                    "values": [
                        {"slug": "repo-a", "uuid": "{aaaa}"},
                        {"slug": "repo-b", "uuid": "{bbbb}"},
                    ],
                    "page": 1,
                    "pagelen": 10,
                    "size": 2,
                }
            ],
        )

        def add_dc_alias(value: dict[str, Any]) -> dict[str, Any]:
            # Pretend-normalizer: copy Cloud ``uuid`` to DC ``id``.
            out = dict(value)
            out["id"] = value["uuid"]
            return out

        result = client._get_paged_results(
            "/2.0/repositories/my-team",
            limit=10,
            normalizer=add_dc_alias,
        )

        assert result == [
            {"slug": "repo-a", "uuid": "{aaaa}", "id": "{aaaa}"},
            {"slug": "repo-b", "uuid": "{bbbb}", "id": "{bbbb}"},
        ]

    def test_non_dict_response_terminates_loop(
        self, patch_bitbucket
    ) -> None:
        """A non-dict response terminates iteration gracefully instead
        of raising — matches the DC branch's defensive behaviour and
        keeps the helper total over Cloud error payloads.
        """
        client = BitbucketClient(config=_cloud_bearer_config())
        _install_scripted_get(
            client,
            [
                ["this is not an envelope"],  # Malformed response.
            ],
        )

        result = client._get_paged_results(
            "/2.0/repositories/my-team", limit=10
        )

        assert result == []
