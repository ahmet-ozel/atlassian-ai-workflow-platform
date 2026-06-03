"""invariant — Compose bootstrap minimal footprint.



Invariant statement
------------------
A fresh ``docker compose -f infra/docker-compose.yml up -d`` invocation
(no ``--profile`` flag, no prior state) MUST start exactly the four
Boot_Bundle services and nothing else::

 Boot_Bundle = {admin-dashboard-ui, admin-dashboard-api, postgres, vault}

Compose's profile-resolution rule is: a service is included in the
default profile-less ``up`` iff its ``profiles:`` field is **absent or
an empty list**. Any non-empty ``profiles:`` value gates the service
behind ``--profile <name>``. invariant therefore reduces to a
purely structural assertion on the parsed Compose document::

 {svc | svc has no/empty 'profiles:'} == Boot_Bundle

This test does not invoke the Docker daemon — it parses the YAML and
checks the equality. The companion integration test under
``tests/integration/test_boot_bundle.py`` covers the runtime side
(actually running ``compose up --wait`` and probing the four services).

Cross-reference with the manifest
---------------------------------
``config/services.manifest.json`` is the single source of truth for the
profile names that the dashboard's Compose_Manager activates. The
manifest is parsed independently of the Python loader (which currently
applies a strict JSON Schema that does not yet whitelist the
``smoke_test_command`` extension field) so this test stays decoupled
from upstream loader changes. We assert that every non-Boot_Bundle
manifest entry declares a non-empty string ``compose_profile`` — that
property is what makes ``docker compose --profile {compose_profile}
up -d`` deterministically resolvable from the dashboard UI.

Strategy
--------
* The Compose document is parsed once at import time so the per-example
 Hypothesis cost is the property check itself, not YAML parsing.
* Hypothesis explores **mutations** of the parsed document — for each
 example we synthesise a "candidate Compose" by either (a) flipping a
 profile-gated service into the default-profile-set, (b) flipping a
 Boot_Bundle service into a profile-gated state, or (c) adding a new
 fictional profile-less service. invariant says any of these
 mutations MUST violate the Boot_Bundle equality, so the property is
 ``mutation_violates_boot_bundle`` for synthetic inputs and
 ``equals_boot_bundle`` for the canonical document.
* Concrete regression anchors are added via ``pytest.mark.parametrize``
 for each Boot_Bundle service so a wiring bug that empties the
 Hypothesis sample space cannot silently green-out the suite.

Module layout mirrors:mod:`tests.property.test_compose_profiles` so
``tests/`` is added to ``sys.path`` defensively for direct
``python -m pytest tests/property`` invocations.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest auto-loads it but we
# add ``tests/`` to ``sys.path`` defensively so this module also imports
# cleanly under a direct ``python -m pytest tests/property`` invocation.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ---------------------------------------------------------------------------
# Boot_Bundle definition —
# ---------------------------------------------------------------------------

#: Exact set of services that ``docker compose -f
#: infra/docker-compose.yml up -d`` (no ``--profile`` flag) MUST start
#: per / invariant. The set is
#: intentionally tiny — admin Setup Wizard activates everything else
#: on demand via ``docker compose --profile {compose_profile} up -d``.
BOOT_BUNDLE: frozenset[str] = frozenset(
    {
        "admin-dashboard-ui",
        "admin-dashboard-api",
        "postgres",
        "vault",
    }
)


# ---------------------------------------------------------------------------
# Compose / manifest fixture — single read per session
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
_COMPOSE_PATH: Path = _WORKSPACE_ROOT / "infra" / "docker-compose.yml"
_MANIFEST_PATH: Path = _WORKSPACE_ROOT / "config" / "services.manifest.json"


def _load_compose() -> dict[str, Any]:
    """Parse ``infra/docker-compose.yml`` with ``yaml.safe_load``.

 YAML anchor / merge keys (``<<: *http-healthcheck``) are resolved
 into plain dicts by ``safe_load``. The parsed document is asserted
 to be a non-empty mapping with a non-empty ``services:`` block.
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


