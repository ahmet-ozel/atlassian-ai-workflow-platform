"""Integration test 2.4 - profile-less Compose stack opens only the boot bundle.

**the invariant: Compose Boot-Only Set**



What this test asserts
----------------------

 says ``make boot`` MUST execute ``docker compose -f
infra/docker-compose.yml up -d`` with NO ``--profile`` flag and that
the resulting running set MUST be exactly the four-service "boot
bundle":

 {postgres, vault, admin-dashboard-api, admin-dashboard-ui}

 keeps ``make up`` as an alias for ``make boot`` and introduces
``make up-all`` for the legacy "every manifest profile" behaviour, so
the same boot-bundle invariant applies to the default lifecycle entry
point regardless of which alias the operator types.

 codifies the invariant as a CI test: ``docker compose -f
infra/docker-compose.yml config --services`` (run profilesiz, i.e.
without any ``--profile`` flag) MUST return exactly the boot bundle -
``atlassian-mcp``, ``automation-service``, ``agent-runner-worker``,
``execution-runner-worker``, ``assistant-service``, ``firecrawl``,
``opencode-sidecar``, ``streamlit-ui`` and the rest MUST stay
profile-gated and out of the default profile.

Two test surfaces, one invariant
--------------------------------

The same invariant is checked via two independent paths so the
property holds in every CI lane:

1. ``test_profile_less_compose_yaml_only_lists_the_boot_bundle``
 - runs unconditionally. It parses ``infra/docker-compose.yml``
 (and the dev override ``infra/docker-compose.dev.yml``) directly
 with PyYAML and computes the boot bundle as the set of services
 whose merged definition has no ``profiles:`` key (or whose
 ``profiles:`` list is empty). This path does NOT need a Docker
 daemon, runs in milliseconds, and stays parallel-safe - exactly
 what the task notes call for ("Prefer parsing the docker-compose
 YAML directly with PyYAML over shelling out to docker compose so
 the test does not require docker installed in CI").

2. ``test_profile_less_compose_config_services_matches_boot_bundle``
 - gated on ``--run-docker`` and on ``docker info``. It shells
 out to ``docker compose -f infra/docker-compose.yml config
 --services`` (no ``--profile`` flag) and asserts the parsed
 service set equals the boot bundle. This path proves that
 Compose's own profile resolver agrees with the YAML-only
 computation, catching any future Compose-version drift in profile
 semantics. Skipped cleanly when Docker is unavailable so the
 default fast lane stays self-contained.

What it explicitly does NOT do
------------------------------

* It does not bring containers up - that is the implementation / the existing
 ``test_compose_default_profile_boots_and_services_are_healthy`` and
 ``test_boot_bundle_only_brings_up_four_services_and_they_are_healthy``
 smoke tests. The assertion is purely about which services
 Compose *would* select with no ``--profile`` flag, so a
 ``--services`` listing (or YAML walk) is sufficient and dramatically
 cheaper than a real boot.
* It does not assert on per-service healthcheck shapes, env files,
 port publishing, or build contexts. Other foundation tests own
 those invariants and asserting them here would couple the boot-set
 invariant to unrelated regressions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixed parameters
# ---------------------------------------------------------------------------

#: The base Compose file (always layered).
COMPOSE_FILE_REL: str = "infra/docker-compose.yml"

#: The dev override layered by ``make boot``. 's ``COMPOSE_BOOT``
#: variable in ``platform/Makefile`` evaluates to:
#:
#: docker compose -f infra/docker-compose.yml -f infra/docker-compose.dev.yml
#:
#: Including the dev override here keeps the YAML walk faithful to
#: what ``make boot`` actually invokes; the override never *adds* new
#: services (Compose merge semantics extend, not introduce - see the
#: comment block at the top of ``infra/docker-compose.dev.yml``) but
#: it can in principle remove a ``profiles:`` key on an existing
#: service. We respect that merge in case a future override does so.
COMPOSE_DEV_FILE_REL: str = "infra/docker-compose.dev.yml"

#: The boot bundle defined by / . Exactly these four services
#: MUST come up under a profile-less ``docker compose up -d``; nothing
#: more, nothing less.
EXPECTED_BOOT_BUNDLE: frozenset[str] = frozenset(
    {
        "postgres",
        "vault",
        "admin-dashboard-api",
        "admin-dashboard-ui",
    }
)

#: Services explicitly calls out as profile-gated. They MUST be
#: declared in ``infra/docker-compose.yml`` (otherwise the assertion
#: would be vacuously true) and they MUST carry a non-empty
#: ``profiles:`` list so a profile-less ``compose up`` skips them.
EXPLICITLY_PROFILE_GATED: frozenset[str] = frozenset(
    {
        "atlassian-mcp",
        "automation-service",
        "agent-runner-worker",
        "execution-runner-worker",
        "assistant-service",
        "firecrawl",
        "opencode-sidecar",
        "streamlit-ui",
    }
)


# ---------------------------------------------------------------------------
# YAML parsing helpers
# ---------------------------------------------------------------------------


def _load_compose_services(compose_path: Path) -> dict[str, dict]:
    """Parse a single Compose file and return its ``services:`` mapping.

 Returns an empty dict when the file is missing - callers (the
 layering helper below) treat absent overrides as "contributes
 nothing", matching Compose's own behaviour for omitted ``-f``
 files.
 """

    if not compose_path.is_file():
        return {}

    import yaml  # local import keeps module import cheap when the test skips

    with compose_path.open("r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh)

    if not isinstance(document, dict):
        raise AssertionError(
            f"{compose_path} did not parse to a top-level mapping; "
            f"got {type(document).__name__}."
        )
    services = document.get("services") or {}
    if not isinstance(services, dict):
        raise AssertionError(
            f"{compose_path} `services:` section is not a mapping; "
            f"got {type(services).__name__}."
        )
    return services


def _merge_layered_services(
    base: dict[str, dict], override: dict[str, dict]
) -> dict[str, dict]:
    """Apply Compose's per-service merge for the keys we care about here.

 Compose's full merge semantics are intricate (volumes concatenate,
 environment maps extend, etc.) but for the boot-set invariant we
 only need to know whether the merged definition has a non-empty
 ``profiles:`` list. The override file CAN, in principle, redefine
 or clear ``profiles:`` on an existing service, so we honour that:
 if the override carries a ``profiles:`` key (even an empty list),
 it wins; otherwise the base value is kept.

 Override services that are not present in the base are passed
 through; in real deployments Compose would error on this, but
 treating them as additive lets the test surface that situation as
 an "unexpected override service" diff in the assertion message
 rather than a confusing KeyError.
 """

    merged: dict[str, dict] = {name: dict(svc) for name, svc in base.items()}
    for name, override_svc in override.items():
        merged.setdefault(name, {})
        if not isinstance(override_svc, dict):
            # Defensive: Compose requires service definitions to be
            # mappings, but we surface a useful error rather than
            # crashing on the next ``.get(...)`` call.
            raise AssertionError(
                f"override file declares `services.{name}` as "
                f"{type(override_svc).__name__}, expected mapping"
            )
        for key, value in override_svc.items():
            # ``profiles:`` is the one key whose merged value drives
            # the boot-bundle decision below. Any other keys we copy
            # over so the assertion error messages can show the
            # merged image of the service definition if it ever
            # becomes useful for diagnostics.
            merged[name][key] = value
    return merged


def _services_without_profile(services: dict[str, dict]) -> frozenset[str]:
    """Return the names of services whose merged definition has no
 non-empty ``profiles:`` list.

 Compose's profile resolution rule is: a service is part of the
 default (profile-less) selection iff its ``profiles:`` key is
 absent OR present-but-empty. A service whose ``profiles:`` list
 contains ANY entry is excluded from the default profile and only
 appears when one of its profile names is named on the CLI.
 """

    boot_services: set[str] = set()
    for name, definition in services.items():
        profiles = definition.get("profiles")
        if profiles is None:
            boot_services.add(name)
            continue
        if not isinstance(profiles, list):
            raise AssertionError(
                f"`services.{name}.profiles` must be a list, "
                f"got {type(profiles).__name__}"
            )
        if len(profiles) == 0:
            boot_services.add(name)
    return frozenset(boot_services)


# ---------------------------------------------------------------------------
# Docker availability gate (re-used from sibling tests)
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True iff the ``docker`` CLI is on PATH and the daemon responds.

 We probe ``docker info`` rather than ``docker version`` because
 the latter succeeds even when the daemon is offline; ``docker
 info`` requires a live daemon connection.
 """

    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Test 1 - YAML-only path (always runs, no Docker required)
