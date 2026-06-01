"""Property test P1 — URL-based ``is_cloud`` classification.

Validates Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7 of the
``bitbucket-cloud-dc-parity`` spec / design Property 1:

    ``is_cloud_host(url)`` returns ``True`` iff the URL's hostname is
    ``api.bitbucket.org``, ``bitbucket.org``, or ends with
    ``.bitbucket.org``. The comparison is case-insensitive. IP literals,
    ``localhost``, and arbitrary corporate hosts classify as DC
    (``False``). ``BitbucketConfig.is_cloud`` equals
    ``is_cloud_host(config.url)`` for every config, and the classifier
    is deterministic — the same URL always produces the same result
    (no residual unconditional ``return False`` path).

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7**

Testing strategy
----------------
The property tests draw URLs from three disjoint generators:

* ``_cloud_url`` — URLs whose hostname is one of the three Cloud host
  families (Requirements 1.2, 1.3, 1.4). Each example's case is
  randomised per character so the case-insensitive classifier
  (Requirement 1.2 / 1.3 / 1.4 — ``host.lower()`` inside
  :func:`is_cloud_host`) is exercised on every run.
* ``_dc_url`` — URLs whose hostname is a corporate host, ``localhost``,
  or an IPv4 literal (Requirement 1.5). None of these end with
  ``.bitbucket.org`` or equal ``bitbucket.org`` / ``api.bitbucket.org``.
* ``_near_miss_url`` — adversarial URLs that deliberately *look* like
  Cloud but are not (e.g. ``bitbucket.org.evil.com``,
  ``not-bitbucket.org``, ``xbitbucket.org``). These guard the suffix
  rule against substring-style false positives.

No real HTTP is ever issued; the tests operate purely on the string
classifier and the :class:`BitbucketConfig.is_cloud` property. The
``BitbucketConfig`` equivalence property also exercises Requirement
1.6 (``is_cloud`` is a property of the current ``url`` attribute) by
mutating ``cfg.url`` after construction and re-reading ``cfg.is_cloud``.
"""

from __future__ import annotations

import string

from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.config import BitbucketConfig, is_cloud_host


# ---------------------------------------------------------------------------
# Hostname / URL strategies
# ---------------------------------------------------------------------------

# Labels are the building blocks of subdomains and corporate hostnames.
# ASCII letters + digits + hyphen, no leading/trailing hyphen (RFC 1035).
_LABEL_ALPHABET = string.ascii_letters + string.digits + "-"
_labels: st.SearchStrategy[str] = st.text(
    alphabet=_LABEL_ALPHABET, min_size=1, max_size=12
).filter(lambda s: not s.startswith("-") and not s.endswith("-"))

# Optional path suffixes that MUST NOT affect classification. Include
# both empty and populated paths so the URL parser exercises its path
# extraction without leaking into hostname comparison.
_path_suffixes: st.SearchStrategy[str] = st.sampled_from(
    (
        "",
        "/",
        "/some-team",
        "/my-team/repo",
        "/rest/api/latest/projects",
        "/2.0/repositories/workspace/repo",
    )
)

_schemes: st.SearchStrategy[str] = st.sampled_from(("https", "http"))


