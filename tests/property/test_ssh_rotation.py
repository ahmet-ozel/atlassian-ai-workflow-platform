"""Property test 11b — SSH dual-slot rotation invariants.

**Validates: Requirements 6.7**

For an arbitrary sequence of ``rotate_ssh_key`` calls against the same
``runner_id``, the slot machine maintained by a :class:`VaultClient`
SHALL satisfy:

1. After the first rotation: the *active* slot holds the freshly
   issued key, and the *previous* slot is unset (``RotationResult.previous_path is None``).
2. After every subsequent rotation: the *active* slot holds the latest
   key (``RotationResult.active_path``); the *previous* slot holds the
   key that was active just before this call.
3. Rotation never loses material in flight — between any two successive
   rotations the previous slot is exactly the prior active key, so an
   in-flight SSH session that was still using key ``v_(n-1)`` can be
   re-validated until the operator clears the slot (MIMARI §13 E8 dual-slot).
4. The :class:`RotationResult` returns the canonical Vault path strings
   ``vault:ssh/runners/<id>/active`` and ``.../previous`` regardless of
   which backend produced the result.

The test runs against both pluggable backends (``LocalDevBackend`` and
``HashicorpBackend`` via :class:`httpx.MockTransport`) so the same
property guards both production and development code paths. The
Hypothesis ``stateful``-style approach is unnecessary here because the
state machine is already linear: the entire history is captured by
``(active_v_n, active_v_(n-1))`` and Hypothesis can drive the sequence
with a plain ``st.lists(keys)`` strategy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import httpx
import nacl.utils
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vault_client import (
    HashicorpBackend,
    KEY_SIZE,
    LocalDevBackend,
    SshKey,
    VaultClient,
    VaultPath,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Runner id grammar mirrors design.md §"Vault path domeni":
#: ``vault:ssh/runners/<runner_id>/active``. The id segment must satisfy
#: the project-wide ``[a-zA-Z0-9_-]+`` character class so it round-trips
#: through ``VaultPath.parse``.
_RUNNER_ID_ALPHABET = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
runner_ids = st.text(alphabet=_RUNNER_ID_ALPHABET, min_size=1, max_size=24)

#: Each rotation introduces a new key. Generating just the *fingerprint*
#: gives every rotation a unique witness without bloating the example
#: budget; the PEM bodies are derived deterministically from the
#: fingerprint so they stay distinct.
_FINGERPRINT_ALPHABET = st.sampled_from(
    "abcdefghijklmnopqrstuvwxyz0123456789"
)
fingerprints = st.text(alphabet=_FINGERPRINT_ALPHABET, min_size=4, max_size=16)


def _make_key(fp: str) -> SshKey:
    """Build a deterministic :class:`SshKey` whose fields encode *fp*.

    Each field carries a distinct prefix so a regression in the
    backend's slot bookkeeping (e.g. swapping public and private blobs)
    surfaces as an obvious mismatch in the assertion messages, not a
    silent equality on identical strings.
    """
    return SshKey(
        private_pem=f"-----BEGIN PRIVATE-{fp}-----",
        public_pem=f"ssh-ed25519 AAAA{fp}",
        fingerprint=f"SHA256:{fp}",
    )


# ---------------------------------------------------------------------------
# Backend factories — clean instance per Hypothesis example
# ---------------------------------------------------------------------------


def _make_local_dev_backend(tmp_path: Path) -> VaultClient:
    """Build a :class:`LocalDevBackend` rooted at *tmp_path*.

    The function-scoped ``tmp_path`` fixture is shared across all
    Hypothesis examples in one property invocation; the symmetric key
    is regenerated each example. Removing any leftover ``vault.json``
    before constructing the backend keeps every example operating on a
    pristine encrypted store under its own key.
    """
    store = tmp_path / "vault.json"
    if store.exists():
        store.unlink()
    return LocalDevBackend(
        store_path=store,
        key=nacl.utils.random(KEY_SIZE),
    )


def _make_hashicorp_backend() -> VaultClient:
    """Return a :class:`HashicorpBackend` backed by an in-process KV-v2 stub.

    The stub mirrors the one in :mod:`test_vault_backends` so the two
    property tests share an identical mental model of Vault's wire
    contract. SSH rotation only exercises the ``read`` / ``write`` /
    ``delete`` paths, so the same handler suffices.
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


BackendFactory = Callable[[Path], VaultClient]

_BACKEND_FACTORIES: tuple[tuple[str, BackendFactory], ...] = (
    ("local-dev", _make_local_dev_backend),
    ("hashicorp", lambda _tmp: _make_hashicorp_backend()),
)


