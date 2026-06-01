"""Property test P3 — Authentication truth table.

Validates Requirements 3.1, 3.2, 3.6, 3.7, 3.8, 3.9, 4.4, 17.1, 17.2,
17.3, 17.4, 17.5 / design Property 3:

    *The outbound Authorization header matches the row chosen by the
    tuple ``(resolved_mode, config, request_headers)``. Deny rows produce
    either a ``ValueError`` (startup rows I, J) or an HTTP-401-equivalent
    ``unauthorized=True`` resolution (row D) with zero outbound Bitbucket
    calls. A Cloud bearer is never emitted on a DC URL (row K), and a DC
    PAT is never emitted on a Cloud URL (row K inverse).*

The auth truth table is implemented in two layers:

* Per-request header rows A, B, C, D, K — the parser
  :func:`mcp_atlassian.utils.environment._detect_bitbucket_auth_from_headers`
  translates ``X-Atlassian-Bitbucket-*`` headers into a resolved
  :class:`BitbucketHeaderAuth` dataclass (``auth_type`` plus the
  ready-to-use ``Authorization`` header value).
* Env-driven rows E, F, G, H, I, J — :meth:`BitbucketConfig.from_env`
  parses ``BITBUCKET_*`` env vars and :class:`BitbucketClient` wires the
  resulting ``Authorization`` header onto the outgoing
  :class:`requests.Session` owned by the underlying
  :class:`atlassian.Bitbucket` instance.

Row K is a cross-cutting invariant: no combination of per-request
headers or env vars SHALL emit a Cloud bearer token on a DC URL or a DC
PAT on a Cloud URL.

**Validates: Requirements 3.1, 3.2, 3.6, 3.7, 3.8, 3.9, 4.4, 17.1, 17.2,
17.3, 17.4, 17.5**

Testing strategy
----------------
Hypothesis strategies generate random credential values and URL slugs
that are disjoint across rows, so a mis-routed header immediately
surfaces (for example, a DC PAT leaking through on a Cloud URL would
appear as the DC token substring inside a ``Bearer …`` Authorization
value that ought to be the Cloud bearer).

* Rows A–D, K (header layer) are verified against the pure parser
  function — no HTTP, no client construction, no env vars.
* Rows E–H (env layer) are verified by constructing a real
  :class:`BitbucketClient` with ``atlassian.Bitbucket`` replaced by a
  side-effect-free fake; we then inspect the resolved session headers /
  auth tuple. ``monkeypatch`` scrubs the test environment to isolate
  each generated row.
* Rows I, J (startup deny) are verified by assertingmonkeypatch env
  and calling :meth:`BitbucketConfig.from_env` under Hypothesis-random
  Cloud URLs.

No real HTTP is ever issued; the ``atlassian.Bitbucket`` class is
patched at its import site
(``mcp_atlassian.bitbucket.client.Bitbucket``) with a
:class:`FakeBitbucket` that records constructor kwargs and emulates the
``requests.Session`` setup the production code reads back.
"""

from __future__ import annotations

import base64
import string
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import requests
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.bitbucket.config import BitbucketConfig, is_cloud_host
from mcp_atlassian.utils.environment import (
    BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER,
    BITBUCKET_CLOUD_APP_PASSWORD_HEADER,
    BITBUCKET_CLOUD_USERNAME_HEADER,
    BITBUCKET_DC_PAT_HEADER,
    BITBUCKET_URL_HEADER,
    BitbucketHeaderAuth,
    _detect_bitbucket_auth_from_headers,
)


# ---------------------------------------------------------------------------
# Token / username / URL strategies
# ---------------------------------------------------------------------------

# Token alphabet — printable ASCII without control characters, whitespace,
# or the colon that delimits Basic credentials. Ensures a bearer token or
# DC PAT is self-contained in the Authorization header and will not be
# mistaken for the separator in ``username:password``.
_TOKEN_ALPHABET = string.ascii_letters + string.digits + "-_.~"
tokens: st.SearchStrategy[str] = st.text(
    alphabet=_TOKEN_ALPHABET,
    min_size=1,
    max_size=40,
)

