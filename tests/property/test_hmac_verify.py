"""invariant for HMAC-SHA256 sign-verify round-trip and tamper rejection.



invariant: HMAC-SHA256 sign-verify round-trip and tamper-rejection.

Invariants tested:
 2a. For any (secret, payload) pair, compute(payload, secret) followed by
 verify(payload, signature, secret) always returns True (round-trip).
 2b. Tampering with the payload invalidates the signature (tampered payload).
 2c. Tampering with the signature invalidates verification (tampered sig).
 2d. Using a different secret invalidates verification (tampered secret).
 2e. The verify function uses hmac.compare_digest for constant-time
 comparison (AST inspection).

invariant (appended below the invariant block): Webhook handler -
per-dept HMAC, rotation overlap and dept_id resolution. The HMAC half
of invariant lives here (per-dept secret isolation, 1h rotation
overlap, missing-secret rejection, unsupported-provider error). The
dept_id-resolution half lives in
``test_webhook_predicates.py::TestWebhookDeptUnresolved`` so each
property file stays focused on a single collaborator.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

# Ensure the automation-service src is importable for invariant.
_AUTOMATION_SRC = Path(__file__).resolve().parents[1].parent / "services" / "automation-service" / "src"
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from decision.hmac_verify import compute, verify


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Secrets: 1-256 bytes (non-empty, realistic key sizes)
_secrets = st.binary(min_size=1, max_size=256)

# Payloads: 0-65536 bytes (empty payloads are valid webhook edge case)
_payloads = st.binary(min_size=0, max_size=65536)


# ---------------------------------------------------------------------------
# invariant: Round-trip - compute then verify always succeeds
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(secret=_secrets, payload=_payloads)
def test_hmac_sign_verify_round_trip(secret: bytes, payload: bytes) -> None:
    """invariant - compute(payload, secret)  verify(payload, sig, secret) is True.

 For every valid (secret, payload) pair, signing and then verifying
 with the same inputs must always succeed.
 """
    signature = compute(payload, secret)
    assert verify(payload, signature, secret) is True


# ---------------------------------------------------------------------------
# invariant: Tampered payload  verification fails
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(secret=_secrets, payload=_payloads, tampered_payload=_payloads)
def test_hmac_tampered_payload_rejected(
    secret: bytes, payload: bytes, tampered_payload: bytes
) -> None:
    """invariant - tampering with the payload invalidates the signature.

 If the payload changes (even by one byte), the original signature
 must no longer verify.
 """
    assume(payload != tampered_payload)

    signature = compute(payload, secret)
    assert verify(tampered_payload, signature, secret) is False


# ---------------------------------------------------------------------------
# invariant: Tampered signature  verification fails
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    secret=_secrets,
    payload=_payloads,
    tamper_index=st.integers(min_value=0, max_value=63),
)
def test_hmac_tampered_signature_rejected(
    secret: bytes, payload: bytes, tamper_index: int
) -> None:
    """invariant - flipping any hex character in the signature invalidates it.

 The signature format is 'sha256=' + 64 hex chars. We flip one hex
 character at a random position to simulate signature tampering.
 """
    signature = compute(payload, secret)
    # Extract the hex portion after 'sha256='
    prefix = "sha256="
    hex_part = signature[len(prefix):]

    # Flip one hex character
    original_char = hex_part[tamper_index]
    # Pick a different hex character
    replacement = "0" if original_char != "0" else "1"
    tampered_hex = hex_part[:tamper_index] + replacement + hex_part[tamper_index + 1:]
    tampered_signature = prefix + tampered_hex

    assert verify(payload, tampered_signature, secret) is False


# ---------------------------------------------------------------------------
# invariant: Wrong secret  verification fails
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(secret=_secrets, payload=_payloads, wrong_secret=_secrets)
def test_hmac_wrong_secret_rejected(
    secret: bytes, payload: bytes, wrong_secret: bytes
) -> None:
    """invariant - using a different secret invalidates verification.

 If the verifier uses a different secret than the signer, the
 verification must fail.
 """
    assume(secret != wrong_secret)

    signature = compute(payload, secret)
    assert verify(payload, signature, wrong_secret) is False


# ---------------------------------------------------------------------------
# invariant: Constant-time comparison via hmac.compare_digest (AST scan)
# ---------------------------------------------------------------------------


def test_verify_uses_hmac_compare_digest() -> None:
    """invariant - verify uses hmac.compare_digest for constant-time comparison.

 This is a structural assertion: the source code of verify must
 contain a call to hmac.compare_digest (or compare_digest) to prevent
 timing side-channel attacks. We parse the AST to confirm this.
 """
    source = inspect.getsource(verify)
    tree = ast.parse(source)

    compare_digest_calls: list[ast.Call] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Match: hmac.compare_digest(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "compare_digest"
                and isinstance(func.value, ast.Name)
                and func.value.id == "hmac"
            ):
                compare_digest_calls.append(node)
            # Match: compare_digest(...) (if imported directly)
            elif isinstance(func, ast.Name) and func.id == "compare_digest":
                compare_digest_calls.append(node)

    assert len(compare_digest_calls) >= 1, (
        "verify must use hmac.compare_digest for constant-time comparison "
        "to prevent timing side-channel attacks. No such call was found in "
        "the function's AST."
    )


# ---------------------------------------------------------------------------
# invariant - Webhook handler: per-dept HMAC + rotation overlap
# ---------------------------------------------------------------------------
#
# **invariant: Webhook handler - per-dept HMAC, rotation overlap ve
# dept_id çözümlemesi**
#
#
# Companion to ``test_webhook_predicates.py``'s ``TestWebhookDeptUnresolved``
# class which covers the *dept_id resolution  HTTP 400* leg of the
# property. Here we exercise the *per-department HMAC verification*
# leg through:func:`vault_client.verify_webhook_hmac`:
#
# - **Per-dept secret**: a body signed with department A's secret
# never validates against department B's secret, even when both
# departments live in the same Vault store. ( - "tek bir global
# webhook secret kullanmaz".)
#
# - **Rotation overlap**: after rotating
# ``vault:webhooks/<provider>/<dept_id>``, both the *previous* and
# the *new* secret SHALL verify successfully for one hour; once the
# overlap window expires, only the new secret is accepted, and the
# old secret SHALL be rejected. ( - "1 saatlik bir overlap
# penceresi".)
#
# - **Tamper rejection under rotation**: tampering with the body
# invalidates the signature regardless of which secret in the
# overlap window was used. (Composition with.x.)
#
# - **Provider isolation**: a body signed with the Jira secret for a
# given dept does not validate against the Bitbucket / Confluence
# secret for the same dept (each provider has its own slot).
#
# These properties drive ``verify_webhook_hmac`` against a
#:class:`vault_client.LocalDevBackend` so the tests stay
# self-contained - no Hashicorp HTTP round-trip and no shared global
# state. The local-dev backend's slot encoding (``active`` /
# ``previous`` with ``overlap_until``) is the same shape the
# Hashicorp backend produces under KV-v2 versioning, so the property
# is backend-agnostic at the assertion layer.
# ---------------------------------------------------------------------------

import hashlib
import hmac
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nacl.utils
import pytest

from vault_client import (
    KEY_SIZE,
    LocalDevBackend,
    verify_webhook_hmac,
)


_PROVIDERS: tuple[str, ...] = ("jira", "bitbucket", "confluence")


def _make_backend(tmp_path: Path) -> LocalDevBackend:
    """Build a fresh, isolated:class:`LocalDevBackend` per test draw."""
    return LocalDevBackend(
        store_path=tmp_path / "vault.json",
        key=nacl.utils.random(KEY_SIZE),
    )


def _sign_with_secret(secret: str, body: bytes) -> str:
    """Produce an Atlassian-style ``X-Hub-Signature: sha256=<hex>`` value."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# Hypothesis strategies ------------------------------------------------------

