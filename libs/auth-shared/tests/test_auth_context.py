"""Unit tests for :class:`auth_shared.AuthContext` claim extraction
and :meth:`OIDCConfig.from_env`.

Covers the three explicit environment and claim-extraction behaviors:

1. ``OIDCConfig.from_env`` honours the ``AUTH_PROVIDER`` /
   ``OIDC_ISSUER_URL`` / ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET``
   contract.
2. ``AUTH_PROVIDER=local`` selects the dev bypass without requiring
   any of the OIDC_* variables.
3. ``extract_auth_context`` maps ``sub``  ``actor_id``, the
   ``role`` / ``roles`` / ``groups`` claim  ``actor_role`` and the
   ``dept_ids`` / ``departments`` claim  ``dept_ids``.

These tests intentionally avoid network I/O - every call path
operates on plain dicts.
"""

from __future__ import annotations

import pytest

from auth_shared import (
    AuthContext,
    InvalidTokenError,
    MissingClaimError,
    OIDCConfig,
    OIDCValidator,
    extract_auth_context,
)


# ---------------------------------------------------------------------------
# OIDCConfig.from_env
# ---------------------------------------------------------------------------


class TestFromEnvProductionMode:
    def test_resolves_full_oidc_config_from_env(self) -> None:
        cfg = OIDCConfig.from_env(
            {
                "AUTH_PROVIDER": "oidc",
                "OIDC_ISSUER_URL": "https://idp.example.test/",
                "OIDC_CLIENT_ID": "admin-dashboard",
                "OIDC_CLIENT_SECRET": "topsecret",
            }
        )

        assert cfg.auth_mode == "production"
        assert cfg.issuer == "https://idp.example.test/"
        assert cfg.client_id == "admin-dashboard"
        assert cfg.client_secret == "topsecret"
        # Default audience falls back to client_id.
        assert cfg.audience == "admin-dashboard"
        # Default jwks_url is derived from the issuer.
        assert cfg.jwks_url == "https://idp.example.test/.well-known/jwks.json"

    def test_explicit_audience_and_jwks_url_take_precedence(self) -> None:
        cfg = OIDCConfig.from_env(
            {
                "AUTH_PROVIDER": "oidc",
                "OIDC_ISSUER_URL": "https://idp.example.test/",
                "OIDC_CLIENT_ID": "admin-dashboard",
                "OIDC_CLIENT_SECRET": "topsecret",
                "OIDC_AUDIENCE": "internal-services",
                "OIDC_JWKS_URL": "https://idp.example.test/.well-known/jwks-rotated.json",
            }
        )

        assert cfg.audience == "internal-services"
        assert (
            cfg.jwks_url
            == "https://idp.example.test/.well-known/jwks-rotated.json"
        )

    @pytest.mark.parametrize(
        "missing_var",
        ["OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"],
    )
    def test_missing_required_oidc_var_raises_value_error(
        self, missing_var: str
    ) -> None:
        env = {
            "AUTH_PROVIDER": "oidc",
            "OIDC_ISSUER_URL": "https://idp.example.test/",
            "OIDC_CLIENT_ID": "admin-dashboard",
            "OIDC_CLIENT_SECRET": "topsecret",
        }
        env.pop(missing_var)

        with pytest.raises(ValueError):
            OIDCConfig.from_env(env)

    def test_unknown_auth_provider_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            OIDCConfig.from_env({"AUTH_PROVIDER": "saml"})


class TestFromEnvLocalMode:
    def test_local_provider_yields_dev_mode(self) -> None:
        # AUTH_PROVIDER=local is the explicit dev opt-in. It must not
        # require any OIDC_* variables.
        cfg = OIDCConfig.from_env({"AUTH_PROVIDER": "local"})

        assert cfg.auth_mode == "dev"
        assert cfg.client_id is None
        assert cfg.client_secret is None

    def test_local_provider_validator_accepts_any_token(self) -> None:
        # End-to-end check: configure a validator from env and
        # confirm the dev bypass yields the canned admin claims.
        validator = OIDCValidator(
            OIDCConfig.from_env({"AUTH_PROVIDER": "local"})
        )

        claims = validator.validate("anything")

        assert claims["sub"] == "dev-admin"
        assert claims["role"] == "admin"

    def test_local_provider_rejects_empty_token(self) -> None:
        validator = OIDCValidator(
            OIDCConfig.from_env({"AUTH_PROVIDER": "local"})
        )

        with pytest.raises(InvalidTokenError):
            validator.validate("")