# ---------------------------------------------------------------------------
# Property: dual-slot invariant under any rotation history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_name,factory",
    _BACKEND_FACTORIES,
    ids=[name for name, _ in _BACKEND_FACTORIES],
)
@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    runner_id=runner_ids,
    fps=st.lists(fingerprints, min_size=1, max_size=8, unique=True),
)
def test_active_previous_slot_invariant(
    backend_name: str,
    factory: BackendFactory,
    tmp_path: Path,
    runner_id: str,
    fps: list[str],
) -> None:
    """**Validates: Requirements 6.7**

    Drive a sequence of rotations and assert at every step:

    * ``RotationResult.active_path`` resolves to
      ``vault:ssh/runners/<runner_id>/active``.
    * On the **first** rotation, ``previous_path is None`` and reading
      the previous slot raises :class:`KeyError`.
    * On every **subsequent** rotation:
      - ``previous_path`` resolves to ``vault:ssh/runners/<runner_id>/previous``.
      - The active slot holds the just-issued key (matching
        ``new_key`` for this iteration).
      - The previous slot holds the key that was active immediately
        before this rotation.
    """
    backend = factory(tmp_path)
    expected_active = f"vault:ssh/runners/{runner_id}/active"
    expected_previous = f"vault:ssh/runners/{runner_id}/previous"
    prior_active_payload: dict[str, str] | None = None

    try:
        for index, fp in enumerate(fps):
            new_key = _make_key(fp)
            new_payload = {
                "private_pem": new_key.private_pem,
                "public_pem": new_key.public_pem,
                "fingerprint": new_key.fingerprint,
            }

            result = backend.rotate_ssh_key(runner_id, new_key)

            # 1. Active path is canonical and matches the slot grammar.
            assert isinstance(result.active_path, VaultPath)
            assert result.active_path.raw == expected_active

            # 2. Previous slot bookkeeping depends on whether this is
            #    the very first rotation in this test run.
            if index == 0:
                assert result.previous_path is None, (
                    f"{backend_name}: previous_path must be None on the first "
                    "rotation; got "
                    f"{result.previous_path!r}"
                )
                with pytest.raises(KeyError):
                    backend.read(VaultPath.parse(expected_previous))
            else:
                assert result.previous_path is not None
                assert result.previous_path.raw == expected_previous
                # 3. Previous slot equals the *prior* active payload —
                #    so an in-flight SSH session using key v_(n-1) can
                #    still validate after we cut over to v_n.
                previous_value = dict(backend.read(result.previous_path))
                assert previous_value == prior_active_payload, (
                    f"{backend_name}: previous slot drift at rotation #{index}"
                )

            # 4. Active slot reflects the just-rotated key.
            active_value = dict(backend.read(result.active_path))
            assert active_value == new_payload, (
                f"{backend_name}: active slot mismatch at rotation #{index}"
            )

            # 5. Slots never coincide — leaking the same payload into
            #    both would defeat the dual-slot invariant.
            if result.previous_path is not None:
                assert active_value != dict(backend.read(result.previous_path))

            prior_active_payload = new_payload
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Property: rotations are isolated per runner_id
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
@given(
    runner_a=runner_ids,
    runner_b=runner_ids,
    fp_a=fingerprints,
    fp_b=fingerprints,
)
def test_rotation_is_per_runner(
    backend_name: str,
    factory: BackendFactory,
    tmp_path: Path,
    runner_a: str,
    runner_b: str,
    fp_a: str,
    fp_b: str,
) -> None:
    """**Validates: Requirements 6.7**

    Rotating runner ``A`` MUST NOT shift slot state on runner ``B``.
    The two runners share the same Vault prefix
    (``vault:ssh/runners/...``) but live under disjoint ``runner_id``
    segments, so a regression that strips the runner segment (e.g.
    using a single global ``active`` key) would surface here.
    """
    if runner_a == runner_b:
        # Same runner: the per-runner property reduces to the dual-slot
        # property already covered above. Skip to keep the assertion
        # focused on cross-runner isolation.
        return

    backend = factory(tmp_path)
    try:
        backend.rotate_ssh_key(runner_a, _make_key(fp_a))
        # B has no key yet — reading its active slot must raise.
        with pytest.raises(KeyError):
            backend.read(
                VaultPath.parse(f"vault:ssh/runners/{runner_b}/active")
            )

        backend.rotate_ssh_key(runner_b, _make_key(fp_b))

        # A's active slot still reflects fp_a, even after B rotated.
        a_active = dict(
            backend.read(
                VaultPath.parse(f"vault:ssh/runners/{runner_a}/active")
            )
        )
        assert a_active["fingerprint"] == f"SHA256:{fp_a}"

        # B's active slot reflects fp_b.
        b_active = dict(
            backend.read(
                VaultPath.parse(f"vault:ssh/runners/{runner_b}/active")
            )
        )
        assert b_active["fingerprint"] == f"SHA256:{fp_b}"
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
