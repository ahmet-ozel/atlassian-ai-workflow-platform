"""Idempotent Jira Issue Template deploy script (task 12.2, R10.2).

Deploys the platform's standard Issue Template — issue type, custom
fields and screen scheme — to a target Jira tenant. The script reads
the desired template from a YAML / JSON configuration file (or an
in-memory dict for direct programmatic use) and aligns the live Jira
state with that desired state via per-entity ``read → diff → write``
operations.

Idempotency contract (Property 17 / R10.2)
------------------------------------------

Two consecutive invocations of :func:`deploy` with the same template
SHALL leave Jira in identical states:

1. The first run creates / updates every entity until live state
   matches the template.
2. The second run reads each entity, compares it to the template,
   and **issues no mutating call** when they match — the deploy is a
   strict no-op.
3. If an operator edits Jira between runs ("drift"), the next run
   heals the drift with a single mutation per drifted entity, and
   the run after that is again a no-op.

The script never deletes entities — Jira admin objects can be
referenced by historical issues, so the cleanup of removed fields /
issue types is a separate operator-driven workflow.

Dependency on ``atlassian_unified`` MCP
---------------------------------------

Production usage routes every Jira call through the
``atlassian_unified`` MCP service per MIMARI §1 Kural 1 (R1.2). The
``deploy`` function accepts any client that satisfies the structural
:class:`JiraTemplateClient` protocol — production wiring binds an
MCP-backed implementation; the property test
``platform/tests/property/test_jira_template_deploy.py`` injects a
hand-built fake.

CLI entry point
---------------

The module is also importable as a script:

.. code-block:: bash

    python -m scripts.deploy_jira_issue_template \\
        --template platform/config/jira_issue_template.yaml \\
        --base-url https://acme.atlassian.net

When invoked as a script the runtime client construction is
deferred to :func:`_make_default_client` which builds the MCP-backed
client from the standard env (``MCP_BASE_URL``, ``CLIENT_SOURCE``).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "JiraTemplateClient",
    "TemplateDeployResult",
    "deploy",
    "load_template",
    "main",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


class JiraTemplateClient(Protocol):
    """Structural interface the deploy script consumes.

    Mirrors the surface the property test fake exposes
    (:class:`MockJiraClient`). Any MCP-backed implementation that
    matches the same method signatures is a drop-in replacement.

    Each method returns / accepts a plain ``dict[str, Any]``; the
    deploy script does not depend on a specific Jira REST DTO
    library so it can be exercised against any backing store.
    """

    # -- issue type ---------------------------------------------------------
    def get_issue_type(self, name: str) -> dict[str, Any] | None: ...
    def create_issue_type(self, name: str, payload: dict[str, Any]) -> None: ...
    def update_issue_type(self, name: str, payload: dict[str, Any]) -> None: ...

    # -- custom field -------------------------------------------------------
    def get_field(self, name: str) -> dict[str, Any] | None: ...
    def create_field(self, name: str, payload: dict[str, Any]) -> None: ...
    def update_field(self, name: str, payload: dict[str, Any]) -> None: ...

    # -- screen scheme ------------------------------------------------------
    def get_screen_scheme(self, name: str) -> dict[str, Any] | None: ...
    def create_screen_scheme(self, name: str, payload: dict[str, Any]) -> None: ...
    def update_screen_scheme(self, name: str, payload: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateDeployResult:
    """Summary of mutations performed by a single :func:`deploy` call.

    Attributes:
        issue_type_changes: Number of create/update calls issued for
            the issue type entity (always 0 or 1).
        field_changes: Number of mutating calls issued across all
            custom fields.
        screen_scheme_changes: Mutations on the screen scheme
            (always 0 or 1).
    """

    issue_type_changes: int
    field_changes: int
    screen_scheme_changes: int

    @property
    def total_mutations(self) -> int:
        """Sum of all mutations — useful for "is this a no-op?" checks."""

        return (
            self.issue_type_changes
            + self.field_changes
            + self.screen_scheme_changes
        )


# ---------------------------------------------------------------------------
# Diff primitives
# ---------------------------------------------------------------------------


def _entity_unchanged(current: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    """Return ``True`` iff ``current`` already matches ``desired``.

    The comparison is a deep dict equality. Both sides must be plain
    dictionaries (lists / scalars compare with ``==`` semantics
    inside the dict). The function is the **single source of truth**
    for "do we need to mutate this entity?" — every per-entity
    branch in :func:`deploy` consults it so the script's
    idempotency guarantees rest on a single comparison.
    """

    return dict(current) == dict(desired)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def deploy(
    client: JiraTemplateClient,
    template: Mapping[str, Any],
) -> TemplateDeployResult:
    """Align the live Jira state with *template*.

    Args:
        client: An object satisfying :class:`JiraTemplateClient`.
        template: A mapping with the keys ``"issue_type"``,
            ``"fields"``, ``"screen_scheme"``. Field shapes match
            what each ``create_*`` / ``update_*`` method on the
            client accepts.

    Returns:
        :class:`TemplateDeployResult` summarising the mutations.
        ``result.total_mutations == 0`` indicates a strict no-op
        (Property 17 / R10.2).
    """

    issue_type_changes = _deploy_issue_type(client, template["issue_type"])
    field_changes = _deploy_fields(client, template.get("fields") or [])
    screen_scheme_changes = _deploy_screen_scheme(
        client, template["screen_scheme"]
    )

    result = TemplateDeployResult(
        issue_type_changes=issue_type_changes,
        field_changes=field_changes,
        screen_scheme_changes=screen_scheme_changes,
    )
    _LOG.info(
        "deploy.done issue_type=%d fields=%d screen_scheme=%d total=%d",
        result.issue_type_changes,
        result.field_changes,
        result.screen_scheme_changes,
        result.total_mutations,
    )
    return result


def _deploy_issue_type(
    client: JiraTemplateClient, desired: Mapping[str, Any]
) -> int:
    """Apply the ``issue_type`` portion of the template; return mutation count."""

    name = desired["name"]
    current = client.get_issue_type(name)
    desired_dict = dict(desired)
    if current is None:
        client.create_issue_type(name, desired_dict)
        return 1
    if _entity_unchanged(current, desired_dict):
        return 0
    client.update_issue_type(name, desired_dict)
    return 1


def _deploy_fields(
    client: JiraTemplateClient, desired_fields: Sequence[Mapping[str, Any]]
) -> int:
    """Apply each declared custom field; return total mutation count."""

    mutations = 0
    for field_payload in desired_fields:
        name = field_payload["name"]
        desired_dict = dict(field_payload)
        current = client.get_field(name)
        if current is None:
            client.create_field(name, desired_dict)
            mutations += 1
            continue
        if _entity_unchanged(current, desired_dict):
            continue
        client.update_field(name, desired_dict)
        mutations += 1
    return mutations


def _deploy_screen_scheme(
    client: JiraTemplateClient, desired: Mapping[str, Any]
) -> int:
    """Apply the screen scheme portion of the template."""

    name = desired["name"]
    current = client.get_screen_scheme(name)
    desired_dict = dict(desired)
    if current is None:
        client.create_screen_scheme(name, desired_dict)
        return 1
    if _entity_unchanged(current, desired_dict):
        return 0
    client.update_screen_scheme(name, desired_dict)
    return 1


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


def load_template(path: str) -> dict[str, Any]:
    """Load a template definition from JSON or YAML.

    The deploy script accepts either format so the same artifact can
    be checked in alongside the Compose / config files. YAML support
    is optional — the function falls back to JSON parsing when
    ``PyYAML`` is not installed.
    """

    with open(path, encoding="utf-8") as fh:
        raw = fh.read()

    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"PyYAML required to load {path!r}; install it or "
                "re-encode the template as JSON"
            ) from exc
        loaded = yaml.safe_load(raw)
    else:
        loaded = json.loads(raw)

    if not isinstance(loaded, dict):
        raise ValueError(
            f"template file {path!r} must contain a top-level mapping"
        )
    return loaded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _make_default_client(base_url: str) -> JiraTemplateClient:
    """Construct the production MCP-backed client.

    Stub for now — the full MCP-backed Jira admin client lands in
    Spec 2 alongside the rest of the activity surface. The CLI path
    is exposed today so an operator can run the deploy step against
    a future client without changing the script.
    """

    raise RuntimeError(
        "production MCP-backed Jira admin client not wired up yet; "
        "import deploy() from your own runtime which constructs the "
        "client and call it directly"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``python -m scripts.deploy_jira_issue_template``."""

    parser = argparse.ArgumentParser(
        prog="deploy_jira_issue_template",
        description="Idempotently deploy the platform's Jira Issue Template",
    )
    parser.add_argument(
        "--template",
        required=True,
        help="Path to the template JSON / YAML file",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ATLASSIAN_BASE_URL", ""),
        help="Atlassian site base URL (e.g. https://acme.atlassian.net)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    template = load_template(args.template)
    client = _make_default_client(args.base_url)
    result = deploy(client, template)
    print(
        f"deployed: issue_type={result.issue_type_changes} "
        f"fields={result.field_changes} "
        f"screen_scheme={result.screen_scheme_changes} "
        f"total={result.total_mutations}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
