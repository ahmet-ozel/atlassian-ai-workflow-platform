"""Stop lifecycle purge profile guard property tests.

The ``ServicesLifecycleRouter.stop`` endpoint MUST refuse a
``purge_vault=true`` flag when the deployment profile is
``production`` - the dev-only Vault purge is a developer-tool
escape hatch and must never apply on a production cluster.

The test exercises a tiny state-machine guard that mirrors the
production check; it is independent of the actual router so the
guard's semantics can evolve without touching the HTTP wiring.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


class _PurgeRejected(RuntimeError):
    """Raised by the guard when purge_vault is forbidden."""


def _guard(*, deployment_profile: str, purge_vault: bool) -> None:
    """Mirror of ServicesLifecycleRouter._guard_purge_vault."""

    if purge_vault and deployment_profile.lower() == "production":
        raise _PurgeRejected(
            "purge_vault=true is forbidden on the production profile"
        )


_PROFILE = st.sampled_from(["dev", "staging", "production"])


@settings(max_examples=200, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(profile=_PROFILE, purge_vault=st.booleans())
def test_guard_blocks_production_vault_purge(
    profile: str, purge_vault: bool
) -> None:
    if profile == "production" and purge_vault:
        with pytest.raises(_PurgeRejected):
            _guard(deployment_profile=profile, purge_vault=purge_vault)
    else:
        # Every other combination must be a no-op.
        _guard(deployment_profile=profile, purge_vault=purge_vault)


def test_case_insensitive_production_match() -> None:
    with pytest.raises(_PurgeRejected):
        _guard(deployment_profile="PRODUCTION", purge_vault=True)
    with pytest.raises(_PurgeRejected):
        _guard(deployment_profile="Production", purge_vault=True)
