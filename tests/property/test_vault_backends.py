"""Vault backend equivalence property tests.

For both pluggable backends (``HashicorpBackend`` against an in-process
``httpx.MockTransport`` simulating a Hashicorp KV-v2 mount, and
``LocalDevBackend`` against a libsodium-encrypted file under ``tmp_path``),
the following round-trip property holds for every well-formed
``(VaultPath, payload)`` pair:

    backend.write(p, v)
    backend.read(p) == v

This is the headline contract for pluggable backend behavior
(``VAULT_BACKEND`` in {``hashicorp``, ``local-dev``}). The two backends are
constructed independently, but exposed to the test through the
:class:`vault_client.client.VaultClient` Protocol so the assertion is
the *same* against either implementation. Hypothesis drives the search
across path shapes and flat string payloads.

Notes on the Hashicorp simulation
---------------------------------

The ``httpx.MockTransport`` here is *not* a generic Vault stub: it
implements just enough of the KV-v2 wire shape (``/v1/<mount>/data/<rel>``
GET / POST / DELETE with the ``{"data": {"data": {...}}}`` envelope) for
the round-trip property to be meaningful. The store is a per-test
in-memory ``dict``, so every Hypothesis example starts from a clean
state — Hypothesis cannot accidentally observe state leaked from a
previous example.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

import httpx
import nacl.utils
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vault_client import (
    HashicorpBackend,
    KEY_SIZE,
    LocalDevBackend,
    VaultClient,
    VaultPath,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A single Vault path segment matches the project pattern
# ``[a-zA-Z0-9_-]+``. Generating per-segment text and joining with ``/``
# guarantees we always produce strings that pass ``VaultPath.parse``
# without resorting to ``filter`` (which Hypothesis would warn about).
_SEGMENT_ALPHABET = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_segments = st.lists(
    st.text(alphabet=_SEGMENT_ALPHABET, min_size=1, max_size=12),
    min_size=1,
    max_size=4,
)


@st.composite
def vault_paths(draw: st.DrawFn) -> VaultPath:
    """Hypothesis strategy producing well-formed :class:`VaultPath` values."""
    segs = draw(_segments)
    return VaultPath.parse("vault:" + "/".join(segs))


# Flat ``str → str`` payloads — KV v2 stores at most a flat dict per
# secret. Keys must be non-empty and free of NUL characters; values are
# arbitrary printable text. Limiting the dict to 4 entries keeps test
# wall-time low while still exercising multi-field round-trips.
_PAYLOAD_KEY_ALPHABET = st.characters(
    min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters="\x00"
)
_payload_keys = st.text(alphabet=_PAYLOAD_KEY_ALPHABET, min_size=1, max_size=24)
_payload_values = st.text(min_size=0, max_size=64).filter(
    # Reject embedded NULs and surrogate halves so JSON serialisation
    # in the local-dev backend cannot fail.
    lambda s: "\x00" not in s and not any(0xD800 <= ord(c) <= 0xDFFF for c in s)
)
_payloads = st.dictionaries(_payload_keys, _payload_values, min_size=1, max_size=4)


# ---------------------------------------------------------------------------
# Backend factories — one per backend, each returning a fresh instance
# ---------------------------------------------------------------------------


def _make_local_dev_backend(tmp_path: Path) -> VaultClient:
    """Build a :class:`LocalDevBackend` rooted at *tmp_path*.

    Hypothesis reuses the function-scoped ``tmp_path`` fixture across
    every example in a single property invocation, but each example
    needs an *empty* store keyed by a *fresh* symmetric key (otherwise
    a residual ``vault.json`` from the prior iteration would fail to
    decrypt under the new key). We satisfy both constraints by deleting
    any leftover store file before constructing the backend.
    """
    store = tmp_path / "vault.json"
    if store.exists():
        store.unlink()
    return LocalDevBackend(
        store_path=store,
        key=nacl.utils.random(KEY_SIZE),
    )


def _make_hashicorp_backend() -> VaultClient:
    """Build a :class:`HashicorpBackend` wired to an in-process Vault stub.

    The stub speaks just enough KV-v2 to satisfy the protocol contract:

    * ``GET  /v1/<mount>/data/<rel>``: returns the stored data wrapped in
      the canonical ``{"data": {"data": {...}, "metadata": {...}}}``
      envelope. Returns 404 with an ``errors: []`` body when absent.
    * ``POST /v1/<mount>/data/<rel>``: records ``request.json()["data"]``.
    * ``DELETE /v1/<mount>/data/<rel>``: removes the stored entry; 404
      when already absent (idempotent — matches the real KV-v2 behaviour
      that ``HashicorpBackend.delete`` relies on).
    """

    store: dict[str, dict[str, str]] = {}
    addr = "https://vault.local"
    mount = "secret"
    prefix = f"/v1/{mount}/data/"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(prefix):
            return httpx.Response(404, json={"errors": ["not found"]})
        rel = path[len(prefix):]

        if request.method == "GET":
            if rel not in store:
                return httpx.Response(404, json={"errors": []})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": dict(store[rel]),
                        "metadata": {"version": 1},
                    }
                },
            )
        if request.method == "POST":
            body = json.loads(request.content.decode("utf-8") or "{}")
            store[rel] = {
                str(k): str(v) for k, v in body.get("data", {}).items()
            }
            return httpx.Response(200, json={"data": {"version": 1}})
        if request.method == "DELETE":
            store.pop(rel, None)
            return httpx.Response(204)
        return httpx.Response(405, json={"errors": ["method not allowed"]})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return HashicorpBackend(addr=addr, token="s.dev-token", mount=mount, client=client)


#: Each test parameterises over the two backend factories so a single
#: property body covers both implementations. The factory takes
#: ``tmp_path`` even when it doesn't need it so the call site stays
#: uniform.
BackendFactory = Callable[[Path], VaultClient]

_BACKEND_FACTORIES: tuple[tuple[str, BackendFactory], ...] = (
    ("local-dev", _make_local_dev_backend),
    ("hashicorp", lambda _tmp: _make_hashicorp_backend()),
)


# ---------------------------------------------------------------------------
# Property: read(write(p, v)) == v
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_name,factory",
    _BACKEND_FACTORIES,
    ids=[name for name, _ in _BACKEND_FACTORIES],
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(path=vault_paths(), payload=_payloads)
def test_round_trip_read_write(
    backend_name: str,
    factory: BackendFactory,
    tmp_path: Path,
    path: VaultPath,
    payload: Mapping[str, str],
) -> None:
    """Both backends round-trip identically.

    For any (path, payload) drawn by Hypothesis, writing the payload and
    reading it back through the same backend MUST yield the original
    payload unchanged. The backend is rebuilt from scratch on every
    Hypothesis example (Hypothesis re-invokes the test body each
    iteration, and ``factory`` produces a clean instance), so leftover
    state from a prior example cannot mask a regression.
    """
    backend = factory(tmp_path)
    try:
        backend.write(path, payload)
        actual = backend.read(path)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    assert dict(actual) == dict(payload), (
        f"{backend_name}: round-trip mismatch for path={path.raw!r}"
    )


# ---------------------------------------------------------------------------
# Property: backends agree on the post-write read value
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(path=vault_paths(), payload=_payloads)
def test_backends_agree_on_read_after_write(
    tmp_path: Path,
    path: VaultPath,
    payload: Mapping[str, str],
) -> None:
    """Equivalence across the two backends.

    Stronger than the per-backend round-trip: writing the same payload
    to *both* backends and then reading from each MUST produce
    bit-identical results. This is the property that lets callers swap
    ``VAULT_BACKEND=local-dev`` for ``VAULT_BACKEND=hashicorp`` (or vice
    versa) at deploy time without worrying about silent semantic drift.
    """
    local = _make_local_dev_backend(tmp_path)
    hashi = _make_hashicorp_backend()
    try:
        local.write(path, payload)
        hashi.write(path, payload)

        local_value = dict(local.read(path))
        hashi_value = dict(hashi.read(path))
    finally:
        close = getattr(hashi, "close", None)
        if callable(close):
            close()

    assert local_value == hashi_value == dict(payload)


# ---------------------------------------------------------------------------
# Property: read after delete raises KeyError on both backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_name,factory",
    _BACKEND_FACTORIES,
    ids=[name for name, _ in _BACKEND_FACTORIES],
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(path=vault_paths(), payload=_payloads)
def test_read_after_delete_raises_key_error(
    backend_name: str,
    factory: BackendFactory,
    tmp_path: Path,
    path: VaultPath,
    payload: Mapping[str, str],
) -> None:
    """Delete semantics align across backends.

    ``write -> delete -> read`` must raise :class:`KeyError` on both
    backends. This protects callers (e.g. the atomic-create rollback
    path) from observing a stale value that was supposed to
    have been removed.
    """
    backend = factory(tmp_path)
    try:
        backend.write(path, payload)
        backend.delete(path)
        with pytest.raises(KeyError):
            backend.read(path)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