#: Department IDs follow the same kebab-case shape as
#: ``departments.schema.json`` ``id`` (``^[a-z][a-z0-9-]{1,30}$``); the
#: handler does no further validation, but using a realistic shape
#: keeps the Vault paths well-formed under:func:`VaultPath.parse`.
_dept_ids = st.from_regex(r"^[a-z][a-z0-9-]{1,30}$", fullmatch=True)

#: Webhook secrets - non-empty printable ASCII so the HMAC layer is
#: not exercised against malformed UTF-8 inputs (which Atlassian
#: would never produce anyway).
_secrets_str = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=8,
    max_size=128,
)

#: Distinct-secret pairs. Hypothesis ``filter(...)`` is cheap here because
#: collisions are vanishingly rare in the chosen alphabet.
_distinct_secret_pairs = st.tuples(_secrets_str, _secrets_str).filter(
    lambda pair: pair[0] != pair[1]
)

#: Webhook bodies - empty body is a valid edge case (Atlassian sends
#: ``{}``-payloads on some lifecycle hooks).
_bodies = st.binary(min_size=0, max_size=4096)

_providers = st.sampled_from(_PROVIDERS)


# invariant: per-dept secret isolation -----------------------------------


class TestPerDeptHmacIsolation:
    """A webhook signed for dept A SHALL NOT verify against dept B.


 """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        dept_pair=st.lists(_dept_ids, min_size=2, max_size=2, unique=True),
        secret_pair=_distinct_secret_pairs,
        body=_bodies,
        provider=_providers,
    )
    def test_signature_for_dept_a_rejected_for_dept_b(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        dept_pair: list[str],
        secret_pair: tuple[str, str],
        body: bytes,
        provider: str,
    ) -> None:
        """Cross-department signature MUST NOT validate.



 Provisions both departments under the same provider with
 different secrets; signs *body* with department A's secret;
 verifies against department B - must return ``False``.
 """
        tmp_path = tmp_path_factory.mktemp("per_dept_hmac_isolation")
        backend = _make_backend(tmp_path)
        dept_a, dept_b = dept_pair
        secret_a, secret_b = secret_pair

        backend.rotate_webhook_secret(provider, dept_a, secret_a)
        backend.rotate_webhook_secret(provider, dept_b, secret_b)

        sig_a = _sign_with_secret(secret_a, body)
        now = datetime.now(timezone.utc)

        # Round-trip: dept A's signature validates for dept A.
        assert verify_webhook_hmac(
            backend, provider, dept_a, body, sig_a, now
        ) is True
        # Cross-dept: dept A's signature MUST NOT validate for dept B.
        assert verify_webhook_hmac(
            backend, provider, dept_b, body, sig_a, now
        ) is False

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        dept_id=_dept_ids,
        secret=_secrets_str,
        body=_bodies,
    )
    def test_provider_slots_are_isolated(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        dept_id: str,
        secret: str,
        body: bytes,
    ) -> None:
        """A dept's Jira secret MUST NOT validate against its Bitbucket slot.



 Provisions a single dept under provider ``"jira"`` only; a
 signature computed with that secret MUST NOT validate when
 looked up against ``"bitbucket"`` or ``"confluence"`` for the
 same dept (those slots are absent  ``False``).
 """
        tmp_path = tmp_path_factory.mktemp("provider_slot_isolation")
        backend = _make_backend(tmp_path)
        backend.rotate_webhook_secret("jira", dept_id, secret)

        sig = _sign_with_secret(secret, body)
        now = datetime.now(timezone.utc)

        assert verify_webhook_hmac(
            backend, "jira", dept_id, body, sig, now
        ) is True
        for other in ("bitbucket", "confluence"):
            assert verify_webhook_hmac(
                backend, other, dept_id, body, sig, now
            ) is False, f"signature validated under unrelated provider {other!r}"