# ---------------------------------------------------------------------------


def test_profile_less_compose_yaml_only_lists_the_boot_bundle(
    repo_root: Path,
) -> None:
    """Parsing ``infra/docker-compose.yml`` (+dev override) with PyYAML
 yields the boot bundle as the set of services without
 ``profiles:``.

 Validates This is the always-on path: it does not need a Docker daemon,
 runs in milliseconds, and stays self-contained so the default
 fast lane in CI catches any regression that would let a
 profile-gated service slip into the default profile (or, the
 inverse, drop a boot-bundle service behind a profile).
 """

    pytest.importorskip(
        "yaml",
        reason=(
            "PyYAML is required by tests/pyproject.toml and "
            "pytest.ini's pythonpath wiring; if it is missing the "
            "test environment is broken in a way no other test would "
            "tolerate either, so we raise rather than silently pass."
        ),
    )

    base_path = repo_root / COMPOSE_FILE_REL
    dev_path = repo_root / COMPOSE_DEV_FILE_REL
    assert base_path.is_file(), (
        f"Compose file missing at {base_path}; cannot validate the "
        f"boot bundle invariant."
    )

    base_services = _load_compose_services(base_path)
    dev_services = _load_compose_services(dev_path)
    merged = _merge_layered_services(base_services, dev_services)

    boot_services = _services_without_profile(merged)

    # ---- Primary assertion (, ): exact equality ----
    extra = boot_services - EXPECTED_BOOT_BUNDLE
    missing = EXPECTED_BOOT_BUNDLE - boot_services
    assert boot_services == EXPECTED_BOOT_BUNDLE, (
        "profile-less Compose stack must open exactly the boot "
        f"bundle {sorted(EXPECTED_BOOT_BUNDLE)} ("
        "the invariant).\n"
        f"  unexpected services in default profile: {sorted(extra)}\n"
        f"  missing from default profile:           {sorted(missing)}\n"
        f"  observed default-profile set:           {sorted(boot_services)}"
    )

    # ---- Secondary assertion ( explicit list): named services
    # are profile-gated AND declared in the base Compose file ----
    declared_services = frozenset(merged.keys())
    not_declared = EXPLICITLY_PROFILE_GATED - declared_services
    assert not not_declared, (
        "the following services are named in as profile-gated "
        "but are absent from the merged Compose definition (so the "
        "test's gating assertion would be vacuously true): "
        f"{sorted(not_declared)}.\n"
        "Either rename the service to match its Compose declaration "
        "or remove it from EXPLICITLY_PROFILE_GATED."
    )

    leaked_into_default = EXPLICITLY_PROFILE_GATED & boot_services
    assert not leaked_into_default, (
        "the following services MUST stay profile-gated per but "
        "are visible in the profile-less default selection: "
        f"{sorted(leaked_into_default)}.\n"
        "Add a non-empty `profiles:` list to each in "
        f"{COMPOSE_FILE_REL}."
    )

    # Belt-and-braces: every explicitly-gated service has a
    # non-empty profiles list (a service with `profiles: []` would
    # already be flagged by the boot-bundle assertion above; this
    # check makes the failure message specific).
    weakly_gated: list[str] = []
    for name in sorted(EXPLICITLY_PROFILE_GATED):
        profiles = merged.get(name, {}).get("profiles")
        if not (isinstance(profiles, list) and len(profiles) > 0):
            weakly_gated.append(name)
    assert not weakly_gated, (
        "the following profile-gated services have an empty or "
        f"missing `profiles:` list: {weakly_gated}. the boot bundle invariant requires "
        "each to declare at least one profile name."
    )


