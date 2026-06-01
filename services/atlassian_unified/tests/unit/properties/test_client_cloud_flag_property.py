"""Property test P4 — ``BitbucketClient`` cloud-flag forwarding invariant.

Validates Requirements 4.1, 4.2, 4.3, 4.4 of the
``bitbucket-cloud-dc-parity`` spec / design Property 4:

    For any :class:`~mcp_atlassian.bitbucket.config.BitbucketConfig`
    (varying ``url``, ``auth_type``, and credentials), the
    ``cloud=`` kwarg forwarded to :class:`atlassian.Bitbucket` inside
    :class:`~mcp_atlassian.bitbucket.client.BitbucketClient.__init__`
    equals ``config.is_cloud`` for PAT and Basic auth, and is
    unconditionally ``True`` for the ``cloud_bearer`` variant. The
    :attr:`BitbucketClient.is_cloud` property always mirrors
    ``config.is_cloud``. The effective mode of a per-request fetcher —
    whether it comes from the global config or from a Cloud URL header
    override — equals :func:`~mcp_atlassian.bitbucket.config.is_cloud_host`
    applied to the resolved URL.

Why a property test
-------------------
The existing unit tests in
:mod:`tests.unit.bitbucket.test_client_dual_mode` cover specific truth-
table rows with hand-curated hosts. This property test complements those
by generating a broad distribution of URL + auth-type + credential
combinations and asserting the single invariant
``kwargs["cloud"] == expected_cloud`` holds for *every* generated
:class:`BitbucketConfig` — the strongest anti-regression guard against a
future refactor accidentally reintroducing an unconditional
``cloud=False`` path (Requirement 4.3).

**Validates: Requirements 4.1, 4.2, 4.3, 4.4**

Testing strategy
----------------
Random :class:`BitbucketConfig` values are drawn across three axes:

1. **URL** — a mixture of Cloud hosts
   (``api.bitbucket.org``, ``bitbucket.org``, ``*.bitbucket.org``) and
   DC hosts (``stash.corp.local``, ``bitbucket.your-company.com``,
   ``localhost``, IPv4 literals). Hostnames are drawn with mixed case
   so the case-insensitive classifier in :func:`is_cloud_host` is
   exercised on every run.
2. **Auth type** — ``"pat"``, ``"basic"``, and ``"cloud_bearer"`` (the
   last only paired with Cloud URLs, matching the from-env truth table).
3. **Credentials** — dummy tokens / usernames / passwords that never
   reach the network; a :class:`FakeBitbucket` stand-in records the
   constructor kwargs so the test can assert the forwarded
   ``cloud=`` flag without issuing HTTP.

The :class:`FakeBitbucket` helper mirrors the pattern in
:mod:`tests.unit.bitbucket.test_client_dual_mode` so the two test
modules share an understanding of how the client wires ``cloud=`` into
``atlassian.Bitbucket``.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import requests
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.bitbucket.config import BitbucketConfig, is_cloud_host


# ---------------------------------------------------------------------------
# Fake atlassian.Bitbucket — mirrors the pattern in test_client_dual_mode.py
# ---------------------------------------------------------------------------


@dataclass
class FakeBitbucket:
    """Record-only stand-in for :class:`atlassian.Bitbucket`.

    Captures the constructor kwargs so the property test can assert that
    ``cloud=`` was forwarded with the expected value. Emulates just
    enough of ``atlassian-python-api``'s session setup for
    :class:`BitbucketClient.__init__` to complete without hitting the
    network — a real :class:`requests.Session` is attached so the
    client's post-ctor wiring (custom headers, SSL, proxy, Cloud auth
    re-assignment) exercises the same code paths as production.
    """

    kwargs: dict[str, Any] = field(default_factory=dict)

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)
        self._session = requests.Session()

        # Reproduce the subset of ``atlassian-python-api``'s auth session
        # setup that :class:`BitbucketClient` observes on
        # ``self.bitbucket._session``. These mirrors let the client's
        # Cloud-auth re-assignment block in ``client.py`` run to
        # completion.
        username = kwargs.get("username")
        password = kwargs.get("password")
        token = kwargs.get("token")
        if username and password:
            self._session.auth = (username, password)
        elif token is not None:
            self._session.headers["Authorization"] = f"Bearer {str(token).strip()}"


@pytest.fixture
def patch_bitbucket():
    """Patch ``atlassian.Bitbucket`` at the import site in ``client.py``.

    Yields the patched class reference so the test body can introspect
    the most recently constructed :class:`FakeBitbucket` via
    ``client.bitbucket``.
    """
    with patch(
        "mcp_atlassian.bitbucket.client.Bitbucket", new=FakeBitbucket
    ) as patched:
        yield patched


# ---------------------------------------------------------------------------
# Hypothesis strategies for Cloud and DC URLs
# ---------------------------------------------------------------------------

# Short, URL-safe label strategy used to synthesise subdomain and path
# fragments. Keeping the alphabet narrow avoids conflating "does the
# classifier survive unusual Unicode" (covered by Property 1) with "does
# the cloud flag forward correctly" (the invariant under test here).
_LABEL_ALPHABET = string.ascii_letters + string.digits + "-"


@st.composite
def _mixed_case(draw: st.DrawFn, text: str) -> str:
    """Return *text* with each ASCII letter independently randomised in case.

    The classifier is case-insensitive; drawing mixed case on every run
    gives us a steady trickle of ``API.BITBUCKET.ORG``,
    ``Api.Bitbucket.Org``, etc. without inflating the example count with
    a dedicated permutation axis.
    """
    flipped: list[str] = []
    for ch in text:
        if ch.isalpha() and draw(st.booleans()):
            flipped.append(ch.upper() if ch.islower() else ch.lower())
        else:
            flipped.append(ch)
    return "".join(flipped)


_labels: st.SearchStrategy[str] = st.text(
    alphabet=_LABEL_ALPHABET, min_size=1, max_size=12
).filter(lambda s: not s.startswith("-") and not s.endswith("-"))


@st.composite
def _cloud_url(draw: st.DrawFn) -> str:
    """Generate URLs that MUST classify as Cloud (``is_cloud_host == True``).

    The three branches cover the three host patterns in Requirement 1.2
    / 1.3 / 1.4: the API host, the bare tenant host, and an arbitrary
    subdomain of ``bitbucket.org``.
    """
    shape = draw(st.sampled_from(("api", "bare", "subdomain")))
    scheme = draw(st.sampled_from(("https", "http")))
    path_suffix = draw(st.sampled_from(("", "/", "/some-team", "/my-team/repo")))
    if shape == "api":
        host = "api.bitbucket.org"
    elif shape == "bare":
        host = "bitbucket.org"
    else:
        subdomain = draw(_labels)
        host = f"{subdomain}.bitbucket.org"
    host = draw(_mixed_case(host))
    return f"{scheme}://{host}{path_suffix}"


@st.composite
def _dc_url(draw: st.DrawFn) -> str:
    """Generate URLs that MUST classify as DC (``is_cloud_host == False``).

    Covers the three DC shapes enumerated in the existing config unit
    tests (``test_config_is_cloud.py``): custom corporate hosts,
    localhost, and IPv4 literals. None of these end with
    ``.bitbucket.org`` or equal ``bitbucket.org`` / ``api.bitbucket.org``.
    """
    shape = draw(st.sampled_from(("corp", "localhost", "ip")))
    scheme = draw(st.sampled_from(("https", "http")))
    path_suffix = draw(st.sampled_from(("", "/", "/bitbucket", "/projects")))
    if shape == "corp":
        label = draw(_labels)
        # Deliberately avoid any ``bitbucket.org`` suffix — the
        # classifier rule is an exact suffix match, so ``*.bitbucket.org``
        # is the only excluded TLD here.
        tld = draw(st.sampled_from(("com", "local", "corp", "internal", "net")))
        host = f"{label}.{tld}"
    elif shape == "localhost":
        host = "localhost"
        if draw(st.booleans()):
            port = draw(st.integers(min_value=1024, max_value=65535))
            host = f"{host}:{port}"
    else:
        octets = [draw(st.integers(min_value=0, max_value=255)) for _ in range(4)]
        host = ".".join(str(o) for o in octets)
    host = draw(_mixed_case(host))
    return f"{scheme}://{host}{path_suffix}"


# Union strategy — every run draws both Cloud and DC URLs with roughly
# equal weight so a refactor that accidentally flipped one mode's
# cloud-flag forwarding would fail within a handful of examples.
_any_bitbucket_url: st.SearchStrategy[str] = st.one_of(_cloud_url(), _dc_url())


# ---------------------------------------------------------------------------
# Hypothesis strategies for BitbucketConfig instances
# ---------------------------------------------------------------------------

_tokens: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "-_", min_size=1, max_size=24
)
_usernames: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_lowercase + string.digits + "._-",
    min_size=1,
    max_size=16,
)
_passwords: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "@#$%^&*",
    min_size=1,
    max_size=24,
)


@st.composite
def _pat_configs(draw: st.DrawFn) -> BitbucketConfig:
    """Random PAT-auth :class:`BitbucketConfig` for either mode."""
    return BitbucketConfig(
        url=draw(_any_bitbucket_url),
        auth_type="pat",
        personal_token=draw(_tokens),
    )


@st.composite
def _basic_configs(draw: st.DrawFn) -> BitbucketConfig:
    """Random Basic-auth :class:`BitbucketConfig` for either mode.

    On DC URLs we populate the ``password`` field; on Cloud URLs we
    populate ``app_password``. The :class:`BitbucketClient` forwards
    ``config.password or config.app_password`` as the ``password=``
    kwarg, so either shape satisfies the constructor's Basic-auth
    expectations and the invariant under test (``cloud == is_cloud``)
    holds independently of which credential field is set.
    """
    url = draw(_any_bitbucket_url)
    username = draw(_usernames)
    if is_cloud_host(url):
        return BitbucketConfig(
            url=url,
            auth_type="basic",
            username=username,
            app_password=draw(_passwords),
        )
    return BitbucketConfig(
        url=url,
        auth_type="basic",
        username=username,
        password=draw(_passwords),
    )


@st.composite
def _cloud_bearer_configs(draw: st.DrawFn) -> BitbucketConfig:
    """Random ``cloud_bearer`` :class:`BitbucketConfig`.

    The ``cloud_bearer`` auth type is Cloud-only by construction — it is
    produced by :meth:`BitbucketConfig.from_env` only when
    ``BITBUCKET_URL`` resolves to a CloudHost. We mirror that invariant
    here by drawing the URL from :func:`_cloud_url` so the generated
    config is never internally inconsistent (Cloud auth paired with a
    DC URL would be rejected by from-env row K).
    """
    return BitbucketConfig(
        url=draw(_cloud_url()),
        auth_type="cloud_bearer",
        cloud_access_token=draw(_tokens),
    )


# ---------------------------------------------------------------------------
# Property A — PAT branch forwards ``cloud=config.is_cloud`` for every config
# ---------------------------------------------------------------------------


@given(cfg=_pat_configs())
def test_pat_client_forwards_is_cloud_to_atlassian_bitbucket(
    cfg: BitbucketConfig, patch_bitbucket
) -> None:
    """For any PAT-auth config, the client forwards
    ``cloud=config.is_cloud`` to :class:`atlassian.Bitbucket` and its
    :attr:`BitbucketClient.is_cloud` property mirrors the same value.

    Validates Requirements 4.1 (Cloud URL ⇒ ``cloud=True``), 4.2 (DC
    URL ⇒ ``cloud=False``), 4.4 (``BitbucketClient.is_cloud`` is
    URL-derived and read-only).
    """
    expected_cloud = is_cloud_host(cfg.url)
    assert cfg.is_cloud is expected_cloud

    client = BitbucketClient(config=cfg)

    # The ``cloud=`` kwarg forwarded to ``atlassian.Bitbucket`` matches
    # the config's ``is_cloud`` for every generated URL.
    assert client.bitbucket.kwargs["cloud"] is expected_cloud
    # The PAT branch forwards the token and MUST NOT leak Basic creds.
    assert client.bitbucket.kwargs["token"] == cfg.personal_token
    assert client.bitbucket.kwargs.get("username") is None
    assert client.bitbucket.kwargs.get("password") is None
    # ``BitbucketClient.is_cloud`` mirrors the config (Req 4.4).
    assert client.is_cloud is expected_cloud


# ---------------------------------------------------------------------------
# Property B — Basic branch forwards ``cloud=config.is_cloud`` for every config
# ---------------------------------------------------------------------------


@given(cfg=_basic_configs())
def test_basic_client_forwards_is_cloud_to_atlassian_bitbucket(
    cfg: BitbucketConfig, patch_bitbucket
) -> None:
    """For any Basic-auth config (DC password or Cloud app password),
    the client forwards ``cloud=config.is_cloud`` to
    :class:`atlassian.Bitbucket` and the ``BitbucketClient.is_cloud``
    property mirrors the same value.

    Validates Requirements 4.1, 4.2, 4.4.
    """
    expected_cloud = is_cloud_host(cfg.url)
    assert cfg.is_cloud is expected_cloud

    client = BitbucketClient(config=cfg)

    assert client.bitbucket.kwargs["cloud"] is expected_cloud
    assert client.bitbucket.kwargs["username"] == cfg.username
    # The client passes ``config.password or config.app_password`` as
    # the ``password=`` kwarg — either credential field satisfies the
    # invariant.
    forwarded_password = client.bitbucket.kwargs["password"]
    assert forwarded_password == (cfg.password or cfg.app_password)
    # Basic auth MUST NOT set ``token=``.
    assert client.bitbucket.kwargs.get("token") is None
    # Property under test — Req 4.4.
    assert client.is_cloud is expected_cloud


# ---------------------------------------------------------------------------
# Property C — ``cloud_bearer`` always forwards ``cloud=True``
# ---------------------------------------------------------------------------


@given(cfg=_cloud_bearer_configs())
def test_cloud_bearer_client_always_forwards_cloud_true(
    cfg: BitbucketConfig, patch_bitbucket
) -> None:
    """For any ``cloud_bearer`` config, the ``cloud=`` kwarg forwarded to
    :class:`atlassian.Bitbucket` is unconditionally ``True``, and the
    ``BitbucketClient.is_cloud`` property is also ``True``.

    ``cloud_bearer`` is a Cloud-only auth type — the configured URL is
    always a CloudHost by construction (see :func:`_cloud_bearer_configs`
    and the from-env resolver). This property pins Requirement 4.1 for
    the Cloud bearer branch independently of the generated URL's exact
    shape.

    Validates Requirement 4.1 and the ``cloud_bearer``-specific bullet in
    :meth:`BitbucketClient.__init__`.
    """
    assert cfg.is_cloud is True  # sanity — strategy invariant

    client = BitbucketClient(config=cfg)

    assert client.bitbucket.kwargs["cloud"] is True
    assert client.bitbucket.kwargs["token"] == cfg.cloud_access_token
    # Cloud bearer MUST NOT combine with Basic credentials (Req 3.2).
    assert client.bitbucket.kwargs.get("username") is None
    assert client.bitbucket.kwargs.get("password") is None
    assert client.is_cloud is True


# ---------------------------------------------------------------------------
# Property D — No residual ``cloud=False`` path for Cloud URLs (Req 4.3)
# ---------------------------------------------------------------------------


@st.composite
def _any_auth_on_cloud_url(draw: st.DrawFn) -> BitbucketConfig:
    """Any supported auth type paired with a CloudHost URL.

    This strategy is the lever for Requirement 4.3: any Cloud URL — no
    matter which auth_type accompanies it — MUST result in
    ``cloud=True`` being forwarded to :class:`atlassian.Bitbucket`. If a
    developer reintroduced a hardcoded ``cloud=False`` in any of the
    three auth branches, at least one example from this strategy would
    hit the `assert kwargs["cloud"] is True` below and fail.
    """
    url = draw(_cloud_url())
    auth_type = draw(st.sampled_from(("pat", "basic", "cloud_bearer")))
    if auth_type == "pat":
        return BitbucketConfig(
            url=url,
            auth_type="pat",
            personal_token=draw(_tokens),
        )
    if auth_type == "cloud_bearer":
        return BitbucketConfig(
            url=url,
            auth_type="cloud_bearer",
            cloud_access_token=draw(_tokens),
        )
    # basic on a Cloud URL uses the app_password field
    return BitbucketConfig(
        url=url,
        auth_type="basic",
        username=draw(_usernames),
        app_password=draw(_passwords),
    )


@given(cfg=_any_auth_on_cloud_url())
def test_no_unconditional_cloud_false_path_for_cloud_urls(
    cfg: BitbucketConfig, patch_bitbucket
) -> None:
    """Req 4.3 — there is no residual ``cloud=False`` hardcode anywhere
    in :class:`BitbucketClient.__init__`.

    For every Cloud URL, regardless of auth type, the forwarded
    ``cloud=`` flag MUST be ``True``. This property acts as a
    structural guard: a hypothetical future refactor that accidentally
    reintroduced the pre-feature ``Bitbucket(cloud=False)`` call on any
    branch would produce at least one counter-example here.
    """
    assert cfg.is_cloud is True  # sanity — strategy invariant

    client = BitbucketClient(config=cfg)

    assert client.bitbucket.kwargs["cloud"] is True, (
        f"Cloud URL {cfg.url!r} with auth_type={cfg.auth_type!r} "
        "did not forward cloud=True to atlassian.Bitbucket"
    )
    assert client.is_cloud is True


# ---------------------------------------------------------------------------
# Property E — Per-request fetcher is_cloud equals effective mode
# ---------------------------------------------------------------------------
#
# Requirement 4.4 mandates that :attr:`BitbucketClient.is_cloud` reflects
# the resolved operating mode for the *current request*, including any
# per-request override supplied via the ``X-Atlassian-Bitbucket-Url``
# header. The dependency layer implements that override by constructing
# a fresh :class:`BitbucketConfig` whose ``url`` is the header value,
# then instantiating a new :class:`BitbucketClient` against it (see
# ``servers/dependencies.py::_get_fetcher``). We model the same
# construction pattern here: for any combination of (global URL,
# override URL) we build a per-request config with the effective URL
# and assert the resulting client's ``is_cloud`` equals
# :func:`is_cloud_host` applied to that effective URL — independent of
# whether the effective URL came from the global config or the header.


@given(
    global_url=_any_bitbucket_url,
    header_url=st.one_of(st.none(), _any_bitbucket_url),
    auth_type=st.sampled_from(("pat", "basic", "cloud_bearer")),
)
def test_per_request_fetcher_is_cloud_reflects_effective_mode(
    global_url: str,
    header_url: str | None,
    auth_type: str,
    patch_bitbucket,
) -> None:
    """For any per-request fetcher, ``BitbucketClient.is_cloud`` equals
    :func:`is_cloud_host` applied to the resolved URL — whether that URL
    came from the global config or from the
    ``X-Atlassian-Bitbucket-Url`` header override.

    Validates Requirement 4.4.

    The resolver precedence used by ``servers/dependencies.py`` is
    header URL > global URL. We pre-compute the effective URL here and
    construct a :class:`BitbucketConfig` with it, matching the shape
    produced by ``_get_fetcher`` when a Cloud URL header is present
    alongside Cloud credential headers (rows B, C of the auth truth
    table) and by the global fallback otherwise.

    Pairing a ``cloud_bearer`` auth type with a DCHost effective URL is
    an internally-inconsistent config that the from-env resolver would
    never produce (row K is filtered at parse time), so we skip that
    combination to stay within the feature's supported state space.
    """
    effective_url = header_url if header_url is not None else global_url
    expected_cloud = is_cloud_host(effective_url)

    # ``cloud_bearer`` is only ever paired with Cloud URLs by the
    # config resolver; skip the internally-inconsistent combination.
    if auth_type == "cloud_bearer" and not expected_cloud:
        return

    if auth_type == "pat":
        cfg = BitbucketConfig(
            url=effective_url,
            auth_type="pat",
            personal_token="per-request-pat",
        )
    elif auth_type == "cloud_bearer":
        cfg = BitbucketConfig(
            url=effective_url,
            auth_type="cloud_bearer",
            cloud_access_token="per-request-bearer",
        )
    else:  # basic
        if expected_cloud:
            cfg = BitbucketConfig(
                url=effective_url,
                auth_type="basic",
                username="alice",
                app_password="per-request-app-pass",
            )
        else:
            cfg = BitbucketConfig(
                url=effective_url,
                auth_type="basic",
                username="alice",
                password="per-request-dc-password",
            )

    client = BitbucketClient(config=cfg)

    # Req 4.4 — the per-request fetcher's ``is_cloud`` equals the
    # effective mode (global or header override). The effective URL is
    # already encoded into ``cfg.url`` by the time the client is
    # constructed, so the assertion reduces to the core invariant.
    assert client.is_cloud is expected_cloud
    # And the forwarded ``cloud=`` kwarg matches — the client does not
    # re-classify based on anything except the config URL.
    if auth_type == "cloud_bearer":
        assert client.bitbucket.kwargs["cloud"] is True
    else:
        assert client.bitbucket.kwargs["cloud"] is expected_cloud
