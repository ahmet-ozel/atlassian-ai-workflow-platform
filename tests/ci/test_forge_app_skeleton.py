"""CI gate — Forge add-on skeleton (`platform-mimari-uyumluluk` task 8.4).

**Validates: Requirements 6.1, 6.2, 6.7**

The Forge add-on skeleton ships under ``platform/forge-app/`` so that
the ``FEATURE_FLAG_FORGE_ADDON_ENABLED`` opt-in path has something to
deploy. This test asserts the **structural** acceptance criteria from
the spec:

* R6.1 / R6.7 — ``forge-app/manifest.yml`` exists and is parseable YAML.
* R6.1        — the manifest has a non-empty top-level ``name`` (the
                Forge runtime requires a human-readable display name).
                The skeleton stores that name on the
                ``modules.jira:issueType[0].name`` entry; a top-level
                ``name`` field is also accepted for forward
                compatibility with future Forge manifest revisions.
* R6.2 / R6.7 — the manifest declares a ``modules.jira:issueType``
                key (this is the Forge module that materialises the
                "AI Bot Task" custom issue type with mandatory fields).

The test only inspects file shape — it never executes ``forge`` CLI or
talks to Atlassian. Schema validation beyond "key present, name
non-empty" is intentionally out of scope; deeper checks belong to the
Forge CLI itself during ``forge deploy``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_FORGE_APP_DIR = (
    Path(__file__).resolve().parent.parent.parent / "forge-app"
)
_MANIFEST_PATH = _FORGE_APP_DIR / "manifest.yml"


def _load_manifest() -> dict:
    """Parse ``forge-app/manifest.yml`` and return the mapping.

    The Forge manifest is always a single YAML document at the top
    level of the file; ``yaml.safe_load`` is sufficient and avoids
    arbitrary tag construction.
    """

    assert _MANIFEST_PATH.is_file(), (
        f"Missing {_MANIFEST_PATH} — Requirement 6.1 mandates the "
        "Forge skeleton ship a manifest.yml at platform/forge-app/."
    )
    raw = _MANIFEST_PATH.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), (
        "forge-app/manifest.yml must parse to a YAML mapping; got "
        f"{type(parsed).__name__}. Forge manifests are always a "
        "top-level mapping."
    )
    return parsed


def test_forge_manifest_file_exists() -> None:
    """R6.1 — the skeleton ships ``forge-app/manifest.yml``."""

    assert _MANIFEST_PATH.is_file(), (
        f"Missing {_MANIFEST_PATH}; Requirement 6.1 requires the "
        "Forge add-on skeleton to land at platform/forge-app/."
    )


def test_forge_manifest_parses_as_yaml() -> None:
    """R6.7 — manifest.yml must be valid YAML so ``forge deploy`` can read it."""

    try:
        _load_manifest()
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        pytest.fail(f"forge-app/manifest.yml is not valid YAML: {exc}")


def test_forge_manifest_has_non_empty_name() -> None:
    """R6.1 — Forge requires a human-readable display name.

    The skeleton stores the display name on the
    ``modules.jira:issueType[0].name`` entry (the issue type is the
    only Forge module the add-on ships). A top-level ``name`` is also
    accepted for forward compatibility.
    """

    manifest = _load_manifest()

    candidate_names: list[str] = []

    top_level_name = manifest.get("name")
    if isinstance(top_level_name, str):
        candidate_names.append(top_level_name)

    modules = manifest.get("modules")
    if isinstance(modules, dict):
        issue_types = modules.get("jira:issueType")
        if isinstance(issue_types, list):
            for entry in issue_types:
                if isinstance(entry, dict):
                    entry_name = entry.get("name")
                    if isinstance(entry_name, str):
                        candidate_names.append(entry_name)

    non_empty = [n for n in candidate_names if n.strip()]
    assert non_empty, (
        "forge-app/manifest.yml must declare a non-empty display "
        "name (top-level `name` or `modules.jira:issueType[*].name`); "
        "Requirement 6.1 requires a human-readable add-on identity."
    )


def test_forge_manifest_declares_jira_issue_type_module() -> None:
    """R6.2 / R6.7 — manifest must declare ``modules.jira:issueType``."""

    manifest = _load_manifest()

    modules = manifest.get("modules")
    assert isinstance(modules, dict), (
        "forge-app/manifest.yml must contain a `modules` mapping; "
        "Forge add-ons declare every capability there."
    )

    assert "jira:issueType" in modules, (
        "forge-app/manifest.yml must declare a `modules.jira:issueType` "
        "key; Requirement 6.2 requires the AI Bot Task custom issue "
        "type to be registered as a Forge module."
    )

    issue_types = modules["jira:issueType"]
    # Forge expects this module key to map to a list of issue type
    # definitions. We do not validate field-level structure here — that
    # belongs to ``forge deploy`` — but a non-list value is a clear
    # indication the skeleton has drifted from the Forge schema.
    assert isinstance(issue_types, list) and issue_types, (
        "`modules.jira:issueType` must be a non-empty list of issue "
        "type definitions; Requirement 6.2 ships at least the "
        "'AI Bot Task' issue type."
    )