def _load_manifest() -> dict[str, Any] | None:
    """Parse ``config/services.manifest.json`` if present.

 Loaded independently of the production
 ``services/admin-dashboard-api/src/manifest.py`` loader (which
 applies a JSON Schema that does not yet whitelist newer extension
 fields like ``smoke_test_command``) so this invariant stays
 decoupled from upstream schema-evolution work. Returns ``None`` if
 the manifest is absent — the manifest is optional for invariant
 on its own; if present the manifest cross-check kicks in.
 """

    if not _MANIFEST_PATH.is_file():
        return None
    with _MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        document = json.load(fh)
    assert isinstance(document, dict), (
        f"services.manifest.json must parse to an object; "
        f"got {type(document).__name__}"
    )
    return document


_COMPOSE_DOC: dict[str, Any] = _load_compose()
_COMPOSE_SERVICES: dict[str, dict[str, Any]] = _COMPOSE_DOC["services"]
_MANIFEST_DOC: dict[str, Any] | None = _load_manifest()


# ---------------------------------------------------------------------------
# Helpers — profile-shape normalisation
# ---------------------------------------------------------------------------


def _profiles_of(service: dict[str, Any]) -> list[str] | None:
    """Return the ``profiles:`` list for a service, or ``None`` if absent.

 Compose YAML lets ``profiles`` be:

 * **Absent** — the service is included in the default ``up -d``.
 * **An explicit empty list** (``profiles: []``) — semantically
 identical to absent for default-profile-set inclusion.
 * **A non-empty list of strings** — the service is *only* included
 when one of those profile names is activated.

 We return ``None`` for the absent case and a list (possibly empty)
 otherwise so callers can distinguish "no directive" from "directive
 present but empty" in error messages — invariant collapses both
 into "default-profile-set member" so the distinction does not
 affect the equality check itself.
 """

    if "profiles" not in service:
        return None
    profiles = service["profiles"]
    assert isinstance(profiles, list), (
        f"'profiles' must be a list; got {type(profiles).__name__} "
        f"({profiles!r})"
    )
    for index, value in enumerate(profiles):
        assert isinstance(value, str), (
            f"profiles[{index}] must be a string; "
            f"got {type(value).__name__} ({value!r})"
        )
    return list(profiles)


def _is_in_default_profile_set(service: dict[str, Any]) -> bool:
    """Return True if Compose includes ``service`` in the profile-less ``up``.

 Compose semantics: a service is started by the default ``up -d``
 invocation iff its ``profiles:`` field is absent or empty. Any
 non-empty list gates it behind ``--profile <name>``.
 """

    profiles = _profiles_of(service)
    return profiles is None or profiles == []


