"""Unit tests for :mod:`vault_client`.

Scope
-----

* :class:`VaultPath` - accepts the canonical pattern, rejects
  malformed strings (plain-text tokens, URLs, empty / non-str input).
* :func:`make_client` - selects the right backend from
  ``VAULT_BACKEND`` and rejects unknown / missing values.
* :class:`LocalDevBackend` - round-trip ``read(write(p, v)) == v``
  through the libsodium-encrypted file backend, plus delete idempotency
  and SSH dual-slot rotation invariants.

The Hashicorp backend's HTTP wire shape is exercised via
:class:`httpx.MockTransport` so the test suite stays self-contained.
The cross-backend equivalence property test
Backend parity tests live elsewhere; these tests cover the
per-backend contract surface.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import nacl.utils
import pytest

from vault_client import (
    HashicorpBackend,
    KEY_SIZE,
    LocalDevBackend,
    RotationResult,
    SshKey,
    VaultClient,
    VaultPath,
    make_client,
    verify_webhook_hmac,
)


# ---------------------------------------------------------------------------
# VaultPath
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "vault:atlassian/payments/jira",
        "vault:webhooks/jira/payments",
        "vault:ssh/runners/runner-01/active",
        "vault:infrastructure/openai/api_key",
        "vault:a",
        "vault:A_B-c/D",
    ],
)
def test_vault_path_parse_accepts_valid(raw: str) -> None:
    p = VaultPath.parse(raw)
    assert p.raw == raw
    assert p.relative == raw[len("vault:"):]


@pytest.mark.parametrize(
    "bad",
    [
        "",                               # empty
        "atlassian/payments/jira",        # missing scheme
        "vault:",                          # empty path
        "vault:bad path",                 # whitespace
        "vault:bad:colon",                # colon in path
        "vault:bad.dot",                  # dot in path
        "https://vault.local/secret/x",   # URL
        "Bearer abc.def.ghi",             # token-shaped
        "vault:a\n",                       # trailing newline
        "vault:a/" + ("x" * 256) + "?v=1",  # query string
    ],
)
def test_vault_path_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        VaultPath.parse(bad)


def test_vault_path_parse_rejects_non_str() -> None:
    with pytest.raises(ValueError):
        VaultPath.parse(123)  # type: ignore[arg-type]


def test_vault_path_segments_filters_empties() -> None:
    p = VaultPath.parse("vault:atlassian/payments/jira")
    assert p.segments == ("atlassian", "payments", "jira")


def test_vault_path_is_frozen() -> None:
    p = VaultPath.parse("vault:atlassian/x/y")
    with pytest.raises((AttributeError, TypeError)):
        p.raw = "vault:other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# make_client factory
# ---------------------------------------------------------------------------


def test_make_client_rejects_missing_backend() -> None:
    with pytest.raises(ValueError, match="VAULT_BACKEND"):
        make_client({})


def test_make_client_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown VAULT_BACKEND"):
        make_client({"VAULT_BACKEND": "memory"})


def test_make_client_hashicorp_requires_addr_and_token() -> None:
    with pytest.raises(ValueError, match="VAULT_ADDR"):
        make_client({"VAULT_BACKEND": "hashicorp"})
    with pytest.raises(ValueError, match="VAULT_TOKEN"):
        make_client({
            "VAULT_BACKEND": "hashicorp",
            "VAULT_ADDR": "https://vault.local",
        })


def test_make_client_hashicorp_returns_hashicorp_backend() -> None:
    client = make_client(
        {
            "VAULT_BACKEND": "hashicorp",
            "VAULT_ADDR": "https://vault.local",
            "VAULT_TOKEN": "s.dev-token",
        }
    )
    assert isinstance(client, HashicorpBackend)
    assert client.backend == "hashicorp"
    # Conforms to the runtime-checkable Protocol.
    assert isinstance(client, VaultClient)
    client.close()


def test_make_client_local_dev_returns_local_dev_backend(
    tmp_path: Path,
) -> None:
    key_hex = nacl.utils.random(KEY_SIZE).hex()
    client = make_client(
        {
            "VAULT_BACKEND": "local-dev",
            "VAULT_LOCAL_KEY": key_hex,
            "VAULT_LOCAL_STORE": str(tmp_path / "vault.json"),
        }
    )
    assert isinstance(client, LocalDevBackend)
    assert client.backend == "local-dev"
    assert isinstance(client, VaultClient)


def test_make_client_local_dev_rejects_weak_key() -> None:
    with pytest.raises(ValueError, match="weak placeholder"):
        make_client({"VAULT_BACKEND": "local-dev", "VAULT_LOCAL_KEY": "changeme"})
    with pytest.raises(ValueError, match="weak placeholder"):
        make_client({"VAULT_BACKEND": "local-dev"})  # missing entirely


def test_make_client_local_dev_rejects_short_key() -> None:
    # Hex-decodes to 16 bytes - half the required key size.
    with pytest.raises(ValueError, match="32 bytes"):
        make_client(
            {
                "VAULT_BACKEND": "local-dev",
                "VAULT_LOCAL_KEY": "00112233445566778899aabbccddeeff",
            }
        )


# ---------------------------------------------------------------------------
# LocalDevBackend round-trip
# ---------------------------------------------------------------------------


def _local_backend(tmp_path: Path) -> LocalDevBackend:
    key = nacl.utils.random(KEY_SIZE)
    return LocalDevBackend(store_path=tmp_path / "vault.json", key=key)


def test_local_dev_round_trip(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    path = VaultPath.parse("vault:atlassian/payments/jira")
    payload = {"email": "bot@example.com", "api_token": "supersecret"}

    backend.write(path, payload)
    assert dict(backend.read(path)) == payload


def test_local_dev_overwrite(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    path = VaultPath.parse("vault:atlassian/payments/jira")
    backend.write(path, {"v": "1"})
    backend.write(path, {"v": "2"})
    assert dict(backend.read(path)) == {"v": "2"}


def test_local_dev_read_missing_raises_keyerror(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    with pytest.raises(KeyError):
        backend.read(VaultPath.parse("vault:does/not/exist"))


def test_local_dev_delete_is_idempotent(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    path = VaultPath.parse("vault:atlassian/x/y")
    backend.write(path, {"k": "v"})
    backend.delete(path)
    backend.delete(path)  # second delete must not raise
    with pytest.raises(KeyError):
        backend.read(path)


def test_local_dev_rejects_nonstring_payload(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    path = VaultPath.parse("vault:atlassian/x/y")
    with pytest.raises(TypeError):
        backend.write(path, {"k": 123})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        backend.write(path, "not a mapping")  # type: ignore[arg-type]


def test_local_dev_disk_does_not_contain_plaintext(tmp_path: Path) -> None:
    """The encrypted file must not contain plain-text values."""
    store = tmp_path / "vault.json"
    backend = LocalDevBackend(store_path=store, key=nacl.utils.random(KEY_SIZE))
    secret_marker = "PLAINTEXT_TOKEN_MUST_NOT_LEAK_42"
    backend.write(
        VaultPath.parse("vault:atlassian/x/jira"),
        {"api_token": secret_marker},
    )
    raw = store.read_bytes()
    assert secret_marker.encode("utf-8") not in raw
    # The on-disk file is a JSON envelope wrapping a base64-encoded
    # ciphertext blob - sanity-check the structure.
    envelope = json.loads(raw.decode("utf-8"))
    assert envelope["version"] == 1
    base64.b64decode(envelope["ciphertext"])  # raises if not valid base64


def test_local_dev_corrupted_file_surfaces_runtime_error(
    tmp_path: Path,
) -> None:
    store = tmp_path / "vault.json"
    backend = LocalDevBackend(store_path=store, key=nacl.utils.random(KEY_SIZE))
    backend.write(VaultPath.parse("vault:a/b"), {"k": "v"})

    # Re-open with a different key - decryption MUST fail loudly.
    other = LocalDevBackend(store_path=store, key=nacl.utils.random(KEY_SIZE))
    with pytest.raises(RuntimeError, match="unreadable"):
        other.read(VaultPath.parse("vault:a/b"))


def test_local_dev_ssh_rotation_invariants(tmp_path: Path) -> None:
    """``rotate_ssh_key`` writes new key to active and stashes prior in previous."""
    backend = _local_backend(tmp_path)
    runner_id = "runner-01"

    key_v1 = SshKey(private_pem="PEM_V1", public_pem="PUB_V1", fingerprint="fp1")
    res1 = backend.rotate_ssh_key(runner_id, key_v1)
    assert isinstance(res1, RotationResult)
    assert res1.active_path.raw == f"vault:ssh/runners/{runner_id}/active"
    assert res1.previous_path is None  # no prior key existed
    assert dict(backend.read(res1.active_path))["private_pem"] == "PEM_V1"

    key_v2 = SshKey(private_pem="PEM_V2", public_pem="PUB_V2", fingerprint="fp2")
    res2 = backend.rotate_ssh_key(runner_id, key_v2)
    assert res2.previous_path is not None
    assert (
        res2.previous_path.raw == f"vault:ssh/runners/{runner_id}/previous"
    )
    assert dict(backend.read(res2.active_path))["private_pem"] == "PEM_V2"
    assert dict(backend.read(res2.previous_path))["private_pem"] == "PEM_V1"


def test_local_dev_webhook_rotation_records_overlap(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    res1 = backend.rotate_webhook_secret("jira", "payments", "secret-v1")
    assert res1.overlap_until is None  # first write, no prior secret
    res2 = backend.rotate_webhook_secret("jira", "payments", "secret-v2")
    assert res2.overlap_until is not None
    assert res2.previous_path is not None
    previous = dict(backend.read(res2.previous_path))
    assert previous["secret"] == "secret-v1"
    assert "overlap_until" in previous


# ---------------------------------------------------------------------------
# HashicorpBackend wire shape (KV v2)
# ---------------------------------------------------------------------------


def _hashi_with_mock(handler) -> HashicorpBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return HashicorpBackend(
        addr="https://vault.local",
        token="s.dev-token",
        client=client,
    )


def test_hashicorp_read_unwraps_kv_v2_envelope() -> None:
    expected_url = "https://vault.local/v1/secret/data/atlassian/payments/jira"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == expected_url
        assert request.headers["X-Vault-Token"] == "s.dev-token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {"email": "bot@example.com", "api_token": "tk"},
                    "metadata": {"version": 1},
                }
            },
        )

    backend = _hashi_with_mock(handler)
    out = backend.read(VaultPath.parse("vault:atlassian/payments/jira"))
    assert dict(out) == {"email": "bot@example.com", "api_token": "tk"}


def test_hashicorp_write_posts_data_envelope() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": {"version": 1}})

    backend = _hashi_with_mock(handler)
    backend.write(
        VaultPath.parse("vault:atlassian/payments/jira"),
        {"api_token": "tk"},
    )
    assert captured["method"] == "POST"
    assert (
        captured["url"]
        == "https://vault.local/v1/secret/data/atlassian/payments/jira"
    )
    assert captured["body"] == {"data": {"api_token": "tk"}}


def test_hashicorp_read_missing_raises_keyerror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": []})

    backend = _hashi_with_mock(handler)
    with pytest.raises(KeyError):
        backend.read(VaultPath.parse("vault:does/not/exist"))


def test_hashicorp_delete_is_idempotent_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    backend = _hashi_with_mock(handler)
    backend.delete(VaultPath.parse("vault:already/gone"))  # MUST NOT raise


# ---------------------------------------------------------------------------
# clear_previous_ssh_slot - post-validation cleanup
# ---------------------------------------------------------------------------


def test_clear_previous_ssh_slot_removes_previous(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    runner_id = "runner-01"

    # Two rotations populate active + previous.
    backend.rotate_ssh_key(
        runner_id, SshKey("PEM_V1", "PUB_V1", "fp1")
    )
    backend.rotate_ssh_key(
        runner_id, SshKey("PEM_V2", "PUB_V2", "fp2")
    )
    previous = VaultPath.parse(f"vault:ssh/runners/{runner_id}/previous")
    active = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
    assert dict(backend.read(previous))["private_pem"] == "PEM_V1"

    backend.clear_previous_ssh_slot(runner_id)

    with pytest.raises(KeyError):
        backend.read(previous)
    # Active slot is untouched.
    assert dict(backend.read(active))["private_pem"] == "PEM_V2"


def test_clear_previous_ssh_slot_is_idempotent(tmp_path: Path) -> None:
    backend = _local_backend(tmp_path)
    # No rotation has happened yet - clearing must not raise.
    backend.clear_previous_ssh_slot("never-rotated")
    backend.clear_previous_ssh_slot("never-rotated")  # second call also fine


def test_hashicorp_clear_previous_ssh_slot_calls_delete() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    backend = _hashi_with_mock(handler)
    backend.clear_previous_ssh_slot("runner-01")

    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/data/ssh/runners/runner-01/previous")


# ---------------------------------------------------------------------------
# verify_webhook_hmac - 1h overlap window
# ---------------------------------------------------------------------------


def _sign(secret: str, body: bytes) -> str:
    """Helper: produce an ``X-Hub-Signature`` value for *body*."""
    import hashlib as _h
    import hmac as _hm
    digest = _hm.new(secret.encode("utf-8"), body, _h.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_webhook_hmac_active_secret_only(tmp_path: Path) -> None:
    """Without rotation, only the active secret is consulted."""
    from datetime import datetime, timezone as _tz

    backend = _local_backend(tmp_path)
    backend.rotate_webhook_secret("jira", "payments", "current-secret")
    body = b'{"event":"jira:issue_created"}'
    now = datetime.now(_tz.utc)

    # Correct signature  True.
    assert verify_webhook_hmac(
        backend,
        provider="jira",
        dept_id="payments",
        body=body,
        signature=_sign("current-secret", body),
        now=now,
    ) is True

    # Wrong secret  False.
    assert verify_webhook_hmac(
        backend,
        provider="jira",
        dept_id="payments",
        body=body,
        signature=_sign("nope", body),
        now=now,
    ) is False


def test_verify_webhook_hmac_within_overlap_accepts_both(tmp_path: Path) -> None:
    """Within 1h of rotation, both old and new secrets are accepted."""
    from datetime import datetime, timedelta, timezone as _tz

    backend = _local_backend(tmp_path)
    backend.rotate_webhook_secret("jira", "payments", "old-secret")
    res = backend.rotate_webhook_secret("jira", "payments", "new-secret")
    assert res.overlap_until is not None

    body = b"payload"
    halfway = res.overlap_until - timedelta(minutes=30)

    assert verify_webhook_hmac(
        backend, "jira", "payments", body, _sign("new-secret", body), halfway
    ) is True
    assert verify_webhook_hmac(
        backend, "jira", "payments", body, _sign("old-secret", body), halfway
    ) is True


def test_verify_webhook_hmac_after_overlap_rejects_old(tmp_path: Path) -> None:
    """Past the overlap deadline, only the new secret is accepted."""
    from datetime import timedelta

    backend = _local_backend(tmp_path)
    backend.rotate_webhook_secret("jira", "payments", "old-secret")
    res = backend.rotate_webhook_secret("jira", "payments", "new-secret")
    assert res.overlap_until is not None

    body = b"payload"
    after = res.overlap_until + timedelta(minutes=1)

    assert verify_webhook_hmac(
        backend, "jira", "payments", body, _sign("new-secret", body), after
    ) is True
    assert verify_webhook_hmac(
        backend, "jira", "payments", body, _sign("old-secret", body), after
    ) is False


def test_verify_webhook_hmac_no_secret_returns_false(tmp_path: Path) -> None:
    from datetime import datetime, timezone as _tz

    backend = _local_backend(tmp_path)
    body = b"payload"
    assert verify_webhook_hmac(
        backend,
        "jira",
        "no-such-dept",
        body,
        _sign("anything", body),
        datetime.now(_tz.utc),
    ) is False


def test_verify_webhook_hmac_malformed_header_returns_false(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone as _tz

    backend = _local_backend(tmp_path)
    backend.rotate_webhook_secret("jira", "payments", "current-secret")
    body = b"payload"
    now = datetime.now(_tz.utc)

    for bad in ("", "sha256=", "md5=deadbeef", "no-prefix-just-hex", "sha256"):
        assert verify_webhook_hmac(
            backend, "jira", "payments", body, bad, now
        ) is False


def test_verify_webhook_hmac_rejects_unknown_provider(tmp_path: Path) -> None:
    from datetime import datetime, timezone as _tz

    backend = _local_backend(tmp_path)
    with pytest.raises(ValueError, match="unsupported webhook provider"):
        verify_webhook_hmac(
            backend,
            "github",  # not an Atlassian provider
            "payments",
            b"x",
            "sha256=00",
            datetime.now(_tz.utc),
        )


def test_verify_webhook_hmac_naive_now_is_treated_as_utc(tmp_path: Path) -> None:
    """Naive ``now`` values are promoted to UTC so comparisons stay total."""
    from datetime import datetime, timedelta

    backend = _local_backend(tmp_path)
    backend.rotate_webhook_secret("jira", "payments", "old-secret")
    res = backend.rotate_webhook_secret("jira", "payments", "new-secret")
    assert res.overlap_until is not None

    body = b"x"
    naive_halfway = (res.overlap_until - timedelta(minutes=30)).replace(tzinfo=None)
    assert verify_webhook_hmac(
        backend,
        "jira",
        "payments",
        body,
        _sign("old-secret", body),
        naive_halfway,
    ) is True
