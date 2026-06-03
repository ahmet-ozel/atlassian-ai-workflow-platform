"""invariant — Compose profile labels structural consistency.


on for ``task-intake-service``).

Invariant statement
------------------
The parsed ``infra/docker-compose.yml`` document MUST satisfy *all* of
the following invariants jointly:

1. **Boot_Bundle absence-of-profiles** — for every
 service in the Boot_Bundle (``admin-dashboard-ui``,
 ``admin-dashboard-api``, ``postgres``, ``vault``) the ``profiles:``
 directive is either **absent** OR an explicitly empty list. Either
 shape causes Compose to include the service in the default
 profile-less ``up -d`` invocation, which is what Boot_Bundle
 semantics require.
2. **Managed_Service self-naming** — for every
 ``ManagedServiceEntry`` ``S`` declared in
 ``config/services.manifest.json``, the Compose service named
 ``S.compose_service_name`` MUST declare a non-empty ``profiles:``
 list, and that list MUST contain at least one value that is
 **byte-for-byte equal** to ``S.name``. This is the property that
 ``docker compose --profile <S.name> up -d <S.compose_service_name>``
 deterministically targets exactly one service.
3. **task-intake backward-compat** — *if*
 ``task-intake-service`` is listed in the manifest, its Compose
 ``profiles:`` list MUST contain BOTH the legacy ``"task-intake"``
 label and the new ``"task-intake-service"`` label. Removing the legacy label
 would break the existing ``docker compose --profile task-intake``
 invocation path, which explicitly forbids.

Strategy
--------
* Hypothesis runs ``st.sampled_from(_MANAGED_SERVICES)`` to pick a
 Managed_Service entry per example. ``@settings(deadline=None,
 max_examples=20)`` keeps the test budget bounded; the manifest only
 declares a handful of entries so ``max_examples=20`` lets Hypothesis
 cover every entry multiple times.
* The Compose document is parsed once at import time via
 ``yaml.safe_load`` (Compose anchor / merge keys are flattened into
 plain dicts). Re-reading on every Hypothesis example would make the
 test I/O-bound for no semantic gain.
* Concrete regression anchors are added via ``pytest.mark.parametrize``
 for both Boot_Bundle services and every manifest-driven service, so a
 bug in the property-test wiring (e.g. the manifest accidentally
 becoming empty) cannot silently green-out the suite.

Module layout mirrors:mod:`tests.property.test_form_schema_lhs_match`:
``tests/`` and ``services/admin-dashboard-api`` are added to
``sys.path`` defensively so the file imports cleanly under direct
``python -m pytest tests/property`` invocations.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest auto-loads it but we
# add ``tests/`` to ``sys.path`` defensively so this module also imports
# cleanly under a direct ``python -m pytest tests/property`` invocation
# (mirrors the pattern used by every other invariant in this folder).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way ``test_form_schema_lhs_match.py`` does. This lets us
# ``import src.manifest`` directly.
_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
_SERVICE_ROOT: Path = _WORKSPACE_ROOT / "services" / "admin-dashboard-api"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.manifest import (  # noqa: E402
    ManagedServiceEntry,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Boot_Bundle definition — / Glossary
# ---------------------------------------------------------------------------

#: Infrastructure + control-plane services that ``docker compose -f
#: infra/docker-compose.yml up -d`` (no ``--profile`` flag) MUST start.
#: Per each of these services MUST either omit the
#: ``profiles:`` directive entirely or declare it as an empty list.
#:
#: Note: ``admin-dashboard-api`` and ``admin-dashboard-ui`` are included
#: here even though Managed_Service entries normally declare
#: ``profiles:`` containing their ``compose_service_name``.
#:
#: The bootstrap behavior takes precedence for these services:
#: a fresh ``docker compose up -d`` MUST start exactly the four
#: Boot_Bundle services (admin-dashboard-ui, admin-dashboard-api,
#: postgres, vault) so that the dashboard's Setup Wizard
#: can drive activation of
#: every other service. The two admin-dashboard entries therefore no
#: longer carry ``profiles:`` in ``infra/docker-compose.yml`` (see the
#: header comment block of that file). The ordering here is purely
#: presentational — the property is set-shaped.
_BOOT_BUNDLE_SERVICES: tuple[str, ...] = (
    "admin-dashboard-ui",
    "admin-dashboard-api",
    "postgres",
    "vault",
)


# ---------------------------------------------------------------------------
# Compose document fixture — single read per session
# ---------------------------------------------------------------------------

_COMPOSE_PATH: Path = _WORKSPACE_ROOT / "infra" / "docker-compose.yml"


def _load_compose() -> dict[str, Any]:
    """Parse ``infra/docker-compose.yml`` with ``yaml.safe_load``.

 YAML anchor / merge keys (``<<: *http-healthcheck``) are resolved
 into plain dicts by ``safe_load``; this is identical to the parse
 done by ``test_compose_structure.py`` so both property suites see
 the same logical document.
 """

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
    return document


#: Parsed Compose document, loaded once at module import time so the
#: per-example Hypothesis cost is the property check itself, not YAML
#: parsing. Mirrors the pattern in ``test_form_schema_lhs_match.py``
#: where the manifest is loaded once.
_COMPOSE_DOC: dict[str, Any] = _load_compose()
_COMPOSE_SERVICES: dict[str, dict[str, Any]] = _COMPOSE_DOC["services"]


# ---------------------------------------------------------------------------
# Manifest discovery — drives ``st.sampled_from`` and parametrize ids
# ---------------------------------------------------------------------------

#: Tuple of every Managed_Service entry, loaded once per session. The
#: variable name matches the wording of the task description
#: ("``st.sampled_from(MANAGED_SERVICES_FROM_MANIFEST)``") so future
#: readers can grep the spec back to this strategy source.
_MANAGED_SERVICES: tuple[ManagedServiceEntry, ...] = load_manifest(_WORKSPACE_ROOT)
assert _MANAGED_SERVICES, (
    "config/services.manifest.json must declare at least one Managed_Service "
    "for invariant to be meaningful"
)
MANAGED_SERVICES_FROM_MANIFEST: tuple[ManagedServiceEntry, ...] = _MANAGED_SERVICES

#: Manifest entries that are NOT in the Boot_Bundle. The
#: profile-presence invariants (invariant (b), invariant (compose-side
#: profile membership)) only apply to non-Boot_Bundle services because
#: admin-dashboard-ui and admin-dashboard-api must carry NO ``profiles:``
#: directive (they are part of the default-profile-set Boot_Bundle).
#: The manifest still lists them so the dashboard can manage their
#: lifecycle metadata uniformly, but the Compose-side profile checks
#: would otherwise conflict with the bootstrap behavior.
_BOOT_BUNDLE_SET: frozenset[str] = frozenset(_BOOT_BUNDLE_SERVICES)
_NON_BOOT_BUNDLE_MANAGED_SERVICES: tuple[ManagedServiceEntry, ...] = tuple(
    entry
    for entry in _MANAGED_SERVICES
    if entry.compose_service_name not in _BOOT_BUNDLE_SET
)
assert _NON_BOOT_BUNDLE_MANAGED_SERVICES, (
    "after filtering Boot_Bundle entries, services.manifest.json must "
    "still declare at least one Managed_Service for invariant(b) to "
    "be meaningful"
)


# ---------------------------------------------------------------------------
# Helpers — normalise the ``profiles:`` field across YAML shapes
# ---------------------------------------------------------------------------


def _profiles_of(service_name: str) -> list[str] | None:
    """Return the ``profiles:`` list for ``service_name``, or ``None`` if absent.

 Compose YAML lets ``profiles`` be:

 * **Absent** — the service is included in the default ``up -d``.
 * **An explicit empty list** (``profiles: []``) — semantically
 identical to absent for the default-profile-set inclusion check.
 * **A non-empty list of strings** — the service is *only* included
 when one of those profile names is activated.

 We return ``None`` for the absent case and a list (possibly empty)
 otherwise, so the caller can distinguish "no directive" from
 "directive present but empty" in error messages without losing the
 Boot_Bundle equivalence collapses both into
 "default-profile-set membership").
 """

    service = _COMPOSE_SERVICES.get(service_name)
    assert service is not None, (
        f"Compose document is missing service {service_name!r}; "
        f"declared services: {sorted(_COMPOSE_SERVICES.keys())!r}"
    )

    if "profiles" not in service:
        return None

    profiles = service["profiles"]
    # Compose schema accepts only list-of-strings here; we assert that
    # explicitly so a malformed YAML (string instead of list, dict, …)
    # produces a deterministic failure rather than a confusing
    # downstream ``TypeError``.
    assert isinstance(profiles, list), (
        f"{service_name}: 'profiles' must be a list; "
        f"got {type(profiles).__name__} ({profiles!r})"
    )
    for index, value in enumerate(profiles):
        assert isinstance(value, str), (
            f"{service_name}: profiles[{index}] must be a string; "
            f"got {type(value).__name__} ({value!r})"
        )
    return list(profiles)


# ---------------------------------------------------------------------------
# invariant (a) — Boot_Bundle services declare no profile gating
# ---------------------------------------------------------------------------


@given(boot_service=st.sampled_from(_BOOT_BUNDLE_SERVICES))
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_boot_bundle_services_have_no_profile_gating(boot_service: str) -> None:
    """invariant (a) — Boot_Bundle services have absent or empty ``profiles:``.



 Boot_Bundle semantics demand that ``docker compose -f
 infra/docker-compose.yml up -d`` (no ``--profile`` flag) start
 exactly the four Boot_Bundle services. Compose includes a service
 in the default profile-less invocation iff its ``profiles:`` field
 is absent OR empty — any non-empty value gates the service behind
 a profile flag. This property pins both shapes as acceptable and
 rejects any non-empty list for the Boot_Bundle members.
 """

    profiles = _profiles_of(boot_service)
    assert profiles is None or profiles == [], (
        f"Boot_Bundle service {boot_service!r} MUST have an absent or "
        f"empty 'profiles:' directive (the operational rule); got {profiles!r}"
    )


# ---------------------------------------------------------------------------
# invariant (b) — every Managed_Service is self-named in profiles
# ---------------------------------------------------------------------------


@given(entry=st.sampled_from(_NON_BOOT_BUNDLE_MANAGED_SERVICES))
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_managed_service_profiles_contain_self_name(
    entry: ManagedServiceEntry,
) -> None:
    """invariant (b) — non-Boot_Bundle Managed_Service ``profiles:`` contains its own name.


 Boot_Bundle exemption)

 For every non-Boot_Bundle Managed_Service ``S`` in the manifest, the
 Compose service ``S.compose_service_name`` MUST declare a non-empty
 ``profiles:`` list and that list MUST contain at
 least one entry that is byte-for-byte equal to ``S.name``. The latter is the invariant that
 ``docker compose --profile <S.name> up -d <S.compose_service_name>``
 deterministically resolves to a single service.

 Boot_Bundle services (admin-dashboard-ui, admin-dashboard-api,
 postgres, vault) are intentionally exempt:
 the bootstrap behavior mandates they carry NO ``profiles:``
 so a bare ``docker compose up -d`` starts only those four. They
 are pinned by the separate Boot_Bundle properties above.
 """

    profiles = _profiles_of(entry.compose_service_name)

    # — directive present and non-empty.
    assert profiles is not None, (
        f"Managed_Service {entry.name!r} maps to Compose service "
        f"{entry.compose_service_name!r} which MUST declare a 'profiles:' "
        f"directive (the operational rule); none found"
    )
    assert profiles, (
        f"Managed_Service {entry.name!r} maps to Compose service "
        f"{entry.compose_service_name!r} which MUST declare a non-empty "
        f"'profiles:' list (the operational rule); got an empty list"
    )

    # — list contains the manifest ``name`` verbatim.
    assert entry.name in profiles, (
        f"Managed_Service {entry.name!r} maps to Compose service "
        f"{entry.compose_service_name!r} whose 'profiles:' list MUST "
        f"contain the manifest name {entry.name!r} (the operational rule); "
        f"got profiles={profiles!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors — Boot_Bundle (parametrize)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boot_service",
    _BOOT_BUNDLE_SERVICES,
    ids=list(_BOOT_BUNDLE_SERVICES),
)
def test_boot_bundle_anchor_no_profiles(boot_service: str) -> None:
    """Concrete anchor — every Boot_Bundle service is profile-free.

 The Hypothesis-driven property above samples from the same set,
 but a wiring bug that accidentally narrowed
 ``_BOOT_BUNDLE_SERVICES`` would still pass. This pytest-level
 parametrize pins each of the four named services individually so
 a regression that drops one is caught with a fully readable test
 id (``test_boot_bundle_anchor_no_profiles[postgres]``).
 """

    profiles = _profiles_of(boot_service)
    assert profiles is None or profiles == [], (
        f"Boot_Bundle service {boot_service!r}: 'profiles:' MUST be "
        f"absent or empty (the operational rule); got {profiles!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors — Managed_Service self-naming (parametrize)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _NON_BOOT_BUNDLE_MANAGED_SERVICES,
    ids=[e.name for e in _NON_BOOT_BUNDLE_MANAGED_SERVICES],
)
def test_managed_service_anchor_profiles_contains_own_name(
    entry: ManagedServiceEntry,
) -> None:
    """Concrete anchor — every non-Boot_Bundle service's profile list includes its name.

 Mirrors:func:`test_managed_service_profiles_contain_self_name`
 but as an exhaustive parametrize over the real manifest minus
 Boot_Bundle entries. This
 catches the case where Hypothesis's shrinking might otherwise hide
 a single-service regression behind successful samples of the rest.
 """

    profiles = _profiles_of(entry.compose_service_name)
    assert profiles is not None and profiles, (
        f"Managed_Service {entry.name!r} (compose_service_name="
        f"{entry.compose_service_name!r}) MUST declare a non-empty "
        f"'profiles:' list (the operational rule); got {profiles!r}"
    )
    assert entry.name in profiles, (
        f"Managed_Service {entry.name!r} (compose_service_name="
        f"{entry.compose_service_name!r}): 'profiles:' MUST contain "
        f"{entry.name!r} (the operational rule); got {profiles!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchor — task-intake backward compatibility
# ---------------------------------------------------------------------------


# Resolve the manifest entry (if any) for ``task-intake-service``.
# This only fires when the service is actually managed; if
# the manifest were ever to drop it the legacy-label preservation
# concern becomes moot.
_TASK_INTAKE_ENTRY: ManagedServiceEntry | None = next(
    (e for e in _MANAGED_SERVICES if e.name == "task-intake-service"),
    None,
)


@pytest.mark.skipif(
    _TASK_INTAKE_ENTRY is None,
    reason="task-intake-service is not declared in services.manifest.json",
)
def test_task_intake_service_keeps_legacy_profile_label() -> None:
    """Concrete anchor — ``task-intake-service`` keeps both labels.


 The project keeps the original ``profiles: ["task-intake"]`` value for
 ``task-intake-service``. The dashboard activation flow also uses the canonical
 ``"task-intake-service"`` label so the manifest-driven invocation
 pattern works, while preserving the
 legacy ``"task-intake"`` entry so operators with existing
 ``docker compose --profile task-intake`` workflows MUST continue
 to work. This anchor pins both labels.
 """

    assert _TASK_INTAKE_ENTRY is not None  # for type narrowing
    profiles = _profiles_of(_TASK_INTAKE_ENTRY.compose_service_name)
    assert profiles is not None and profiles, (
        "task-intake-service: 'profiles:' MUST be present and non-empty "
        f"(the operational rule); got {profiles!r}"
    )
    assert "task-intake" in profiles, (
        "task-intake-service: 'profiles:' MUST preserve the legacy "
        f"'task-intake' label for existing operator workflows; "
        f"got {profiles!r}"
    )
    assert "task-intake-service" in profiles, (
        "task-intake-service: 'profiles:' MUST contain the canonical "
        f"'task-intake-service' label (the operational rule); "
        f"got {profiles!r}"
    )


# ===========================================================================
# invariant: Servis topolojisi ve compose-manifest
# shape tutarlılığı — profile-side invariants.
#
#
# This block extends the invariant with the
# stricter contract on profile membership:
#
# 1. The ``profiles:`` list of every Managed_Service MUST contain its
# own ``compose_service_name``. The pre-existing invariant(b) only required the list
# to contain the manifest ``name``; in the current manifest those
# two fields happen to coincide, but this check pins the
# ``compose_service_name`` form so a future schema split would still satisfy the
# invariant.
#
# 2. Every entry in the required service list (regardless of whether it
# is also profile-gated) MUST be present in the parsed Compose
# document. This catches manifest entries whose Compose service was
# never wired up (a class of bug invariant alone cannot detect
# because its sample space is the manifest, not the Compose stack).
# ===========================================================================


# ---------------------------------------------------------------------------
# Foundation 10-entry topology — required Compose service set
# ---------------------------------------------------------------------------

#: Compose service names mandated by the required service topology. The set MUST
#: be a subset of the parsed Compose ``services:`` mapping;
#: ``task-intake-service`` and other carryovers from prior specs are
#: tolerated as additional entries.
_FOUNDATION_REQUIRED_COMPOSE_SERVICES: frozenset[str] = frozenset(
    {
        "automation-service",
        "assistant-service",
        "admin-dashboard-api",
        "agent-runner-worker",
        "execution-runner-worker",
        "atlassian-mcp",
        "firecrawl",
        "opencode-sidecar",
        "streamlit-ui",
        "admin-dashboard-ui",
    }
)


def test_foundation_compose_declares_all_canonical_services() -> None:
    """invariant (compose-side) — every foundation service is in Compose.



 The foundation 10-entry topology must round-trip from manifest to
 ``infra/docker-compose.yml``. A missing service here means
 ``docker compose --profile <name> up -d <compose_service_name>``
 has nothing to start, regardless of whether the manifest is
 well-formed.
 """

    declared = frozenset(_COMPOSE_SERVICES.keys())
    missing = _FOUNDATION_REQUIRED_COMPOSE_SERVICES - declared
    assert not missing, (
        "infra/docker-compose.yml must declare every required "
        f"service (the operational rule, invariant); missing: "
        f"{sorted(missing)!r}; declared: {sorted(declared)!r}"
    )


# ---------------------------------------------------------------------------
# invariant — Managed_Service profiles contain compose_service_name
# ---------------------------------------------------------------------------


@given(entry=st.sampled_from(_NON_BOOT_BUNDLE_MANAGED_SERVICES))
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_foundation_profiles_contain_compose_service_name(
    entry: ManagedServiceEntry,
) -> None:
    """invariant — non-Boot_Bundle ``profiles:`` MUST contain ``compose_service_name``.


 exemption — invariant).

 Managed services expose their ``compose_service_name`` in the ``profiles`` list.
 The pre-existing invariant(b) checks that ``profiles`` contains ``entry.name``; this
 additional check pins the ``compose_service_name`` form so the
 invariant survives a future schema split where the two fields no
 longer coincide.

 Boot_Bundle services (admin-dashboard-ui, admin-dashboard-api,
 postgres, vault) are exempt because the bootstrap behavior mandates they carry no ``profiles:`` directive
 so a bare ``docker compose up -d`` starts only the bootstrap set.
 """

    profiles = _profiles_of(entry.compose_service_name)
    assert profiles is not None and profiles, (
        f"Managed_Service {entry.name!r} (compose_service_name="
        f"{entry.compose_service_name!r}) MUST declare a non-empty "
        f"'profiles:' list (the operational rule, invariant); "
        f"got {profiles!r}"
    )
    assert entry.compose_service_name in profiles, (
        f"Managed_Service {entry.name!r}: 'profiles:' MUST contain "
        f"the compose_service_name {entry.compose_service_name!r} "
        f"(the operational rule, invariant); got {profiles!r}"
    )
