"""Property test 14 — Streamlit credential session isolation.

**Validates: Requirements 13.1, 13.2, 13.6**

Hypothesis-driven exercise of
``components.credential_manager.CredentialManager``
(``platform/ui/streamlit-app/components/credential_manager.py``)
implementing the ``platform-gap-fill`` design Property 14:

> *For any* Streamlit session, credentials SHALL exist only in
> memory (``session_state``) and SHALL be cleared on timeout
> (60 min inactivity) or explicit logout.

The tests drive the **pure** :class:`CredentialManager` slice with
fresh ``state`` dicts and a deterministic monotonic clock, so every
example is fully isolated from sibling examples and from Streamlit
itself. The Streamlit ``render_*`` helpers and the default HTTP
validator are out of scope here — Property 14 is a state-machine
invariant on the storage seam, not on the UI surface.

Properties enforced
-------------------

1. **Storage isolation (R13.1)** — after :meth:`store`, the raw token
   bytes are reachable from the state dict *only* via
   ``state["_credential_manager_state"]["credentials"][service]``.
   No sibling ``st.session_state`` key sees the token, no metadata
   field inside the namespaced bucket (``last_activity``,
   ``session_started_at``) carries it, and no recursive walk through
   the foreign state finds it.
2. **Snapshot non-leak (R13.1)** — :meth:`snapshot` is the public
   diagnostic dict that the credentials page renders into ``st.json``.
   It MUST omit the raw ``api_token`` value entirely, both by
   recursive object walk and by JSON serialisation.
3. **Inactivity timeout (R13.2)** — once the wall clock advances by
   strictly more than 60 minutes since the last interaction, the next
   :meth:`get` MUST return ``None`` AND clear the namespaced bucket
   from the state dict, leaving no token byte reachable.
4. **Explicit logout (R13.6)** — :meth:`clear_all` (the path the
   "Oturumu Kapat" button drives) MUST remove the namespaced bucket
   entirely; no token byte SHALL remain reachable from the state
   dict, even if foreign components have written unrelated keys
   alongside the credential bucket.

Why drive the pure class, not the rendered page
-----------------------------------------------

``CredentialManager`` is deliberately split off from the ``render_*``
helpers (see the module docstring of ``credential_manager.py``) so
the storage / lifecycle invariants can be exercised without standing
up Streamlit. The Streamlit-side tests live in the integration suite
(``tests/integration/test_streamlit_credential_page.py``) and the CI
page-presence smoke test; the property assertions here pin the
state-machine invariants those higher-level tests rely on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path bootstrap — make ``components.credential_manager`` importable.
# Mirrors the seam used by ``test_streamlit_dept_switcher_reset.py`` so
# the CredentialManager class resolves without installing the streamlit
# app as a package.
# ---------------------------------------------------------------------------

_STREAMLIT_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "ui" / "streamlit-app"
)
if str(_STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_ROOT))


try:  # pragma: no cover — guarded import (Streamlit may be missing).
    from components.credential_manager import (  # type: ignore[import-not-found]
        CredentialManager,
        StoredCredential,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    CredentialManager = None  # type: ignore[assignment]
    StoredCredential = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    CredentialManager is None,
    reason=(
        "components.credential_manager not importable "
        f"(streamlit missing?); error: {_IMPORT_ERROR!r}"
    ),
)


# ---------------------------------------------------------------------------
# Constants kept in lockstep with credential_manager.py
# ---------------------------------------------------------------------------

#: Module-private constant in ``credential_manager.py``; duplicated here
#: so the property assertions read against a stable literal rather than
#: poking the underscore-prefixed symbol.
_STATE_KEY: str = "_credential_manager_state"

#: Inactivity threshold (Requirement 13.2 — 60 minutes).
_SESSION_TIMEOUT_SECONDS: int = 60 * 60


# ---------------------------------------------------------------------------
# Test seams (clock + validator)
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic monotonic-clock seam.

    Mirrors the helper used by ``test_streamlit_credential_manager.py``
    so the inactivity-window arithmetic is exact and Hypothesis can
    advance the clock by any positive offset without sleeping.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _stub_validator(
    service: str, email: str, token: str
) -> tuple[bool, str | None]:
    """No-op validator — keeps tests free of network IO.

    The default validator in ``credential_manager.py`` issues an HTTP
    request through ``httpx`` to the MCP ``/healthz`` endpoint; the
    property test only exercises the storage / lifecycle seam, so we
    swap in a stub that always succeeds. Storage paths
    (``store``, ``clear_all``, the timeout sweep) never call the
    validator anyway, so this stub mostly silences the
    ``ImportError`` fallback in the default impl when ``httpx`` is
    not on the path of a stripped CI runner.
    """
    return True, None


# ---------------------------------------------------------------------------
# Recursive string walker
# ---------------------------------------------------------------------------


def _walk_strings(obj: Any, _seen: set[int] | None = None) -> Iterator[str]:
    """Yield every string reachable from *obj*.

    Walks dictionaries (keys + values), sequences (lists, tuples,
    sets, frozensets), and dataclass instances (via
    ``__dataclass_fields__``). Cycles are suppressed with an
    identity-keyed ``seen`` set so a credential dict that grows a
    back-reference during a future refactor can't trip the walker
    into infinite recursion.

    The walker intentionally **descends into** :class:`StoredCredential`
    instances so the property tests can verify that the token byte
    stream lives ONLY inside the namespaced bucket — if the
    StoredCredential were skipped, a regression that copied
    ``api_token`` into a sibling key would slip past the assertion.
    """
    if _seen is None:
        _seen = set()
    oid = id(obj)
    if oid in _seen:
        return
    _seen.add(oid)

    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, (bytes, bytearray)):
        try:
            yield obj.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:  # pragma: no cover — defensive
            return
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk_strings(key, _seen)
            yield from _walk_strings(value, _seen)
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            yield from _walk_strings(item, _seen)
        return
    fields = getattr(obj, "__dataclass_fields__", None)
    if fields is not None:
        for field_name in fields:
            yield from _walk_strings(getattr(obj, field_name), _seen)
        return
    # Anything else (int, float, None, custom object): nothing to yield.


def _contains_token(obj: Any, token: str) -> bool:
    """Return ``True`` iff ``token`` is a substring of any string in *obj*."""
    return any(token in s for s in _walk_strings(obj))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Service names align verbatim with
# ``components.credential_manager._SUPPORTED_SERVICES``. Sampling from a
# fixed tuple lets Hypothesis cover all three Atlassian surfaces
# uniformly per example.
_SERVICE = st.sampled_from(("jira", "confluence", "bitbucket"))


def _store_kwargs(service: str) -> dict[str, str]:
    if service == "bitbucket":
        return {"workspace": "example_workspace"}
    return {}

# Email addresses MUST contain ``@`` (``CredentialManager.store`` rejects
# strings without it with ``ValueError``). The local-part alphabet is
# ASCII alphanumerics plus ``.``, ``_``, ``-`` — i.e. the safe subset of
# RFC 5322 — and the domain is fixed at ``@example.com`` so the masking
# helper consistently lands at ``ali***@example.com``.
_EMAIL = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    ),
    min_size=1,
    max_size=24,
).map(lambda local: f"{local}@example.com")

# Tokens carry a verbatim ``pmtoken_`` prefix that uses no hyphen and
# no ``@`` — disjoint from every character class the email and foreign
# strategies emit, so a substring search for the token across the
# foreign state cannot collide by accident. ``min_size=8`` of trailing
# alphanumerics rules out shrunk inputs like a single ``"a"`` that
# would frequently substring-match into unrelated state values.
_TOKEN_TAIL = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    ),
    min_size=8,
    max_size=64,
)
_TOKEN = _TOKEN_TAIL.map(lambda s: f"pmtoken_{s}")

# Inactivity offsets large enough to cross the 60-minute threshold by
# at least a millisecond — strict greater-than so the comparison in
# ``CredentialManager.is_expired`` (``>=``) trips on the first call.
# Upper bound at 24h keeps shrinker output short and the clock
# arithmetic well within float precision.
_INACTIVITY_SECONDS = st.floats(
    min_value=_SESSION_TIMEOUT_SECONDS + 0.001,
    max_value=24 * 60 * 60,
    allow_nan=False,
    allow_infinity=False,
)

# ``st.session_state`` is shared across every component of the
# Streamlit app — the dept switcher, the chat page, the auth
# bootstrap can all park unrelated values alongside the credential
# bucket. Property 14 must hold under that reality, so we
# pre-populate the dict with random nonsense and assert the
# credential token never bleeds into it.
#
# Foreign keys / values use an alphabet that DOES NOT include the
# ``pmtoken_`` token prefix (no underscore-prefixed alphanumeric runs
# over 8+ chars), so substring collisions between the token and an
# unrelated state value are statistically zero.
_FOREIGN_KEY = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    ),
    min_size=1,
    max_size=24,
).filter(lambda k: k != _STATE_KEY)
_FOREIGN_STR = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:!?"
    ),
    max_size=32,
)
_FOREIGN_STATE = st.dictionaries(
    keys=_FOREIGN_KEY,
    values=st.one_of(
        _FOREIGN_STR,
        st.integers(),
        st.booleans(),
        st.lists(_FOREIGN_STR, max_size=4),
        st.dictionaries(_FOREIGN_KEY, _FOREIGN_STR, max_size=4),
    ),
    max_size=8,
)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    service=_SERVICE,
    email=_EMAIL,
    token=_TOKEN,
    foreign=_FOREIGN_STATE,
)
def test_store_keeps_token_only_in_namespaced_bucket(
    service: str, email: str, token: str, foreign: dict
) -> None:
    """**Validates: Requirements 13.1** — storage isolation invariant.

    For every (service, email, token) triple the manager accepts, the
    raw token byte stream MUST appear *only* inside
    ``state[_STATE_KEY]["credentials"][service]``: no foreign
    ``st.session_state`` key sees it, and no metadata field
    (``last_activity``, ``session_started_at``) within the namespaced
    bucket carries it either.

    Pre-populating the state dict with ``foreign`` keys models the
    real Streamlit reality — multiple components share
    ``st.session_state`` and the property must hold regardless of
    what else the user / other components have written.
    """
    state: dict = dict(foreign)
    mgr = CredentialManager(state=state, now=_Clock(), validator=_stub_validator)

    cred = mgr.store(service, email=email, api_token=token, **_store_kwargs(service))

    # Token landed where the contract says it lives — both on the
    # returned dataclass and inside the namespaced bucket.
    assert cred.api_token == token
    bucket = state[_STATE_KEY]
    assert isinstance(bucket, dict)
    assert isinstance(bucket["credentials"][service], StoredCredential)
    assert bucket["credentials"][service].api_token == token

    # No sibling top-level state key carries the token. We slice the
    # namespaced bucket out of the dict and walk every reachable
    # string of the rest; ``foreign`` was generated from an alphabet
    # disjoint from the token's prefix, so any hit here is a real
    # leak (e.g. a regression that mirror-wrote the token to a
    # ``"_last_credential"`` cache key).
    foreign_view = {k: v for k, v in state.items() if k != _STATE_KEY}
    assert not _contains_token(foreign_view, token), (
        "token leaked into foreign st.session_state keys: "
        f"foreign_keys={sorted(foreign_view)!r}, token_prefix={token[:12]!r}"
    )

    # Inside the namespaced bucket, the token must be reachable only
    # via ``credentials[service].api_token``. Slicing ``credentials``
    # out leaves the lifecycle metadata (``last_activity``,
    # ``session_started_at``) which is float-typed and therefore
    # carries no string at all — any substring match here is a
    # regression that started smuggling the token into a metadata
    # field.
    bucket_meta = {k: v for k, v in bucket.items() if k != "credentials"}
    assert not _contains_token(bucket_meta, token), (
        "token leaked into namespaced-bucket metadata: "
        f"meta_keys={sorted(bucket_meta)!r}, token_prefix={token[:12]!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    service=_SERVICE,
    email=_EMAIL,
    token=_TOKEN,
)
def test_snapshot_never_contains_raw_token(
    service: str, email: str, token: str
) -> None:
    """**Validates: Requirements 13.1** — snapshot non-leak invariant.

    :meth:`CredentialManager.snapshot` is the public diagnostic dict
    that the credentials page feeds into ``st.json`` for the
    "Oturum durumu" expander. It MUST omit ``api_token`` entirely so
    a screenshot of the panel is safe to share. We pin the contract
    twice:

    * by recursive object walk (``_contains_token``), which would
      catch a regression that swapped ``asdict`` + ``pop`` for a
      ``__repr__``-based dump that leaks the token into a string
      field;
    * by ``json.dumps`` round-trip, which would catch a regression
      that hid the token behind a ``__str__`` override that lands
      verbatim once the snapshot is JSON-serialised.

    Both checks are needed: the recursive walk catches structured
    leaks (token nested in a sub-dict), the JSON dump catches
    string-fusion leaks (token concatenated into another field).
    """
    state: dict = {}
    mgr = CredentialManager(state=state, now=_Clock(), validator=_stub_validator)
    mgr.store(service, email=email, api_token=token, **_store_kwargs(service))

    snap = mgr.snapshot()

    assert isinstance(snap, dict)
    assert snap.get("active") is True
    # The snapshot's ``credentials`` slot must list the service but
    # never carry the raw token under any key (``email`` is masked,
    # ``api_token`` is popped before serialisation).
    cred_entry = snap.get("credentials", {}).get(service)
    assert cred_entry is not None
    assert "api_token" not in cred_entry, (
        "snapshot retained the api_token key — Requirement 13.1 forbids it"
    )

    assert not _contains_token(snap, token), (
        f"token leaked into snapshot dict: snapshot={snap!r}, "
        f"token_prefix={token[:12]!r}"
    )
    serialised = json.dumps(snap, default=str)
    assert token not in serialised, (
        f"token leaked into snapshot JSON: serialised={serialised!r}, "
        f"token_prefix={token[:12]!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    service=_SERVICE,
    email=_EMAIL,
    token=_TOKEN,
    inactivity=_INACTIVITY_SECONDS,
)
def test_token_cleared_after_inactivity_timeout(
    service: str, email: str, token: str, inactivity: float
) -> None:
    """**Validates: Requirements 13.2** — inactivity timeout invariant.

    After an idle window of strictly more than 60 minutes, the next
    interaction (here :meth:`get`) MUST:

    1. return ``None`` — the credential is no longer observable;
    2. drop the namespaced state bucket — ``_STATE_KEY`` removed
       from the state dict outright;
    3. leave no token byte reachable from the state dict.

    Calling ``get()`` only once after the timeout matters: the
    ``enforce_timeout`` path inside ``get()`` short-circuits before
    ``_ensure_state`` can re-create an empty bucket, so the
    ``_STATE_KEY not in state`` assertion is a tight check on the
    timeout path rather than on a follow-up read.
    """
    state: dict = {}
    clk = _Clock()
    mgr = CredentialManager(state=state, now=clk, validator=_stub_validator)
    mgr.store(service, email=email, api_token=token, **_store_kwargs(service))

    # Sanity: the token IS reachable inside the namespaced bucket
    # before the threshold elapses. If this fails the test would
    # vacuously pass post-timeout, so we assert it explicitly.
    assert _contains_token(state, token), (
        "token unexpectedly not reachable post-store — fixture is broken"
    )

    clk.advance(inactivity)

    # First post-timeout interaction: must return None AND clear.
    assert mgr.get(service) is None
    assert _STATE_KEY not in state, (
        "namespaced bucket survived the inactivity-timeout sweep"
    )
    assert not _contains_token(state, token), (
        f"token survived the inactivity-timeout clear path: "
        f"state_keys={sorted(state)!r}, token_prefix={token[:12]!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(
    service=_SERVICE,
    email=_EMAIL,
    token=_TOKEN,
    foreign=_FOREIGN_STATE,
)
def test_clear_all_removes_every_token_byte(
    service: str, email: str, token: str, foreign: dict
) -> None:
    """**Validates: Requirements 13.6** — explicit logout invariant.

    :meth:`CredentialManager.clear_all` is the code path the
    "Oturumu Kapat" button drives (R13.6). After it runs:

    * the namespaced bucket SHALL be removed from the state dict;
    * no token byte SHALL remain reachable anywhere in the state
      dict (including under any foreign key written by sibling
      Streamlit components);
    * a follow-up :meth:`get` SHALL still return ``None`` — the
      credential cannot silently re-materialise out of a stale
      reference.

    The follow-up ``get()`` call deliberately re-creates an empty
    namespaced bucket (per the ``_ensure_state`` contract); we
    re-check the token-absence invariant after that interaction to
    pin down "an empty bucket cannot smuggle the token back in".
    """
    state: dict = dict(foreign)
    mgr = CredentialManager(state=state, now=_Clock(), validator=_stub_validator)
    mgr.store(service, email=email, api_token=token, **_store_kwargs(service))

    mgr.clear_all()

    # Bucket gone, token unreachable from the state dict.
    assert _STATE_KEY not in state, (
        "clear_all left the namespaced bucket behind"
    )
    assert not _contains_token(state, token), (
        f"token survived clear_all in state_keys={sorted(state)!r}, "
        f"token_prefix={token[:12]!r}"
    )

    # A subsequent read must keep returning None — even though
    # ``get()`` re-creates an empty bucket via ``_ensure_state``,
    # the token MUST remain unreachable.
    assert mgr.get(service) is None
    assert not _contains_token(state, token), (
        f"token re-appeared after a post-clear get(): "
        f"state_keys={sorted(state)!r}, token_prefix={token[:12]!r}"
    )
    assert mgr.get_active_services() == []