def _default_profile_set(services: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Return the set of services ``docker compose up -d`` would start.

 Used by both the canonical-document property and the
 Hypothesis-driven mutation property below.
 """

    return frozenset(
        name for name, svc in services.items() if _is_in_default_profile_set(svc)
    )


# ---------------------------------------------------------------------------
# invariant (canonical) — Boot_Bundle equals the default-profile set
# ---------------------------------------------------------------------------


def test_compose_default_profile_set_equals_boot_bundle() -> None:
    """invariant — exactly the Boot_Bundle is in the default-profile set.



 A bare ``docker compose -f infra/docker-compose.yml up -d`` MUST
 start ``admin-dashboard-ui``, ``admin-dashboard-api``, ``postgres``,
 and ``vault`` — and nothing else. Equivalently: those four (and
 only those four) services have an absent or empty ``profiles:``
 directive.
 """

    actual = _default_profile_set(_COMPOSE_SERVICES)

    missing = BOOT_BUNDLE - actual
    extra = actual - BOOT_BUNDLE

    assert not missing, (
        "infra/docker-compose.yml: Boot_Bundle services missing from "
        f"the default-profile set (the operational rule, invariant); "
        f"missing={sorted(missing)!r}; "
        f"actual default-profile set={sorted(actual)!r}"
    )
    assert not extra, (
        "infra/docker-compose.yml: services present in the default-"
        "profile set that are NOT in the Boot_Bundle (the operational rule, "
        "invariant). A bare 'docker compose up -d' would start "
        "these alongside the Boot_Bundle and break the bootstrap "
        f"footprint contract; extra={sorted(extra)!r}; "
        f"Boot_Bundle={sorted(BOOT_BUNDLE)!r}"
    )

    assert actual == BOOT_BUNDLE, (
        "infra/docker-compose.yml: default-profile set MUST equal the "
        f"Boot_Bundle (the operational rule, invariant); "
        f"actual={sorted(actual)!r}, expected={sorted(BOOT_BUNDLE)!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors — Boot_Bundle membership (parametrize)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boot_service",
    sorted(BOOT_BUNDLE),
    ids=sorted(BOOT_BUNDLE),
)
def test_boot_bundle_anchor_default_profile_set(boot_service: str) -> None:
    """Concrete anchor — every Boot_Bundle service is in the default set.

 Each member of ``BOOT_BUNDLE`` MUST be declared in Compose AND
 its ``profiles:`` field MUST be absent or empty. Pinning each
 service individually catches regressions that drop a single member
 with a fully readable test id (e.g.
 ``test_boot_bundle_anchor_default_profile_set[postgres]``).


 """

    service = _COMPOSE_SERVICES.get(boot_service)
    assert service is not None, (
        f"Boot_Bundle service {boot_service!r} MUST be declared in "
        f"infra/docker-compose.yml (the operational rule, invariant); "
        f"declared services: {sorted(_COMPOSE_SERVICES.keys())!r}"
    )

    profiles = _profiles_of(service)
    assert profiles is None or profiles == [], (
        f"Boot_Bundle service {boot_service!r}: 'profiles:' MUST be "
        f"absent or empty so a bare 'docker compose up -d' starts it "
        f"(the operational rule, invariant); got profiles={profiles!r}"
    )


# ---------------------------------------------------------------------------
# Concrete anchor — every non-Boot_Bundle service IS profile-gated
# ---------------------------------------------------------------------------


_NON_BOOT_BUNDLE_COMPOSE_SERVICES: tuple[str, ...] = tuple(
    sorted(name for name in _COMPOSE_SERVICES if name not in BOOT_BUNDLE)
)


@pytest.mark.parametrize(
    "service_name",
    _NON_BOOT_BUNDLE_COMPOSE_SERVICES,
    ids=_NON_BOOT_BUNDLE_COMPOSE_SERVICES,
)
def test_non_boot_bundle_services_are_profile_gated(service_name: str) -> None:
    """Concrete anchor — every non-Boot_Bundle Compose service is profile-gated.

 The complement of the Boot_Bundle within the Compose ``services:``
 mapping MUST declare a non-empty ``profiles:`` list. Equivalently:
 no service outside the Boot_Bundle starts under a bare
 ``docker compose up -d``. This is the symmetric half of:func:`test_compose_default_profile_set_equals_boot_bundle` and
 pins each non-bundle service explicitly so a regression is caught
 with a readable test id.


 """

    service = _COMPOSE_SERVICES[service_name]
    profiles = _profiles_of(service)
    assert profiles is not None and profiles, (
        f"Compose service {service_name!r} is NOT in the Boot_Bundle "
        f"and therefore MUST declare a non-empty 'profiles:' list "
        f"(the operational rule, invariant); got profiles={profiles!r}. "
        f"Without a profile gate a bare 'docker compose up -d' would "
        f"start {service_name!r} alongside the Boot_Bundle and violate "
        f"the bootstrap footprint contract."
    )


# ---------------------------------------------------------------------------
# Manifest cross-reference — non-Boot_Bundle entries declare compose_profile
# ---------------------------------------------------------------------------


def _manifest_non_boot_bundle_entries() -> tuple[dict[str, Any], ...]:
    """Return manifest entries whose ``compose_service_name`` is non-Boot_Bundle.

 Returns an empty tuple when the manifest is absent or has no
 ``services:`` array; the manifest cross-check tests then become
 no-ops via ``pytest.skip`` semantics implemented inside the
 individual tests.
 """

    if _MANIFEST_DOC is None:
        return ()
    services = _MANIFEST_DOC.get("services") or []
    if not isinstance(services, list):
        return ()
    return tuple(
        entry
        for entry in services
        if isinstance(entry, dict)
        and entry.get("compose_service_name") not in BOOT_BUNDLE
    )


_MANIFEST_NON_BOOT_BUNDLE: tuple[dict[str, Any], ...] = (
    _manifest_non_boot_bundle_entries()
)


@pytest.mark.skipif(
    _MANIFEST_DOC is None,
    reason="config/services.manifest.json is not present",
)
@pytest.mark.parametrize(
    "entry",
    _MANIFEST_NON_BOOT_BUNDLE,
    ids=[
        str(e.get("name", f"entry-{i}"))
        for i, e in enumerate(_MANIFEST_NON_BOOT_BUNDLE)
    ],
)
def test_manifest_non_boot_bundle_compose_profile_is_non_empty_string(
    entry: dict[str, Any],
) -> None:
    """invariant (manifest side) — non-Boot_Bundle entries declare a compose_profile.


 for compose_profile lookup).

 For every Managed_Service entry whose ``compose_service_name`` is
 NOT in the Boot_Bundle, ``compose_profile`` MUST be a non-empty
 string. The dashboard's Compose_Manager uses this value as the
 argv to ``docker compose --profile <value> up -d``; a missing or
 empty profile would make the service unreachable from the Setup
 Wizard.

 Boot_Bundle entries (admin-dashboard-ui, admin-dashboard-api) are
 exempt: they are started by the default ``compose up`` and never
 activated through the Setup Wizard, so their ``compose_profile``
 value (if declared) is decorative only and is not checked here.
 """

    name = entry.get("name", "<unnamed>")
    profile = entry.get("compose_profile")
    assert isinstance(profile, str), (
        f"manifest entry {name!r}: 'compose_profile' MUST be a string "
        f"for non-Boot_Bundle services (the operational rule, invariant "
        f"20); got {type(profile).__name__} ({profile!r})"
    )
    assert profile, (
        f"manifest entry {name!r}: 'compose_profile' MUST be non-empty "
        f"for non-Boot_Bundle services (the operational rule, invariant "
        f"20); got an empty string. Without a profile name the Setup "
        f"Wizard cannot activate this service."
    )


# ---------------------------------------------------------------------------
# Hypothesis — mutation property
# ---------------------------------------------------------------------------


_ALL_COMPOSE_SERVICES: tuple[str, ...] = tuple(sorted(_COMPOSE_SERVICES.keys()))


def _candidate_with_extra_default_service(extra_name: str) -> dict[str, dict[str, Any]]:
    """Build a candidate ``services:`` map that adds one profile-less service.

 The new service ``extra_name`` is inserted with no ``profiles:``
 field, simulating the regression "developer adds a new always-on
 service to the stack". invariant says this MUST take the
 candidate out of conformance whenever ``extra_name`` is not in the
 Boot_Bundle.
 """

    candidate = {
        name: dict(svc) for name, svc in _COMPOSE_SERVICES.items()
    }
    candidate[extra_name] = {"image": "synthetic:latest"}
    return candidate


def _candidate_with_demoted_boot_bundle(demoted: str) -> dict[str, dict[str, Any]]:
    """Build a candidate that gates a Boot_Bundle service behind a profile.

 ``demoted`` is forced to declare ``profiles: ["synthetic"]`` so it
 drops out of the default-profile set. invariant says this MUST
 leave the candidate non-conformant.
 """

    candidate = {
        name: dict(svc) for name, svc in _COMPOSE_SERVICES.items()
    }
    candidate[demoted] = {**candidate[demoted], "profiles": ["synthetic"]}
    return candidate


def _candidate_with_promoted_profile_service(
    promoted: str,
) -> dict[str, dict[str, Any]]:
    """Build a candidate that strips the ``profiles:`` from a non-Boot_Bundle service.

 ``promoted`` is forced to drop its ``profiles:`` directive so it
 enters the default-profile set. invariant says this MUST leave
 the candidate non-conformant whenever ``promoted`` is not already
 in the Boot_Bundle.
 """

    candidate = {
        name: dict(svc) for name, svc in _COMPOSE_SERVICES.items()
    }
    promoted_copy = dict(candidate[promoted])
    promoted_copy.pop("profiles", None)
    candidate[promoted] = promoted_copy
    return candidate


# Mutation generator — Hypothesis picks one of three mutation kinds and
# a target service. ``one_of`` keeps the strategy a flat enum; we then
# branch in the test body based on ``kind`` to dispatch the right
# helper. ``max_examples=30`` is plenty given the candidate space is
# bounded by the size of the Compose document.
_MUTATION_KIND = st.sampled_from(("add_extra", "demote_boot", "promote_gated"))


@given(
    kind=_MUTATION_KIND,
    extra_name=st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"), max_codepoint=ord("z")
        ),
        min_size=4,
        max_size=20,
    ),
    target_index=st.integers(min_value=0, max_value=10_000),
)
@settings(
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property20_mutations_violate_boot_bundle_equality(
    kind: str, extra_name: str, target_index: int
) -> None:
    """invariant (mutation) — any structural mutation breaks the equality.



 For each Hypothesis example we synthesise a *candidate* Compose
 services map by applying one of three mutations and assert that
 the resulting default-profile set DOES NOT equal the Boot_Bundle
 (unless the mutation was a no-op, which we filter out
 deterministically). This codifies the contract "invariant is
 fragile — any drift breaks it".

 Mutations
 ---------
 * ``add_extra`` — inserts a new profile-less service named
 ``extra_name``. The mutation is a no-op when ``extra_name``
 collides with an existing Boot_Bundle service; we filter that
 out by requiring the synthesised name to be fresh.
 * ``demote_boot`` — strips a Boot_Bundle service out of the
 default-profile set by giving it a ``profiles:`` gate. The
 mutation is never a no-op (Boot_Bundle services have no profile
 gate by invariant).
 * ``promote_gated`` — drops the ``profiles:`` directive from a
 non-Boot_Bundle service so it enters the default-profile set.
 The mutation is never a no-op when the chosen target is in fact
 profile-gated in the canonical document, which we require.
 """

    canonical = _default_profile_set(_COMPOSE_SERVICES)
    # Sanity — the canonical document MUST satisfy invariant for
    # the mutation logic below to be meaningful. This is the same
    # check as the canonical test above; running it here too avoids
    # a misleading failure mode where Hypothesis flags the canonical
    # document via the mutation suite.
    assert canonical == BOOT_BUNDLE, (
        f"canonical compose document violates invariant "
        f"({canonical!r} != {BOOT_BUNDLE!r}); fix the canonical "
        f"failure first — see test_compose_default_profile_set_equals_boot_bundle"
    )

    if kind == "add_extra":
        # Skip name collisions — adding an "admin-dashboard-ui" again
        # would just shadow the original entry without changing the
        # default-profile set.
        if extra_name in _COMPOSE_SERVICES or extra_name in BOOT_BUNDLE:
            return
        candidate = _candidate_with_extra_default_service(extra_name)
        candidate_set = _default_profile_set(candidate)
        assert candidate_set != BOOT_BUNDLE, (
            f"invariant mutation 'add_extra({extra_name!r})' did NOT "
            f"break the Boot_Bundle equality; got "
            f"candidate_set={sorted(candidate_set)!r}, "
            f"BOOT_BUNDLE={sorted(BOOT_BUNDLE)!r}"
        )
        assert extra_name in candidate_set, (
            f"sanity: synthetic service {extra_name!r} should be in the "
            f"candidate default-profile set"
        )
        return

    if kind == "demote_boot":
        boot_targets = sorted(BOOT_BUNDLE)
        target = boot_targets[target_index % len(boot_targets)]
        candidate = _candidate_with_demoted_boot_bundle(target)
        candidate_set = _default_profile_set(candidate)
        assert target not in candidate_set, (
            f"sanity: demoted Boot_Bundle service {target!r} should "
            f"have left the default-profile set"
        )
        assert candidate_set != BOOT_BUNDLE, (
            f"invariant mutation 'demote_boot({target!r})' did NOT "
            f"break the Boot_Bundle equality; got "
            f"candidate_set={sorted(candidate_set)!r}, "
            f"BOOT_BUNDLE={sorted(BOOT_BUNDLE)!r}"
        )
        return

    # kind == "promote_gated"
    profile_gated_services = tuple(
        sorted(
            name
            for name, svc in _COMPOSE_SERVICES.items()
            if not _is_in_default_profile_set(svc)
        )
    )
    if not profile_gated_services:
        # Edge case — the Compose document somehow has no
        # profile-gated services. The earlier non_boot_bundle anchor
        # would already have failed; treat this Hypothesis example as
        # vacuous so we do not double-report.
        return
    target = profile_gated_services[target_index % len(profile_gated_services)]
    candidate = _candidate_with_promoted_profile_service(target)
    candidate_set = _default_profile_set(candidate)
    assert target in candidate_set, (
        f"sanity: promoted service {target!r} should now be in the "
        f"default-profile set"
    )
    assert candidate_set != BOOT_BUNDLE, (
        f"invariant mutation 'promote_gated({target!r})' did NOT "
        f"break the Boot_Bundle equality; got "
        f"candidate_set={sorted(candidate_set)!r}, "
        f"BOOT_BUNDLE={sorted(BOOT_BUNDLE)!r}"
    )