# invariant: rotation overlap window -------------------------------------


class TestWebhookSecretRotationOverlap:
    """Rotation overlap window - both secrets accepted for 1 hour, then only new.


 """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        provider=_providers,
        dept_id=_dept_ids,
        secret_pair=_distinct_secret_pairs,
        body=_bodies,
        offset_minutes=st.integers(min_value=0, max_value=59),
    )
    def test_within_overlap_both_secrets_accepted(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        provider: str,
        dept_id: str,
        secret_pair: tuple[str, str],
        body: bytes,
        offset_minutes: int,
    ) -> None:
        """Within ``[rotated_at, rotated_at + 1h)``, both secrets verify.



 After rotating from ``old  new``, signatures produced by
 either secret must validate at any instant strictly inside
 the overlap window.
 """
        tmp_path = tmp_path_factory.mktemp("rotation_overlap_within")
        backend = _make_backend(tmp_path)
        old_secret, new_secret = secret_pair

        backend.rotate_webhook_secret(provider, dept_id, old_secret)
        result = backend.rotate_webhook_secret(provider, dept_id, new_secret)
        assert result.overlap_until is not None
        assert result.previous_path is not None

        # Sample a time within the overlap window. ``offset_minutes ∈
        # [0, 59]`` is strictly less than 60 minutes from the rotation
        # instant, so adding it to ``rotated_at`` always falls inside
        # ``[rotated_at, rotated_at + 1h)``.
        sample_now = result.rotated_at + timedelta(minutes=offset_minutes)
        # Sanity: ``sample_now`` is strictly inside the window.
        assert sample_now < result.overlap_until

        sig_new = _sign_with_secret(new_secret, body)
        sig_old = _sign_with_secret(old_secret, body)

        assert verify_webhook_hmac(
            backend, provider, dept_id, body, sig_new, sample_now
        ) is True, "new secret SHALL verify within overlap window"
        assert verify_webhook_hmac(
            backend, provider, dept_id, body, sig_old, sample_now
        ) is True, "old secret SHALL verify within overlap window"

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        provider=_providers,
        dept_id=_dept_ids,
        secret_pair=_distinct_secret_pairs,
        body=_bodies,
        post_offset_seconds=st.integers(min_value=1, max_value=60 * 60 * 24),
    )
    def test_after_overlap_only_new_secret_accepted(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        provider: str,
        dept_id: str,
        secret_pair: tuple[str, str],
        body: bytes,
        post_offset_seconds: int,
    ) -> None:
        """Past ``rotated_at + 1h``, only the new secret verifies.



 At any instant strictly after ``overlap_until``, the old
 secret SHALL be rejected and the new secret SHALL still be
 accepted.
 """
        tmp_path = tmp_path_factory.mktemp("rotation_overlap_after")
        backend = _make_backend(tmp_path)
        old_secret, new_secret = secret_pair

        backend.rotate_webhook_secret(provider, dept_id, old_secret)
        result = backend.rotate_webhook_secret(provider, dept_id, new_secret)
        assert result.overlap_until is not None

        sample_now = result.overlap_until + timedelta(seconds=post_offset_seconds)
        assert sample_now > result.overlap_until

        sig_new = _sign_with_secret(new_secret, body)
        sig_old = _sign_with_secret(old_secret, body)

        assert verify_webhook_hmac(
            backend, provider, dept_id, body, sig_new, sample_now
        ) is True, "new secret SHALL still verify after overlap window"
        assert verify_webhook_hmac(
            backend, provider, dept_id, body, sig_old, sample_now
        ) is False, "old secret SHALL be rejected after overlap window"

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        provider=_providers,
        dept_id=_dept_ids,
        secret_pair=_distinct_secret_pairs,
        wrong_secret=_secrets_str,
        body=_bodies,
        offset_minutes=st.integers(min_value=0, max_value=59),
    )
    def test_overlap_does_not_admit_unrelated_secret(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        provider: str,
        dept_id: str,
        secret_pair: tuple[str, str],
        wrong_secret: str,
        body: bytes,
        offset_minutes: int,
    ) -> None:
        """A signature from a *third* secret MUST NOT slip through the overlap.



 Within the overlap window we accept exactly two secrets
 (``previous`` and ``active``); any signature produced with a
 secret outside that pair must be rejected.
 """
        old_secret, new_secret = secret_pair
        assume(wrong_secret not in (old_secret, new_secret))

        tmp_path = tmp_path_factory.mktemp("rotation_overlap_third_secret")
        backend = _make_backend(tmp_path)
        backend.rotate_webhook_secret(provider, dept_id, old_secret)
        result = backend.rotate_webhook_secret(provider, dept_id, new_secret)
        assert result.overlap_until is not None

        sample_now = result.rotated_at + timedelta(minutes=offset_minutes)
        wrong_sig = _sign_with_secret(wrong_secret, body)

        assert verify_webhook_hmac(
            backend, provider, dept_id, body, wrong_sig, sample_now
        ) is False, (
            "wrong secret MUST NOT validate even inside the overlap window"
        )

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        provider=_providers,
        dept_id=_dept_ids,
        secret_pair=_distinct_secret_pairs,
        body=_bodies,
        tampered_body=_bodies,
        offset_minutes=st.integers(min_value=0, max_value=59),
        use_old_secret=st.booleans(),
    )
    def test_overlap_does_not_admit_tampered_body(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        provider: str,
        dept_id: str,
        secret_pair: tuple[str, str],
        body: bytes,
        tampered_body: bytes,
        offset_minutes: int,
        use_old_secret: bool,
    ) -> None:
        """Tampering with the body invalidates either secret in the overlap.



 Even when the legit secret is in scope (old *or* new during
 the overlap), a signature paired with a different body must
 fail to verify.
 """
        assume(body != tampered_body)
        old_secret, new_secret = secret_pair

        tmp_path = tmp_path_factory.mktemp("rotation_overlap_tamper")
        backend = _make_backend(tmp_path)
        backend.rotate_webhook_secret(provider, dept_id, old_secret)
        result = backend.rotate_webhook_secret(provider, dept_id, new_secret)
        assert result.overlap_until is not None

        sample_now = result.rotated_at + timedelta(minutes=offset_minutes)
        signing_secret = old_secret if use_old_secret else new_secret
        sig = _sign_with_secret(signing_secret, body)

        assert verify_webhook_hmac(
            backend,
            provider,
            dept_id,
            tampered_body,
            sig,
            sample_now,
        ) is False, "tampered body MUST NOT validate inside the overlap"


