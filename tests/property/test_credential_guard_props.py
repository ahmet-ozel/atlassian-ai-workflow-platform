"""invariant for Credential Guard decision correctness.



invariant: Credential Guard Decision Correctness

For any combination of ``platform_env`` value and credential map,
``check_credentials`` SHALL return ``blocked=True`` if and only if
``platform_env == "production"`` AND at least one credential in the map
matches a value in the ``DEV_DEFAULTS`` list. For any non-production
environment (including ``"development"``, empty string, or undefined),
the function SHALL return ``blocked=False`` regardless of credential values.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the credential_guard module is importable from the service source tree.
_SERVICE_SRC = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "admin-dashboard-api"
    / "src"
)
if str(_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVICE_SRC))

from lifecycle.credential_guard import (
    DEV_DEFAULTS,
    DevDefault,
    check_credentials,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Platform environment values: production triggers blocking, others do not.
_PLATFORM_ENV = st.sampled_from(["production", "development", "", "staging"])

# Credential keys: the three monitored env vars plus an unrelated one.
_CREDENTIAL_KEYS = st.sampled_from(
    ["POSTGRES_PASSWORD", "VAULT_TOKEN", "MINIO_ROOT_PASSWORD", "OTHER_VAR"]
)

# Credential values: known insecure dev-defaults plus safe values.
_CREDENTIAL_VALUES = st.sampled_from(
    ["ai_dev_only", "dev-token-not-for-prod", "miniosecret_dev_only", "secure-password-123", ""]
)


# ---------------------------------------------------------------------------
# invariant
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    platform_env=_PLATFORM_ENV,
    credentials=st.dictionaries(
        keys=_CREDENTIAL_KEYS,
        values=_CREDENTIAL_VALUES,
        min_size=1,
        max_size=5,
    ),
)
def test_credential_guard_decision(platform_env: str, credentials: dict[str, str]) -> None:
    """Feature:, invariant: Credential Guard Decision Correctness



 For every combination of platform_env and credential map, the guard
 returns blocked=True iff platform_env is "production" AND at least one
 credential matches a known dev-default value from DEV_DEFAULTS.
 """
    result = check_credentials(platform_env=platform_env, env_vars=credentials)

    is_production = platform_env == "production"
    has_dev_default = any(
        credentials.get(d.env_var) == d.insecure_value
        for d in DEV_DEFAULTS
    )

    assert result.blocked == (is_production and has_dev_default), (
        f"Expected blocked={is_production and has_dev_default}, got blocked={result.blocked}. "
        f"platform_env={platform_env!r}, credentials={credentials!r}"
    )