# Usernames: DC / Cloud user slugs are lowercase ASCII with optional
# digits / dots / dashes. A colon is explicitly excluded because Basic
# credentials use it as the separator.
_USERNAME_ALPHABET = string.ascii_lowercase + string.digits + ".-_"
usernames: st.SearchStrategy[str] = st.text(
    alphabet=_USERNAME_ALPHABET,
    min_size=1,
    max_size=20,
)

# Cloud URLs: draw from the three Cloud host families defined in
# Requirement 1 (api.bitbucket.org, bitbucket.org, any *.bitbucket.org
# subdomain). An optional path segment exercises the workspace-parsing
# fallback without changing the classification.
_CLOUD_HOSTS = (
    "https://api.bitbucket.org",
    "https://bitbucket.org",
    "https://bitbucket.org/my-team",
    "https://myteam.bitbucket.org",
    "https://staging.bitbucket.org/ws",
)
cloud_urls: st.SearchStrategy[str] = st.sampled_from(_CLOUD_HOSTS)

# DC URLs: hostnames that deliberately do NOT match the Cloud classifier.
_DC_HOSTS = (
    "https://stash.corp.local",
    "https://stash.corp.local:7990",
    "https://bitbucket.your-company.com",
    "https://bitbucket-internal.example.com",
    "http://localhost:7990",
)
dc_urls: st.SearchStrategy[str] = st.sampled_from(_DC_HOSTS)


def _basic_header(u: str, p: str) -> str:
    """Return the Authorization header value for Basic ``u:p``."""
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode("ascii")


# ===========================================================================
# Section 1 — Per-request header rows (parser-layer truth table)
# ===========================================================================


# Property 3.A — Row A: DCHost URL + DC PAT header → auth_type="pat",
# Authorization: Bearer <PAT>; Cloud bearer / DC URL mix is never emitted.
# Validates Requirements 3.9, 17.1, 17.5.
@given(
    url=dc_urls,
    dc_pat=tokens,
    # Optional noise headers — Cloud credential headers on a DC URL MUST
    # be atomically discarded (Row K second half). Including them in the
    # input should not change the resolved auth.
    stray_cloud_bearer=st.one_of(st.none(), tokens),
    stray_cloud_username=st.one_of(st.none(), usernames),
    stray_cloud_app_password=st.one_of(st.none(), tokens),
)
def test_row_a_dc_url_plus_dc_pat_resolves_to_bearer_pat(
    url: str,
    dc_pat: str,
    stray_cloud_bearer: str | None,
    stray_cloud_username: str | None,
    stray_cloud_app_password: str | None,
) -> None:
    """Row A — DC URL + DC PAT ⇒ ``Authorization: Bearer <dc_pat>``.

    Any Cloud credential headers supplied alongside are discarded
    (Requirement 17.5). The DC PAT value appears verbatim on the
    Authorization header and inside the ``personal_token`` field so a
    regression that escapes/transforms the token is caught immediately.
    """
    headers: dict[str, str] = {
        BITBUCKET_URL_HEADER: url,
        BITBUCKET_DC_PAT_HEADER: dc_pat,
    }
    if stray_cloud_bearer is not None:
        headers[BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER] = stray_cloud_bearer
    if stray_cloud_username is not None:
        headers[BITBUCKET_CLOUD_USERNAME_HEADER] = stray_cloud_username
    if stray_cloud_app_password is not None:
        headers[BITBUCKET_CLOUD_APP_PASSWORD_HEADER] = stray_cloud_app_password

    result = _detect_bitbucket_auth_from_headers(headers)

    assert isinstance(result, BitbucketHeaderAuth)
    assert result.is_cloud is False
    assert result.auth_type == "pat"
    assert result.authorization == f"Bearer {dc_pat}"
    assert result.personal_token == dc_pat
    assert result.unauthorized is False
    # Row K: Cloud bearer/basic headers are atomically discarded on DC URLs.
    assert result.cloud_access_token is None
    assert result.app_password is None


