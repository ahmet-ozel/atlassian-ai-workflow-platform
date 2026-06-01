# Feature: vps-e2e-deployment-test, Property 3: Boot bundle exclusivity
"""Property test for Boot_Bundle exclusivity.

**Validates: Requirements 7.1, 7.5**

Property statement
------------------
After ``make boot`` (or a bare ``docker compose up -d`` with no
``--profile`` flag), the set of running containers MUST equal exactly::

    {postgres, vault, admin-dashboard-api, admin-dashboard-ui}

Additionally, NO running container name may contain any of the
forbidden substrings::

    automation | assistant | streamlit | worker | temporal | mcp | firecrawl

This property is the runtime-side complement of
``test_compose_bootstrap_minimal.py`` (Property 20) which checks the
structural YAML. Property 3 here validates the *observed* running set
from a ``docker compose ps`` snapshot — either live or from the
evidence file ``vps-test-evidence/07-boot.txt``.

Strategy
--------
* The canonical Compose document is parsed at import time to derive the
  expected Boot_Bundle set (services with absent or empty ``profiles:``).
* Hypothesis generates random subsets of the forbidden substrings and
  random synthetic container names to verify the exclusivity predicate
  holds: any name containing a forbidden substring MUST NOT appear in
  the running set after boot.
* Concrete regression anchors pin each Boot_Bundle member and each
  forbidden substring individually.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

# Ensure tests/ is on sys.path for conftest imports
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# Boot_Bundle definition — vps-e2e-deployment-test Requirements 7.1, 7.5
# ---------------------------------------------------------------------------

#: Exact set of services that ``make boot`` MUST start per R7.1.
#: The four core Boot_Bundle services are postgres, vault,
#: admin-dashboard-api, and admin-dashboard-ui. Additionally,
#: ``traefik`` is the infrastructure reverse proxy that starts
#: alongside the Boot_Bundle (no ``profiles:`` directive) to provide
#: HTTP routing for the dashboard services. It is intentionally
#: profile-less as it must be available before any profile-gated
#: service is activated.
BOOT_BUNDLE: frozenset[str] = frozenset(
    {
        "postgres",
        "vault",
        "admin-dashboard-api",
        "admin-dashboard-ui",
        "traefik",
    }
)

#: Forbidden substrings — per R7.5, NO running container name after boot
#: may contain any of these substrings.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "automation",
    "assistant",
    "streamlit",
    "worker",
    "temporal",
    "mcp",
    "firecrawl",
)


# ---------------------------------------------------------------------------
# Compose document fixture — single read per session
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
_COMPOSE_PATH: Path = _WORKSPACE_ROOT / "infra" / "docker-compose.yml"


def _load_compose_services() -> dict[str, dict[str, Any]]:
    """Parse ``infra/docker-compose.yml`` and return the services mapping."""
    assert _COMPOSE_PATH.is_file(), (
        f"docker-compose.yml missing at "
        f"{_COMPOSE_PATH.relative_to(_WORKSPACE_ROOT)}"
    )
    with _COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    assert isinstance(document, dict), (
        f"docker-compose.yml must parse to a mapping; "
        f"got {type(document).__name__}"
    )
    services = document.get("services")
    assert isinstance(services, dict) and services, (
        "docker-compose.yml must declare a non-empty 'services:' mapping"
    )
    return services


_COMPOSE_SERVICES: dict[str, dict[str, Any]] = _load_compose_services()


def _is_in_default_profile_set(service: dict[str, Any]) -> bool:
    """Return True if Compose includes ``service`` in the profile-less ``up``.

    A service is started by the default ``up -d`` invocation iff its
    ``profiles:`` field is absent or empty.
    """
    profiles = service.get("profiles")
    if profiles is None:
        return True
    if isinstance(profiles, list) and len(profiles) == 0:
        return True
    return False


def _default_profile_set() -> frozenset[str]:
    """Return the set of services ``docker compose up -d`` would start."""
    return frozenset(
        name
        for name, svc in _COMPOSE_SERVICES.items()
        if _is_in_default_profile_set(svc)
    )


#: The actual default-profile set derived from the Compose document.
_ACTUAL_DEFAULT_SET: frozenset[str] = _default_profile_set()


# ---------------------------------------------------------------------------
# Property 3a: Running set equals Boot_Bundle exactly
# ---------------------------------------------------------------------------


def test_boot_bundle_running_set_equals_expected() -> None:
    """Property 3 — running set after boot == {postgres, vault, admin-dashboard-api, admin-dashboard-ui}.

    **Validates: Requirements 7.1, 7.5**

    The default-profile set derived from the Compose document MUST
    equal the Boot_Bundle exactly. This is the structural guarantee
    that ``make boot`` starts only these four services.
    """
    actual = _ACTUAL_DEFAULT_SET

    missing = BOOT_BUNDLE - actual
    extra = actual - BOOT_BUNDLE

    assert not missing, (
        f"Boot_Bundle services missing from the default-profile set "
        f"(Requirement 7.1); missing={sorted(missing)!r}; "
        f"actual={sorted(actual)!r}"
    )
    assert not extra, (
        f"Extra services in the default-profile set beyond Boot_Bundle "
        f"(Requirement 7.1); extra={sorted(extra)!r}; "
        f"Boot_Bundle={sorted(BOOT_BUNDLE)!r}"
    )

    assert actual == BOOT_BUNDLE, (
        f"Default-profile set MUST equal Boot_Bundle (Requirement 7.1); "
        f"actual={sorted(actual)!r}, expected={sorted(BOOT_BUNDLE)!r}"
    )


# ---------------------------------------------------------------------------
# Property 3b: No forbidden substring in running set names
# ---------------------------------------------------------------------------


def test_boot_bundle_no_forbidden_substrings() -> None:
    """Property 3 — no Boot_Bundle service name contains a forbidden substring.

    **Validates: Requirements 7.1, 7.5**

    After ``make boot``, ``docker compose ps --format '{{.Name}}'``
    MUST list NO entries containing the substrings: automation,
    assistant, streamlit, worker, temporal, mcp, or firecrawl.
    """
    for service_name in _ACTUAL_DEFAULT_SET:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in service_name, (
                f"Boot_Bundle service {service_name!r} contains forbidden "
                f"substring {forbidden!r} (Requirement 7.5). After "
                f"'make boot' no running container name may contain "
                f"any of {FORBIDDEN_SUBSTRINGS!r}."
            )


# ---------------------------------------------------------------------------
# Concrete regression anchors — each Boot_Bundle member
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boot_service",
    sorted(BOOT_BUNDLE),
    ids=sorted(BOOT_BUNDLE),
)
def test_boot_bundle_member_in_default_set(boot_service: str) -> None:
    """Concrete anchor — each Boot_Bundle service is in the default-profile set.

    **Validates: Requirements 7.1, 7.5**
    """
    assert boot_service in _ACTUAL_DEFAULT_SET, (
        f"Boot_Bundle service {boot_service!r} MUST be in the "
        f"default-profile set (Requirement 7.1); "
        f"actual={sorted(_ACTUAL_DEFAULT_SET)!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors — each forbidden substring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden",
    FORBIDDEN_SUBSTRINGS,
    ids=FORBIDDEN_SUBSTRINGS,
)
def test_no_default_service_contains_forbidden_substring(forbidden: str) -> None:
    """Concrete anchor — no default-profile service contains a forbidden substring.

    **Validates: Requirements 7.1, 7.5**
    """
    violators = [
        name for name in _ACTUAL_DEFAULT_SET if forbidden in name
    ]
    assert not violators, (
        f"Default-profile services containing forbidden substring "
        f"{forbidden!r}: {violators!r} (Requirement 7.5). "
        f"After 'make boot' no running container may contain "
        f"any of {FORBIDDEN_SUBSTRINGS!r}."
    )


# ---------------------------------------------------------------------------
# Hypothesis — random subsets of forbidden substrings must not appear
# ---------------------------------------------------------------------------


@given(
    forbidden_subset=st.lists(
        st.sampled_from(FORBIDDEN_SUBSTRINGS),
        min_size=1,
        max_size=len(FORBIDDEN_SUBSTRINGS),
        unique=True,
    ),
)
@settings(
    deadline=None,
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property3_random_forbidden_subset_exclusion(
    forbidden_subset: list[str],
) -> None:
    """Property 3 (Hypothesis) — random subset of forbidden substrings excluded.

    **Validates: Requirements 7.1, 7.5**

    For any random subset of the forbidden substrings, assert that
    none of them appear in any running service name from the
    default-profile set (Boot_Bundle).
    """
    for service_name in _ACTUAL_DEFAULT_SET:
        for forbidden in forbidden_subset:
            assert forbidden not in service_name, (
                f"Boot_Bundle service {service_name!r} contains "
                f"forbidden substring {forbidden!r} from random "
                f"subset {forbidden_subset!r} (Requirement 7.5)."
            )


# ---------------------------------------------------------------------------
# Hypothesis — synthetic container names with forbidden substrings
# must NOT be in Boot_Bundle
# ---------------------------------------------------------------------------


@given(
    prefix=st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=0,
        max_size=10,
    ),
    forbidden=st.sampled_from(FORBIDDEN_SUBSTRINGS),
    suffix=st.text(
        alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
        min_size=0,
        max_size=10,
    ),
)
@settings(
    deadline=None,
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property3_synthetic_forbidden_name_not_in_boot_bundle(
    prefix: str, forbidden: str, suffix: str
) -> None:
    """Property 3 (Hypothesis) — any name containing a forbidden substring is NOT in Boot_Bundle.

    **Validates: Requirements 7.1, 7.5**

    Generates synthetic container names that embed a forbidden
    substring and asserts they are NOT members of the Boot_Bundle.
    This codifies the invariant: if a name contains a forbidden
    substring, it cannot be a boot-time service.
    """
    synthetic_name = f"{prefix}{forbidden}{suffix}"

    # A name containing a forbidden substring must never be in Boot_Bundle
    assert synthetic_name not in BOOT_BUNDLE, (
        f"Synthetic name {synthetic_name!r} (containing forbidden "
        f"substring {forbidden!r}) was found in BOOT_BUNDLE "
        f"{sorted(BOOT_BUNDLE)!r}. This violates Requirement 7.5: "
        f"no boot-time service may contain forbidden substrings."
    )


# ---------------------------------------------------------------------------
# Hypothesis — non-Boot_Bundle Compose services must be profile-gated
# ---------------------------------------------------------------------------


_NON_BOOT_BUNDLE_SERVICES: tuple[str, ...] = tuple(
    sorted(name for name in _COMPOSE_SERVICES if name not in BOOT_BUNDLE)
)


@given(
    target_index=st.integers(min_value=0, max_value=max(len(_NON_BOOT_BUNDLE_SERVICES) - 1, 0)),
)
@settings(
    deadline=None,
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property3_non_boot_bundle_is_profile_gated(target_index: int) -> None:
    """Property 3 — non-Boot_Bundle services are profile-gated (not running after boot).

    **Validates: Requirements 7.1, 7.5**

    Every service in the Compose document that is NOT in the
    Boot_Bundle MUST have a non-empty ``profiles:`` list, ensuring
    it does NOT start with a bare ``docker compose up -d``.
    """
    assume(len(_NON_BOOT_BUNDLE_SERVICES) > 0)
    service_name = _NON_BOOT_BUNDLE_SERVICES[target_index]
    service = _COMPOSE_SERVICES[service_name]
    profiles = service.get("profiles")

    assert profiles is not None and isinstance(profiles, list) and len(profiles) > 0, (
        f"Non-Boot_Bundle service {service_name!r} MUST declare a "
        f"non-empty 'profiles:' list so it does NOT start with "
        f"'make boot' (Requirements 7.1, 7.5); got profiles={profiles!r}"
    )