@st.composite
def _mixed_case(draw: st.DrawFn, text: str) -> str:
    """Return *text* with each ASCII letter independently randomised in case.

    The classifier is case-insensitive (Requirement 1.2 / 1.3 / 1.4 —
    implemented via ``host.lower()``). Drawing mixed case on every run
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


@st.composite
def _cloud_url(draw: st.DrawFn) -> str:
    """Generate URLs that MUST classify as Cloud (``is_cloud_host == True``).

    Covers the three host patterns from Requirements 1.2, 1.3, 1.4:

    * ``api.bitbucket.org``            (Requirement 1.2)
    * ``bitbucket.org``                (Requirement 1.3)
    * any subdomain of ``bitbucket.org`` (Requirement 1.4)

    Each hostname is passed through :func:`_mixed_case` so the
    case-insensitive rule is exercised on every example.
    """
    shape = draw(st.sampled_from(("api", "bare", "subdomain")))
    scheme = draw(_schemes)
    path = draw(_path_suffixes)

    if shape == "api":
        host = "api.bitbucket.org"
    elif shape == "bare":
        host = "bitbucket.org"
    else:
        # Arbitrary subdomain, single or multi-level.
        depth = draw(st.integers(min_value=1, max_value=3))
        prefix = ".".join(draw(_labels) for _ in range(depth))
        host = f"{prefix}.bitbucket.org"

    host = draw(_mixed_case(host))
    return f"{scheme}://{host}{path}"


# Corporate TLDs deliberately chosen so no generated hostname can
# accidentally end in ``.bitbucket.org``.
_DC_TLDS: tuple[str, ...] = (
    "com",
    "local",
    "corp",
    "internal",
    "net",
    "io",
    "example",
)


@st.composite
def _dc_corp_url(draw: st.DrawFn) -> str:
    """Generate corporate (non-Cloud) hosts with arbitrary depth.

    None of the generated URLs end with ``.bitbucket.org`` or equal
    ``bitbucket.org`` / ``api.bitbucket.org``. Labels are randomised
    in case so any future case-sensitivity bug (e.g. comparing against
    literal lowercase ``"bitbucket.org"`` without lowercasing the host)
    would still not turn a DC host into a Cloud host.
    """
    scheme = draw(_schemes)
    depth = draw(st.integers(min_value=1, max_value=3))
    labels = [draw(_labels) for _ in range(depth)]
    tld = draw(st.sampled_from(_DC_TLDS))
    host = ".".join([*labels, tld])
    host = draw(_mixed_case(host))
    path = draw(_path_suffixes)
    # Optional port component — MUST NOT affect classification.
    if draw(st.booleans()):
        port = draw(st.integers(min_value=1, max_value=65535))
        return f"{scheme}://{host}:{port}{path}"
    return f"{scheme}://{host}{path}"


@st.composite
def _dc_localhost_url(draw: st.DrawFn) -> str:
    """Generate ``localhost`` URLs with optional port and path.

    Requirement 1.5 — ``localhost`` classifies as DC.
    """
    scheme = draw(_schemes)
    host = draw(_mixed_case("localhost"))
    path = draw(_path_suffixes)
    if draw(st.booleans()):
        port = draw(st.integers(min_value=1, max_value=65535))
        return f"{scheme}://{host}:{port}{path}"
    return f"{scheme}://{host}{path}"


@st.composite
def _dc_ip_url(draw: st.DrawFn) -> str:
    """Generate IPv4-literal URLs.

    Requirement 1.5 — IP literals classify as DC.
    """
    scheme = draw(_schemes)
    octets = [draw(st.integers(min_value=0, max_value=255)) for _ in range(4)]
    host = ".".join(str(o) for o in octets)
    path = draw(_path_suffixes)
    if draw(st.booleans()):
        port = draw(st.integers(min_value=1, max_value=65535))
        return f"{scheme}://{host}:{port}{path}"
    return f"{scheme}://{host}{path}"


_dc_url: st.SearchStrategy[str] = st.one_of(
    _dc_corp_url(), _dc_localhost_url(), _dc_ip_url()
)


# Adversarial near-miss strings. Each template below contains the
# literal text ``bitbucket.org`` somewhere other than at the tail of
# the hostname, so the classifier MUST return ``False``. These guard
# against a naive ``"bitbucket.org" in host`` (substring) implementation.
_NEAR_MISS_HOSTS: tuple[str, ...] = (
    "bitbucket.org.evil.com",
    "bitbucket.org.attacker.net",
    "notbitbucket.org",
    "xbitbucket.org",
    "fakebitbucket.org",
    "mybitbucket.org.internal",
    "api.bitbucket.org.example.com",
    "bitbucket-org.com",
    "bitbucket.com",
    "bitbucket.io",
    "bbucket.org",
)


@st.composite
def _near_miss_url(draw: st.DrawFn) -> str:
    """Generate adversarial hostnames that look like Cloud but are not.

    Each hostname deliberately contains ``bitbucket.org`` or a visually
    similar fragment but does NOT satisfy the suffix rule from
    Requirement 1.4 — the ``.bitbucket.org`` suffix must be an actual
    DNS-label boundary. ``notbitbucket.org`` fails because the host
    ``notbitbucket.org`` ends with ``.org``, not ``.bitbucket.org``
    (it lacks the leading ``.`` before ``bitbucket``).
    ``bitbucket.org.evil.com`` fails because the host ends with
    ``.evil.com``.
    """
    host = draw(st.sampled_from(_NEAR_MISS_HOSTS))
    host = draw(_mixed_case(host))
    scheme = draw(_schemes)
    path = draw(_path_suffixes)
    return f"{scheme}://{host}{path}"


# ---------------------------------------------------------------------------
# Property 1.A — Cloud hosts classify as Cloud
# ---------------------------------------------------------------------------


@given(url=_cloud_url())
def test_cloud_urls_classify_as_cloud(url: str) -> None:
    """For any URL whose hostname is ``api.bitbucket.org``,
    ``bitbucket.org``, or ends with ``.bitbucket.org``,
    :func:`is_cloud_host` returns ``True``.

    Validates Requirements 1.2, 1.3, 1.4.
    """
    assert is_cloud_host(url) is True


# ---------------------------------------------------------------------------
# Property 1.B — DC hosts classify as DC
# ---------------------------------------------------------------------------


@given(url=_dc_url)
def test_dc_urls_classify_as_dc(url: str) -> None:
    """For any corporate host, ``localhost``, or IPv4-literal URL,
    :func:`is_cloud_host` returns ``False``.

    Validates Requirement 1.5. This is the "no unconditional ``True``
    path" complement of Requirement 1.7 — an accidental
    ``return True`` regression in the classifier would immediately
    fail here.
    """
    assert is_cloud_host(url) is False


# ---------------------------------------------------------------------------
# Property 1.C — Near-miss hostnames classify as DC
# ---------------------------------------------------------------------------


@given(url=_near_miss_url())
def test_near_miss_hosts_classify_as_dc(url: str) -> None:
    """Adversarial hostnames that contain ``bitbucket.org`` as a
    substring — but not as a suffix at a DNS-label boundary — classify
    as DC.

    Validates Requirement 1.4 (the suffix rule must be a true
    ``.bitbucket.org`` label-boundary match, not a substring match) and
    Requirement 1.5 (everything else is DC).
    """
    assert is_cloud_host(url) is False


# ---------------------------------------------------------------------------
# Property 1.D — Case-insensitive classification
# ---------------------------------------------------------------------------


@given(url=_cloud_url())
def test_classifier_is_case_insensitive_on_cloud_hosts(url: str) -> None:
    """Classification is stable under arbitrary case folding of the URL.

    For any Cloud URL, ``is_cloud_host`` returns the same ``True`` for
    its lowercased, uppercased, and title-cased spellings. The
    underlying implementation lowercases the hostname before comparing,
    so this property pins Requirement 1 — "case-insensitive" — at every
    generated example.
    """
    assert is_cloud_host(url) is True
    assert is_cloud_host(url.lower()) is True
    assert is_cloud_host(url.upper()) is True
    # Title-case stresses the mixed-case path most aggressively because
    # every word boundary toggles case.
    assert is_cloud_host(url.title()) is True


@given(url=_dc_url)
def test_classifier_is_case_insensitive_on_dc_hosts(url: str) -> None:
    """Classification is stable under arbitrary case folding of the URL.

    Symmetric to the Cloud case-insensitivity property: a DC host stays
    classified as DC regardless of case. Guards against a regression
    where uppercasing introduces a coincidental match.
    """
    assert is_cloud_host(url) is False
    assert is_cloud_host(url.lower()) is False
    assert is_cloud_host(url.upper()) is False


# ---------------------------------------------------------------------------
# Property 1.E — Determinism
# ---------------------------------------------------------------------------


@given(url=st.one_of(_cloud_url(), _dc_url, _near_miss_url()))
def test_classifier_is_deterministic(url: str) -> None:
    """For any URL, calling :func:`is_cloud_host` repeatedly returns the
    same boolean.

    Determinism is the structural guarantee behind Requirement 1.6
    (``is_cloud`` is a *property* of the URL, not a cached side effect)
    and Requirement 1.7 (no hidden unconditional path). A non-pure
    implementation — e.g. one that toggled based on a mutable module
    global — would fail this property within a handful of examples.
    """
    first = is_cloud_host(url)
    for _ in range(4):
        assert is_cloud_host(url) is first


# ---------------------------------------------------------------------------
# Property 1.F — ``BitbucketConfig.is_cloud`` equals ``is_cloud_host(url)``
# ---------------------------------------------------------------------------


@st.composite
def _bitbucket_configs(draw: st.DrawFn) -> BitbucketConfig:
    """Generate a valid :class:`BitbucketConfig` for either mode.

    The config is constructed directly (not via ``from_env``) so we can
    exercise the full URL distribution — including adversarial
    near-miss hosts — without depending on Cloud-credential truth table
    rows. The auth fields are populated to satisfy the dataclass'
    shape but never exercised by the classifier.
    """
    url = draw(st.one_of(_cloud_url(), _dc_url, _near_miss_url()))
    # The ``is_cloud`` property reads only ``self.url`` — auth_type and
    # credentials are irrelevant to this property. We pick a shape that
    # keeps the dataclass internally consistent for both modes.
    if is_cloud_host(url):
        return BitbucketConfig(
            url=url,
            auth_type="cloud_bearer",
            cloud_access_token="dummy-bearer",
        )
    return BitbucketConfig(
        url=url,
        auth_type="pat",
        personal_token="dummy-pat",
    )


@given(cfg=_bitbucket_configs())
def test_config_is_cloud_matches_is_cloud_host(cfg: BitbucketConfig) -> None:
    """For any :class:`BitbucketConfig`,
    ``cfg.is_cloud == is_cloud_host(cfg.url)``.

    Validates Requirements 1.1 (``is_cloud`` is derived from the URL)
    and 1.6 (``is_cloud`` is exposed as a property of the current
    ``url`` attribute).
    """
    assert cfg.is_cloud is is_cloud_host(cfg.url)


# ---------------------------------------------------------------------------
# Property 1.G — ``is_cloud`` tracks mutations to ``url``
# ---------------------------------------------------------------------------


@given(
    initial_url=st.one_of(_cloud_url(), _dc_url, _near_miss_url()),
    next_url=st.one_of(_cloud_url(), _dc_url, _near_miss_url()),
)
def test_config_is_cloud_tracks_url_mutation(
    initial_url: str, next_url: str
) -> None:
    """``BitbucketConfig.is_cloud`` is recomputed from the current
    :attr:`url` on every access — it is not cached at construction time.

    Validates Requirement 1.6: "the ``is_cloud`` property value is
    computed from the current ``url`` attribute". A regression that
    cached the classification at ``__init__`` would fail this property
    whenever *initial_url* and *next_url* classify differently.

    Validates Requirement 1.7 indirectly: a residual ``return False``
    (or ``return True``) implementation would also fail here because
    the property's return value would not follow ``url`` changes.
    """
    cfg = BitbucketConfig(
        url=initial_url,
        auth_type="pat" if not is_cloud_host(initial_url) else "cloud_bearer",
        personal_token="dummy" if not is_cloud_host(initial_url) else None,
        cloud_access_token=None if not is_cloud_host(initial_url) else "dummy",
    )
    assert cfg.is_cloud is is_cloud_host(initial_url)

    # Mutate the URL in place. The auth fields are deliberately left
    # untouched; the ``is_cloud`` property depends only on ``url``.
    cfg.url = next_url
    assert cfg.is_cloud is is_cloud_host(next_url)


# ---------------------------------------------------------------------------
# Property 1.H — No unconditional ``False`` path
# ---------------------------------------------------------------------------


@given(url=_cloud_url())
def test_no_unconditional_false_path(url: str) -> None:
    """Requirement 1.7 — there is no residual ``return False``
    implementation of ``is_cloud``.

    Symmetric to Property 4.D in ``test_client_cloud_flag_property``
    (which guards against a residual ``cloud=False`` hardcode): for
    every generated Cloud URL, :func:`is_cloud_host` MUST return
    ``True`` and :attr:`BitbucketConfig.is_cloud` MUST mirror that
    value. A stubbed-out ``return False`` implementation would be
    caught by the very first example drawn here.
    """
    assert is_cloud_host(url) is True

    cfg = BitbucketConfig(
        url=url,
        auth_type="cloud_bearer",
        cloud_access_token="dummy-bearer",
    )
    assert cfg.is_cloud is True