# Property 3.B — Row B: CloudHost URL + Cloud Access Token header →
# auth_type="cloud_bearer", Authorization: Bearer <token>. DC PATs are
# never emitted on Cloud URLs.
# Validates Requirements 3.2, 3.6, 3.8, 17.2, 17.5.
@given(
    url=cloud_urls,
    cloud_bearer=tokens,
    stray_dc_pat=st.one_of(st.none(), tokens),
)
def test_row_b_cloud_url_plus_cloud_bearer_resolves_to_cloud_bearer(
    url: str,
    cloud_bearer: str,
    stray_dc_pat: str | None,
) -> None:
    """Row B — Cloud URL + Cloud bearer ⇒ Cloud OAuth2 bearer.

    A stray DC PAT header is atomically discarded (Row K inverse — a DC
    PAT is never emitted on a Cloud URL).
    """
    headers: dict[str, str] = {
        BITBUCKET_URL_HEADER: url,
        BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: cloud_bearer,
    }
    if stray_dc_pat is not None:
        headers[BITBUCKET_DC_PAT_HEADER] = stray_dc_pat

    result = _detect_bitbucket_auth_from_headers(headers)

    assert result.is_cloud is True
    assert result.auth_type == "cloud_bearer"
    assert result.authorization == f"Bearer {cloud_bearer}"
    assert result.cloud_access_token == cloud_bearer
    assert result.unauthorized is False
    # Row K inverse: DC PAT never leaks into the resolved auth.
    assert result.personal_token is None


# Property 3.C — Row C: CloudHost URL + Cloud Username + App Password →
# auth_type="basic", Authorization: Basic base64(username:app_password).
# Validates Requirements 3.1, 3.6, 3.7, 17.3.
@given(
    url=cloud_urls,
    username=usernames,
    app_password=tokens,
)
def test_row_c_cloud_url_plus_username_and_app_password_resolves_to_basic(
    url: str,
    username: str,
    app_password: str,
) -> None:
    """Row C — Cloud URL + Username + App Password ⇒ Basic base64(u:p)."""
    headers = {
        BITBUCKET_URL_HEADER: url,
        BITBUCKET_CLOUD_USERNAME_HEADER: username,
        BITBUCKET_CLOUD_APP_PASSWORD_HEADER: app_password,
    }

    result = _detect_bitbucket_auth_from_headers(headers)

    assert result.is_cloud is True
    assert result.auth_type == "basic"
    assert result.authorization == _basic_header(username, app_password)
    assert result.username == username
    assert result.app_password == app_password
    assert result.unauthorized is False
    # Row K inverse: no DC PAT / Cloud bearer contamination.
    assert result.personal_token is None
    assert result.cloud_access_token is None


# Property 3.D — Row D: CloudHost URL with no usable Cloud credential →
# unauthorized=True (caller MUST return HTTP 401 with zero outbound
# Bitbucket calls).
# Validates Requirements 3.9, 17.4.
@given(
    url=cloud_urls,
    # Lone halves of the Cloud Basic pair are not usable.
    lone_username=st.one_of(st.none(), usernames),
    lone_app_password=st.one_of(st.none(), tokens),
    # A DC PAT on a Cloud URL is discarded (Row K inverse); it must NOT
    # rescue Row D into a successful auth.
    stray_dc_pat=st.one_of(st.none(), tokens),
)
def test_row_d_cloud_url_without_cloud_creds_is_unauthorized(
    url: str,
    lone_username: str | None,
    lone_app_password: str | None,
    stray_dc_pat: str | None,
) -> None:
    """Row D — Cloud URL without a complete Cloud credential pair ⇒
    ``unauthorized=True``.

    The parser never emits a DC PAT on a Cloud URL (Row K inverse), so
    supplying one does not rescue Row D. Lone halves of the Cloud Basic
    pair (Req 17.3) fall through to Row D as well.
    """
    # Deliberately construct an ``incomplete'' Cloud credential set:
    # at most one of the two Basic halves is present, and the Cloud
    # bearer header is always absent.
    headers: dict[str, str] = {BITBUCKET_URL_HEADER: url}
    # Supply at most one Basic half so the pair is incomplete (Req 17.3).
    if lone_username is not None and lone_app_password is None:
        headers[BITBUCKET_CLOUD_USERNAME_HEADER] = lone_username
    elif lone_app_password is not None and lone_username is None:
        headers[BITBUCKET_CLOUD_APP_PASSWORD_HEADER] = lone_app_password
    # (If both are None we exercise the plain Row D case.)
    if stray_dc_pat is not None:
        headers[BITBUCKET_DC_PAT_HEADER] = stray_dc_pat

    result = _detect_bitbucket_auth_from_headers(headers)

    assert result.is_cloud is True
    assert result.auth_type is None
    assert result.authorization is None
    assert result.unauthorized is True
    # Row K inverse: no DC PAT leakage.
    assert result.personal_token is None
    # Lone halves do not populate the resolved fields.
    assert result.cloud_access_token is None