# invariant: missing-secret + provider validation ------------------------


class TestVerifyWebhookHmacMissingSecret:
    """When a per-dept secret is absent, every signature MUST be rejected.


 ``webhook_dept_unresolved`` audit when dept_id can't be resolved;
 the predicate-level analogue is "no secret stored  False").
 """

    @settings(
        max_examples=80,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        provider=_providers,
        dept_id=_dept_ids,
        secret=_secrets_str,
        body=_bodies,
    )
    def test_missing_secret_returns_false_regardless_of_signature(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        provider: str,
        dept_id: str,
        secret: str,
        body: bytes,
    ) -> None:
        """No secret stored at the path  ``verify_webhook_hmac`` returns False.


 for dept_id"  reject path).
 """
        tmp_path = tmp_path_factory.mktemp("missing_secret_rejects")
        backend = _make_backend(tmp_path)
        # NOTE: backend is empty - no rotate_webhook_secret called.

        sig = _sign_with_secret(secret, body)
        assert verify_webhook_hmac(
            backend,
            provider,
            dept_id,
            body,
            sig,
            datetime.now(timezone.utc),
        ) is False

    def test_unsupported_provider_raises_value_error(self) -> None:
        """Unknown providers MUST surface a ``ValueError`` to the handler.


 HTTP 400 rather than letting it become a 500 - see
 ``automation_service.webhooks_handlers._process_jira_webhook``
 ``except ValueError`` branch).
 """
        # Build a backend without writing anything; the function raises
        # before any Vault read, so the store contents don't matter.
        with tempfile.TemporaryDirectory() as td:
            backend = LocalDevBackend(
                store_path=Path(td) / "vault.json",
                key=nacl.utils.random(KEY_SIZE),
            )
            for bad in ("github", "gitlab", "", "JIRA", "Jira"):
                with pytest.raises(ValueError, match="unsupported webhook provider"):
                    verify_webhook_hmac(
                        backend,
                        bad,  # type: ignore[arg-type]
                        "any-dept",
                        b"x",
                        "sha256=00",
                        datetime.now(timezone.utc),
                    )