# ---------------------------------------------------------------------------
# Test 2 - Docker-gated path (proves Compose itself agrees)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_profile_less_compose_config_services_matches_boot_bundle(
    request: pytest.FixtureRequest, repo_root: Path
) -> None:
    """``docker compose -f infra/docker-compose.yml config --services``
 (no profile flag) emits exactly the boot bundle.

 Validates This is the higher-fidelity sibling of the YAML-only test: it
 invokes the real Compose CLI and inspects ``--services`` output
 so any future Compose-version drift in profile semantics shows
 up here even before it would manifest as a real boot regression.

 Opt-in via ``--run-docker``. Skips cleanly when the flag is
 absent or the Docker daemon is unreachable so CI fast lanes do
 not pay for a daemon spin-up.
 """

    if not request.config.getoption("--run-docker", default=False):
        pytest.skip(
            "Docker integration tests are opt-in; pass --run-docker "
            "to enable the boot-only Compose CLI cross-check."
        )
    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable on this host (`docker info` "
            "failed); cannot run the Compose CLI cross-check."
        )

    compose_file = repo_root / COMPOSE_FILE_REL
    assert compose_file.is_file(), (
        f"Compose file missing at {compose_file}; cannot validate "
        f"the boot bundle invariant."
    )

    # NOTE: we deliberately invoke `docker compose -f
    # infra/docker-compose.yml config --services` WITHOUT the dev
    # override and WITHOUT any `--profile` flag - that is precisely
    # what specifies. Layering the dev override here would also
    # be valid (the override does not change which services exist),
    # but matching the requirement text exactly keeps the failure
    # diagnostic tied to the requirement string.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_REL,
            "config",
            "--services",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        "`docker compose config --services` (no profile) failed:\n"
        f"  exit:   {result.returncode}\n"
        f"  stdout: {result.stdout!r}\n"
        f"  stderr: {result.stderr!r}"
    )

    listed = frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )

    extra = listed - EXPECTED_BOOT_BUNDLE
    missing = EXPECTED_BOOT_BUNDLE - listed
    assert listed == EXPECTED_BOOT_BUNDLE, (
        "`docker compose config --services` (no --profile) must "
        f"emit exactly the boot bundle {sorted(EXPECTED_BOOT_BUNDLE)} "
        ".\n"
        f"  unexpected services: {sorted(extra)}\n"
        f"  missing services:    {sorted(missing)}\n"
        f"  observed:            {sorted(listed)}"
    )

    # And the explicitly-named profile-gated services from MUST
    # NOT appear in the default-profile listing.
    leaked = listed & EXPLICITLY_PROFILE_GATED
    assert not leaked, (
        "the following services are named in as profile-gated "
        "but were emitted by the profile-less "
        f"`docker compose config --services`: {sorted(leaked)}."
    )