# ---------------------------------------------------------------------------
# extract_auth_context
# ---------------------------------------------------------------------------


class TestExtractAuthContext:
    def test_extracts_actor_id_from_sub(self) -> None:
        ctx = extract_auth_context(
            {"sub": "alice@example.test", "role": "admin"}
        )

        assert ctx.actor_id == "alice@example.test"
        assert ctx.actor_role == "admin"
        assert ctx.dept_ids == frozenset()

    def test_role_claim_takes_precedence_over_groups(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "role": "lead",
                # groups still present but ``role`` wins.
                "groups": ["admin"],
            }
        )

        assert ctx.actor_role == "lead"

    def test_falls_back_to_roles_list_claim(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "roles": ["unknown-role", "dept_admin"],
            }
        )

        assert ctx.actor_role == "dept_admin"

    def test_falls_back_to_space_separated_groups_claim(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "groups": "viewer lead",
            }
        )

        # First match wins; both viewer and lead are valid so the
        # earlier item is picked.
        assert ctx.actor_role == "viewer"

    def test_role_matching_is_case_insensitive(self) -> None:
        ctx = extract_auth_context({"sub": "u", "role": "ADMIN"})

        assert ctx.actor_role == "admin"

    def test_no_recognised_role_raises_missing_claim_error(self) -> None:
        with pytest.raises(MissingClaimError):
            extract_auth_context({"sub": "u", "role": "superuser"})

    def test_missing_role_raises_missing_claim_error(self) -> None:
        with pytest.raises(MissingClaimError):
            extract_auth_context({"sub": "u"})

    def test_missing_sub_claim_raises_missing_claim_error(self) -> None:
        with pytest.raises(MissingClaimError):
            extract_auth_context({"role": "admin"})

    def test_missing_claim_error_is_subclass_of_invalid_token(self) -> None:
        # FastAPI exception handlers catch ``InvalidTokenError`` and
        # translate it into HTTP 401; ``MissingClaimError`` must
        # therefore be a subtype so a single ``except`` branch covers
        # both signature failures and missing-claim cases.
        with pytest.raises(InvalidTokenError):
            extract_auth_context({"role": "admin"})

    def test_extracts_dept_ids_from_list_claim(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "role": "dept_admin",
                "dept_ids": ["payments", "risk"],
            }
        )

        assert ctx.dept_ids == frozenset({"payments", "risk"})

    def test_extracts_dept_ids_from_space_separated_string(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "role": "dept_admin",
                "dept_ids": "payments  risk",  # extra whitespace is tolerated
            }
        )

        assert ctx.dept_ids == frozenset({"payments", "risk"})

    def test_falls_back_to_departments_claim(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "role": "lead",
                "departments": ["payments"],
            }
        )

        assert ctx.dept_ids == frozenset({"payments"})

    def test_admin_with_no_dept_claim_is_legal(self) -> None:
        # admin actors do not need explicit dept membership.
        ctx = extract_auth_context({"sub": "admin@example", "role": "admin"})

        assert ctx.dept_ids == frozenset()
        assert ctx.is_admin() is True

    def test_can_access_dept_for_dept_admin(self) -> None:
        ctx = extract_auth_context(
            {
                "sub": "u",
                "role": "dept_admin",
                "dept_ids": ["payments"],
            }
        )

        assert ctx.can_access_dept("payments") is True
        assert ctx.can_access_dept("risk") is False

    def test_can_access_dept_admin_always_allowed(self) -> None:
        ctx = extract_auth_context({"sub": "u", "role": "admin"})

        assert ctx.can_access_dept("anything") is True

    def test_can_access_dept_rejects_empty_dept_id(self) -> None:
        ctx = extract_auth_context(
            {"sub": "u", "role": "dept_admin", "dept_ids": ["payments"]}
        )

        assert ctx.can_access_dept("") is False