# Property 3.K — Row K: never mix Cloud bearer with DC URL, and never
# mix DC PAT with Cloud URL. The property fires under any combination
# of credential headers against any URL classification.
# Validates Requirement 17.5.
@given(
    url=st.one_of(cloud_urls, dc_urls),
    dc_pat=st.one_of(st.none(), tokens),
    cloud_bearer=st.one_of(st.none(), tokens),
    cloud_username=st.one_of(st.none(), usernames),
    cloud_app_password=st.one_of(st.none(), tokens),
)
def test_row_k_cloud_bearer_never_mixed_with_dc_url_and_vice_versa(
    url: str,
    dc_pat: str | None,
    cloud_bearer: str | None,
    cloud_username: str | None,
    cloud_app_password: str | None,
) -> None:
    """Row K — cross-classification atomicity (Req 17.5).

    Regardless of which credential headers are present, the resolver
    never emits:

    * a Cloud bearer on a DC URL — ``cloud_access_token`` MUST be
      ``None`` whenever the URL classifies as DC,
    * a DC PAT on a Cloud URL — ``personal_token`` MUST be ``None``
      whenever the URL classifies as Cloud.

    The ``authorization`` field likewise never contains a Cloud bearer
    token substring when ``is_cloud`` is False, nor a DC PAT substring
    when ``is_cloud`` is True.
    """
    headers: dict[str, str] = {BITBUCKET_URL_HEADER: url}
    if dc_pat is not None:
        headers[BITBUCKET_DC_PAT_HEADER] = dc_pat
    if cloud_bearer is not None:
        headers[BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER] = cloud_bearer
    if cloud_username is not None:
        headers[BITBUCKET_CLOUD_USERNAME_HEADER] = cloud_username
    if cloud_app_password is not None:
        headers[BITBUCKET_CLOUD_APP_PASSWORD_HEADER] = cloud_app_password

    result = _detect_bitbucket_auth_from_headers(headers)

    # URL classification is authoritative for is_cloud.
    assert result.is_cloud is is_cloud_host(url)

    if result.is_cloud:
        # Cloud URL ⇒ DC PAT is never emitted.
        assert result.personal_token is None
        if dc_pat is not None and result.authorization is not None:
            # Row K inverse: the DC PAT string MUST NOT appear on a
            # Cloud-URL Authorization header. A Cloud bearer may share
            # a generated value with dc_pat by chance, so we only assert
            # the non-bearer case here.
            if cloud_bearer is None:
                # Without a Cloud bearer, the only way Authorization
                # could match ``Bearer {dc_pat}`` is a leak.
                assert result.authorization != f"Bearer {dc_pat}"
    else:
        # DC URL ⇒ Cloud bearer / Cloud App Password are never emitted.
        assert result.cloud_access_token is None
        assert result.app_password is None
        if cloud_bearer is not None and result.authorization is not None:
            # Without a DC PAT, a ``Bearer {cloud_bearer}`` header would
            # be a leak of a Cloud bearer on a DC URL.
            if dc_pat is None:
                assert result.authorization != f"Bearer {cloud_bearer}"


# ===========================================================================
# Section 2 — Env-layer rows (client-layer truth table)
# ===========================================================================


@dataclass
class FakeBitbucket:
    """Minimal stand-in for :class:`atlassian.Bitbucket`.

    Records constructor kwargs and emulates the subset of
    ``atlassian-python-api``'s auth-session setup that
    :class:`BitbucketClient` reads back (Basic auth tuple on
    ``_session.auth`` and ``Authorization: Bearer`` header for token
    auth). No HTTP is ever issued.
    """

    kwargs: dict[str, Any] = field(default_factory=dict)

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self._session = requests.Session()

        username = kwargs.get("username")
        password = kwargs.get("password")
        token = kwargs.get("token")
        if username and password:
            self._session.auth = (username, password)
        elif token is not None:
            self._session.headers["Authorization"] = f"Bearer {str(token).strip()}"


# All Bitbucket env vars ``BitbucketConfig.from_env`` consults. Each
# test clears the full set so a stray developer-machine variable does
# not leak into the truth table.
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
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SOCKS_PROXY",
)


def _scrub_bitbucket_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every Bitbucket env var so each row is evaluated in isolation."""
    for name in _BITBUCKET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# Row E — DC URL + BITBUCKET_PERSONAL_TOKEN env ⇒ Bearer PAT on the
# outbound session; cloud=False forwarded to atlassian.Bitbucket.
# Validates Requirements 3.5, 4.4.
@given(url=dc_urls, dc_pat=tokens)
@settings(
    max_examples=30,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_e_dc_env_pat_emits_bearer_header_with_cloud_false(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    dc_pat: str,
) -> None:
    """Row E — ``BITBUCKET_PERSONAL_TOKEN`` on a DC URL wires
    ``Authorization: Bearer <PAT>`` and forwards ``cloud=False`` to
    ``atlassian.Bitbucket``.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", dc_pat)

    with patch("mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket):
        cfg = BitbucketConfig.from_env()
        client = BitbucketClient(config=cfg)

    assert cfg.is_cloud is False
    assert cfg.auth_type == "pat"
    assert client.is_cloud is False  # Req 4.4
    assert client.bitbucket.kwargs["cloud"] is False
    assert client.bitbucket.kwargs["token"] == dc_pat
    assert (
        client.bitbucket._session.headers.get("Authorization")
        == f"Bearer {dc_pat}"
    )
    # Row K: no Cloud bearer leakage onto a DC URL.
    assert cfg.cloud_access_token is None
    assert cfg.app_password is None
    # DC PAT path must not populate a Basic auth tuple.
    assert client.bitbucket._session.auth is None


# Row F — DC URL + BITBUCKET_USERNAME + BITBUCKET_PASSWORD env ⇒
# session.auth = (u, p); cloud=False forwarded.
# Validates Requirement 3.5.
@given(url=dc_urls, username=usernames, password=tokens)
@settings(
    max_examples=30,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_f_dc_env_basic_sets_session_auth_tuple(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    username: str,
    password: str,
) -> None:
    """Row F — DC Basic wires ``session.auth = (username, password)``."""
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_USERNAME", username)
    monkeypatch.setenv("BITBUCKET_PASSWORD", password)

    with patch("mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket):
        cfg = BitbucketConfig.from_env()
        client = BitbucketClient(config=cfg)

    assert cfg.is_cloud is False
    assert cfg.auth_type == "basic"
    assert client.is_cloud is False
    assert client.bitbucket.kwargs["cloud"] is False
    assert client.bitbucket._session.auth == (username, password)
    # DC Basic must not also emit a bearer Authorization header.
    assert "Authorization" not in client.bitbucket._session.headers


# Row G — Cloud URL + BITBUCKET_CLOUD_ACCESS_TOKEN env ⇒
# Authorization: Bearer <token>, no session.auth tuple, cloud=True.
# Validates Requirements 3.2, 4.4.
@given(url=cloud_urls, cloud_bearer=tokens)
@settings(
    max_examples=30,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_g_cloud_env_bearer_emits_bearer_header_with_cloud_true(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    cloud_bearer: str,
) -> None:
    """Row G — Cloud OAuth2 bearer from env wires
    ``Authorization: Bearer <token>`` and forwards ``cloud=True``.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", cloud_bearer)

    with patch("mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket):
        cfg = BitbucketConfig.from_env()
        client = BitbucketClient(config=cfg)

    assert cfg.is_cloud is True
    assert cfg.auth_type == "cloud_bearer"
    assert client.is_cloud is True  # Req 4.4
    assert client.bitbucket.kwargs["cloud"] is True
    assert client.bitbucket.kwargs["token"] == cloud_bearer
    assert (
        client.bitbucket._session.headers.get("Authorization")
        == f"Bearer {cloud_bearer}"
    )
    # Req 3.2: bearer MUST NOT be combined with Basic credentials.
    assert client.bitbucket._session.auth is None


# Row H — Cloud URL + BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD env ⇒
# session.auth = (u, app_password); cloud=True forwarded.
# Validates Requirement 3.1.
@given(url=cloud_urls, username=usernames, app_password=tokens)
@settings(
    max_examples=30,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_h_cloud_env_basic_app_password_sets_session_auth_tuple(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    username: str,
    app_password: str,
) -> None:
    """Row H — Cloud Basic with App Password wires
    ``session.auth = (username, app_password)`` and forwards ``cloud=True``.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_USERNAME", username)
    monkeypatch.setenv("BITBUCKET_APP_PASSWORD", app_password)

    with patch("mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket):
        cfg = BitbucketConfig.from_env()
        client = BitbucketClient(config=cfg)

    assert cfg.is_cloud is True
    assert cfg.auth_type == "basic"
    assert cfg.app_password == app_password
    assert client.is_cloud is True
    assert client.bitbucket.kwargs["cloud"] is True
    # Req 3.1: Cloud Basic pairs the username with the *app password*.
    assert client.bitbucket._session.auth == (username, app_password)
    # Basic Cloud must not also emit a bearer Authorization header.
    assert "Authorization" not in client.bitbucket._session.headers


# Row I — Cloud URL with NEITHER credential pair set ⇒ ValueError at
# startup with zero outbound Bitbucket calls.
# Validates Requirement 3.3.
@given(url=cloud_urls)
@settings(
    max_examples=15,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_i_cloud_env_without_creds_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Row I — a Cloud URL with no Cloud credential pair set raises a
    startup ``ValueError`` naming the missing credential set.

    The call fails before any :class:`BitbucketClient` is constructed,
    so no outbound Bitbucket HTTP is ever issued.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)

    with pytest.raises(ValueError, match="Bitbucket Cloud authentication"):
        BitbucketConfig.from_env()


# Row J — BITBUCKET_APP_PASSWORD set with BITBUCKET_USERNAME unset ⇒
# ValueError at startup naming the incomplete Basic pair.
# Validates Requirement 3.4.
@given(url=cloud_urls, app_password=tokens)
@settings(
    max_examples=15,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_j_cloud_env_app_password_without_username_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    app_password: str,
) -> None:
    """Row J — ``BITBUCKET_APP_PASSWORD`` without ``BITBUCKET_USERNAME``
    raises a startup ``ValueError``.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_APP_PASSWORD", app_password)

    with pytest.raises(ValueError, match="BITBUCKET_USERNAME"):
        BitbucketConfig.from_env()


# Cross-cutting Row K (env layer) — Cloud bearer env var + DC URL ⇒
# DC credentials chosen, Cloud bearer ignored. Never emits a Cloud
# bearer on a DC base URL.
# Validates Requirement 17.5 + 23.3.
@given(
    url=dc_urls,
    dc_pat=tokens,
    cloud_bearer=tokens,
)
@settings(
    max_examples=30,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
def test_row_k_env_cloud_bearer_with_dc_url_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    dc_pat: str,
    cloud_bearer: str,
) -> None:
    """Row K (env layer) — Cloud bearer set alongside a DC URL is
    atomically ignored; the DC PAT wins and the outbound Authorization
    header does not carry the Cloud bearer.
    """
    _scrub_bitbucket_env(monkeypatch)
    monkeypatch.setenv("BITBUCKET_URL", url)
    monkeypatch.setenv("BITBUCKET_PERSONAL_TOKEN", dc_pat)
    monkeypatch.setenv("BITBUCKET_CLOUD_ACCESS_TOKEN", cloud_bearer)

    with patch("mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket):
        cfg = BitbucketConfig.from_env()
        client = BitbucketClient(config=cfg)

    # DC wins — no Cloud leakage.
    assert cfg.is_cloud is False
    assert cfg.auth_type == "pat"
    assert cfg.personal_token == dc_pat
    assert cfg.cloud_access_token is None
    assert client.bitbucket.kwargs["cloud"] is False
    auth_header = client.bitbucket._session.headers.get("Authorization")
    assert auth_header == f"Bearer {dc_pat}"
    # Row K: the Cloud bearer string is NEVER on the DC-URL Authorization
    # header. Because dc_pat and cloud_bearer are independently drawn,
    # a coincidence is vanishingly rare; we assert the exact Bearer value.
    assert auth_header != f"Bearer {cloud_bearer}" or dc_pat == cloud_bearer
