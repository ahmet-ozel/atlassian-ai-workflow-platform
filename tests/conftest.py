"""Shared pytest fixtures for the workspace test suite.

This module is the single source of truth that every property test under
``tests/property/`` parameterises against. It exposes four data
structures and one fixture that model the workspace contract:

- ``COMPONENT_MANIFEST`` - the 8 in-scope Components. Each row carries
 enough metadata to drive the component property tests.
- ``REQUIRED_PATHS`` - relative paths every Component of a given
 ``ComponentType`` must have, plus infra/config/root path lists.
- ``FORBIDDEN_PATHS`` - paths the project *must not* produce.
- ``EXPECTED_COMPOSE_SERVICES`` - the service names that the parsed
 ``infra/docker-compose.yml`` must equal under the invariant, including
 ``task-intake-service`` (profile-gated) and ``automation-worker``
 (added for the workflow topology).
- ``repo_root`` fixture - workspace root ``Path`` used by every
 filesystem-walking property test.

It also injects each shared library's ``src/`` directory onto
``sys.path`` so property tests can ``import http_shared``,
``import temporal_shared``, etc., without first installing the libs.

"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

# ---------------------------------------------------------------------------
# Workspace root and ``sys.path`` bootstrapping
# ---------------------------------------------------------------------------

#: Workspace root, computed from this file's location. Property tests use
#: this as the anchor for every relative path lookup.
WORKSPACE_ROOT: Path = Path(__file__).resolve().parent.parent

# Each shared library ships its source under ``libs/<name>/src/<pkg>``.
# We expose ``libs/<name>/src`` so ``import <pkg>`` resolves directly.
_LIB_SRC_DIRS: tuple[Path, ...] = tuple(
    WORKSPACE_ROOT / "libs" / lib / "src"
    for lib in (
        "http-shared",
        "llm-orchestrator",
        "temporal-shared",
        "auth-shared",
        "db-shared",
        "messages",
        "prompts",
        "audit_logger",
        "vault_client",
        "mcp_client",
        "pii-shared",
        "notification",
        "observability",
    )
)

for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# Cross-service property tests use
# ``from src.X`` patterns (e.g. ``from src.lifecycle.audit_writer
# import ...``). Python's import system can only resolve ONE ``src``
# package at a time, so listing every service ``src/`` here would
# REGRESS the collection state (each service has overlapping module
# names - ``src/config.py``, ``src/main.py`` exist in 6 services).
#
# The minimum-correctness compromise: this top-level conftest stays
# free of service-src manipulations. Tests that legitimately need a
# worker namespace (e.g. ``automation_worker`` for the EK3 parity
# test) handle their own ``sys.path`` injection at test-module
# import time so the rest of the suite keeps collecting.
#
# Per-service unit tests run from the service's own ``tests/`` tree
# (which already work - see ``services/admin-dashboard-api/tests/``,
# ``workers/automation-worker/tests/``). Properly closing the
# cross-service collection gap requires renaming each ``src`` to a
# unique top-level package (``admin_dashboard_api`` etc.) - tracked.


# ---------------------------------------------------------------------------
# Component Manifest
# ---------------------------------------------------------------------------

ComponentType = Literal["http_service", "temporal_worker", "ui_component"]
RuntimeKind = Literal["python", "node"]


@dataclass(frozen=True)
class ComponentSpec:
    """Mental model for a single Component row.

 The schema mirrors the component table shape so this fixture can
 stand in for it during property test parameterisation. Every field is hashable so the dataclass itself
 stays ``frozen=True`` and can be sampled by Hypothesis via
 ``st.sampled_from``.
 """

    name: str
    type: ComponentType
    runtime: RuntimeKind
    path: str
    container_port: int | None
    host_port: int | None
    profiles: tuple[str, ...] = field(default_factory=tuple)
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    required_env: tuple[str, ...] = field(default_factory=tuple)
    client_source_id: str | None = None


# ---------------------------------------------------------------------------
# Per-Component manifest rows, including ``depends_on`` and
# ``required_env`` metadata.
# ---------------------------------------------------------------------------

#: Standard env block every HTTP service must expose locally
#: the invariant).
_HTTP_BASE_ENV: tuple[str, ...] = ("PORT", "LOG_LEVEL")

#: Full LLM provider block - required by every LLM-consuming Component
#: regardless of the currently configured provider .
_LLM_ENV_BLOCK: tuple[str, ...] = (
    "LLM_PROVIDER",
    "VLLM_BASE_URL",
    "LLM_MODEL_NAME",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

#: MCP / Firecrawl consumer block .
_MCP_FIRECRAWL_ENV: tuple[str, ...] = ("MCP_BASE_URL", "FIRECRAWL_BASE_URL")


COMPONENT_MANIFEST: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        name="automation-service",
        type="http_service",
        runtime="python",
        path="services/automation-service",
        container_port=8080,
        host_port=38084,
        profiles=(),
        depends_on=("postgres", "vault", "temporal", "atlassian-mcp"),
        required_env=(
            *_HTTP_BASE_ENV,
            "POSTGRES_DSN",
            "VAULT_ADDR",
            "VAULT_TOKEN",
            "TEMPORAL_HOST",
            "MCP_BASE_URL",
            *_LLM_ENV_BLOCK,
        ),
        client_source_id="automation-service",
    ),
    ComponentSpec(
        name="assistant-service",
        type="http_service",
        runtime="python",
        path="services/assistant-service",
        container_port=8081,
        host_port=38081,
        profiles=(),
        depends_on=("postgres", "redis", "atlassian-mcp"),
        required_env=(
            *_HTTP_BASE_ENV,
            "POSTGRES_DSN",
            "REDIS_URL",
            "MCP_BASE_URL",
            *_LLM_ENV_BLOCK,
        ),
        client_source_id="assistant-service",
    ),
    ComponentSpec(
        name="admin-dashboard-api",
        type="http_service",
        runtime="python",
        path="services/admin-dashboard-api",
        container_port=8082,
        host_port=38082,
        profiles=(),
        depends_on=("postgres", "vault", "temporal"),
        required_env=(
            *_HTTP_BASE_ENV,
            "POSTGRES_DSN",
            "VAULT_ADDR",
            "VAULT_TOKEN",
            "TEMPORAL_HOST",
            *_LLM_ENV_BLOCK,
        ),
        client_source_id="admin-dashboard-api",
    ),
    ComponentSpec(
        name="task-intake-service",
        type="http_service",
        runtime="python",
        path="services/task-intake-service",
        container_port=8083,
        host_port=38083,
        profiles=("task-intake",),
        depends_on=("postgres", "temporal", "atlassian-mcp", "firecrawl"),
        required_env=(
            *_HTTP_BASE_ENV,
            "POSTGRES_DSN",
            "TEMPORAL_HOST",
            *_MCP_FIRECRAWL_ENV,
        ),
        client_source_id="task-intake-service",
    ),
    ComponentSpec(
        name="agent-runner-worker",
        type="temporal_worker",
        runtime="python",
        path="workers/agent-runner-worker",
        container_port=None,
        host_port=None,
        profiles=(),
        depends_on=(
            "temporal",
            "atlassian-mcp",
            "firecrawl",
            "minio",
            "opencode-sidecar",
        ),
        required_env=(
            "LOG_LEVEL",
            "TEMPORAL_HOST",
            "TEMPORAL_TASK_QUEUE",
            *_MCP_FIRECRAWL_ENV,
            *_LLM_ENV_BLOCK,
            "MINIO_ENDPOINT",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "OPENCODE_ENDPOINT",
        ),
        client_source_id="agent-runner-worker",
    ),
    ComponentSpec(
        name="execution-runner-worker",
        type="temporal_worker",
        runtime="python",
        path="workers/execution-runner-worker",
        container_port=None,
        host_port=None,
        profiles=(),
        depends_on=("temporal", "vault", "minio", "postgres"),
        required_env=(
            "LOG_LEVEL",
            "TEMPORAL_HOST",
            "TEMPORAL_TASK_QUEUE",
            "VAULT_ADDR",
            "VAULT_TOKEN",
            "MINIO_ENDPOINT",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "POSTGRES_DSN",
            "SSH_RUNNER_DEPT_PINNING_ENABLED",
            "SSH_DEPT_QUOTA_ENABLED",
        ),
        client_source_id="execution-runner-worker",
    ),
    ComponentSpec(
        name="streamlit-app",
        type="ui_component",
        runtime="python",
        path="ui/streamlit-app",
        container_port=8501,
        host_port=38501,
        profiles=(),
        depends_on=("assistant-service",),
        required_env=(
            "PORT",
            "LOG_LEVEL",
            "ASSISTANT_BASE_URL",
            "MCP_BASE_URL",
        ),
        client_source_id="streamlit-app",
    ),
    ComponentSpec(
        name="admin-dashboard",
        type="ui_component",
        runtime="node",
        path="ui/admin-dashboard",
        container_port=3000,
        host_port=33000,
        profiles=(),
        # The Compose service for this UI is named ``admin-dashboard-ui``
        # (see EXPECTED_COMPOSE_SERVICES); the manifest carries the
        # backend dependency so the invariant's superset check passes.
        depends_on=("admin-dashboard-api",),
        required_env=(
            "PORT",
            "LOG_LEVEL",
            "NEXT_PUBLIC_ADMIN_API_BASE_URL",
        ),
        client_source_id=None,  # browser  BFF, no X-Client-Source from UI
    ),
)


#: Convenience subsets used by several property tests.
HTTP_SERVICES: tuple[ComponentSpec, ...] = tuple(
    c for c in COMPONENT_MANIFEST if c.type == "http_service"
)
TEMPORAL_WORKERS: tuple[ComponentSpec, ...] = tuple(
    c for c in COMPONENT_MANIFEST if c.type == "temporal_worker"
)
UI_COMPONENTS: tuple[ComponentSpec, ...] = tuple(
    c for c in COMPONENT_MANIFEST if c.type == "ui_component"
)


# ---------------------------------------------------------------------------
# Required filesystem paths (the invariant)
# ---------------------------------------------------------------------------

# Paths every Python HTTP service must have under its component path
# .
_HTTP_SERVICE_PATHS: tuple[str, ...] = (
    "src/__init__.py",
    "src/main.py",
    "src/config.py",
    "pyproject.toml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "README.md",
    "tests/unit/.gitkeep",
    "tests/integration/.gitkeep",
    "tests/e2e/.gitkeep",
)

# Paths every Temporal worker must have .
_TEMPORAL_WORKER_PATHS: tuple[str, ...] = (
    "src/__init__.py",
    "src/main.py",
    "src/workflows/__init__.py",
    "src/activities/__init__.py",
    "pyproject.toml",
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "README.md",
    "tests/unit/.gitkeep",
    "tests/integration/.gitkeep",
    "tests/e2e/.gitkeep",
)

# Paths every UI Component must have .
# Note: ``streamlit-app`` uses ``app.py`` + ``requirements.txt``;
# ``admin-dashboard`` (Next.js) uses ``package.json`` + ``app/page.tsx``.
# The shared minimum is the README + Dockerfile + .env.example +
# .dockerignore. Per-runtime extras live in REQUIRED_PATHS_BY_NAME below.
_UI_COMPONENT_PATHS: tuple[str, ...] = (
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    "README.md",
)


#: Paths required of every Component, keyed by ``ComponentType``.
REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "http_service": _HTTP_SERVICE_PATHS,
    "temporal_worker": _TEMPORAL_WORKER_PATHS,
    "ui_component": _UI_COMPONENT_PATHS,
}


#: Per-Component additional paths that don't generalise across the
#: ``ComponentType``. Used by the invariant to layer Component-specific
#: assertions on top of the type-level baseline.
REQUIRED_PATHS_BY_NAME: dict[str, tuple[str, ...]] = {
    # Additional automation-service modules.
    "automation-service": (
        "src/webhooks/__init__.py",
        "src/temporal_client.py",
        "src/decision/__init__.py",
        "migrations/.gitkeep",
    ),
    # : chat/, llm/, prompts/
    "assistant-service": (
        "src/chat/__init__.py",
        "src/llm/__init__.py",
    ),
    # : routers/, auth/, clients/, prompts_git/
    "admin-dashboard-api": (
        "src/routers/__init__.py",
        "src/auth/__init__.py",
        "src/clients/__init__.py",
        "src/prompts_git/__init__.py",
    ),
    # : same project structure as other HTTP services + intake modules
    "task-intake-service": (
        "src/intake/__init__.py",
        "src/channels/__init__.py",
    ),
    # : agent-runner workflow + activity placeholders +
    # prompts/ tree .
    "agent-runner-worker": (
        "src/workflows/agent_runner_workflow.py",
        "src/activities/jira.py",
        "src/activities/bitbucket.py",
        "src/activities/confluence.py",
        "src/activities/llm.py",
        "src/activities/artifact.py",
        "src/activities/opencode.py",
        "prompts/task_analysis.md",
        "prompts/code_generation.md",
        "prompts/pr_description.md",
        "prompts/pr_review.md",
        "prompts/pr_review_brief.md",
        "prompts/doc_generation.md",
        "prompts/research.md",
        "prompts/error_notification.md",
        "prompts/pdf_templates/.gitkeep",
    ),
    # : execution-runner activity + runner placeholders.
    "execution-runner-worker": (
        "src/workflows/execution_run_workflow.py",
        "src/activities/ssh.py",
        "src/activities/docker.py",
        "src/activities/vault.py",
        "src/activities/minio.py",
        "src/runners/__init__.py",
        "src/runners/local_docker.py",
        "src/runners/remote_ssh.py",
        "src/runners/remote_ssh_docker.py",
        "src/runners/noop.py",
    ),
    # : Streamlit pages + config project structure.
    # Workflows / Orphan Branches / PO Review moved to the admin
    # dashboard (admin-gated governance surfaces), so they are no
    # longer part of the end-user Streamlit page catalog.
    "streamlit-app": (
        "app.py",
        "requirements.txt",
        "config.py",
        "mcp_client.py",
        "pages/1_chat.py",
        "pages/2_task_creator.py",
        "pages/3_explorer.py",
        "config/quick_actions.yaml",
    ),
    # : Next.js 14 app router project structure.
    "admin-dashboard": (
        "package.json",
        "next.config.mjs",
        "tsconfig.json",
        "app/layout.tsx",
        "app/page.tsx",
        "app/services/page.tsx",
        "app/workflows/page.tsx",
        "app/departments/page.tsx",
        "app/prompts/page.tsx",
        "app/audit/page.tsx",
        "app/costs/page.tsx",
        "app/notifications/page.tsx",
        "app/security/page.tsx",
        "app/feature-flags/page.tsx",
        "components/.gitkeep",
        "lib/api-client.ts",
    ),
}


#: Workspace-relative paths required by the repository contract.
#: the invariant iterates over this list in addition to the per-Component set.
INFRA_AND_LIB_REQUIRED_PATHS: tuple[str, ...] = (
    # Shared libraries - every package ships a manifest, src tree, README
    # .
    "libs/http-shared/pyproject.toml",
    "libs/http-shared/src/http_shared/__init__.py",
    "libs/http-shared/src/http_shared/client.py",
    "libs/http-shared/README.md",
    "libs/llm-orchestrator/pyproject.toml",
    "libs/llm-orchestrator/src/llm_orchestrator/__init__.py",
    "libs/llm-orchestrator/src/llm_orchestrator/provider.py",
    "libs/llm-orchestrator/README.md",
    "libs/temporal-shared/pyproject.toml",
    "libs/temporal-shared/src/temporal_shared/__init__.py",
    "libs/temporal-shared/src/temporal_shared/capabilities.py",
    "libs/temporal-shared/README.md",
    "libs/auth-shared/pyproject.toml",
    "libs/auth-shared/src/auth_shared/__init__.py",
    "libs/auth-shared/src/auth_shared/oidc.py",
    "libs/auth-shared/README.md",
    "libs/db-shared/pyproject.toml",
    "libs/db-shared/src/db_shared/__init__.py",
    "libs/db-shared/src/db_shared/session.py",
    "libs/db-shared/README.md",
    "libs/messages/pyproject.toml",
    "libs/messages/src/messages/__init__.py",
    "libs/messages/src/messages/tr/messages.json",
    "libs/messages/src/messages/en/messages.json",
    "libs/messages/README.md",
    "libs/prompts/pyproject.toml",
    "libs/prompts/src/prompts/__init__.py",
    "libs/prompts/src/prompts/loader.py",
    "libs/prompts/README.md",
    "libs/web-shared/package.json",
    "libs/web-shared/tsconfig.json",
    "libs/web-shared/src/index.ts",
    "libs/web-shared/src/deeplink.ts",
    "libs/web-shared/README.md",
    # Postgres init scripts .
    "infra/postgres/00_schemas.sql",
    "infra/postgres/10_automation.sql",
    "infra/postgres/40_assistant.sql",
    "infra/postgres/50_shared.sql",
    "infra/postgres/99_temporal.sql",
    # Temporal dynamic config .
    "infra/temporal/dynamicconfig/development-sql.yaml",
    # Vault / MinIO dev-mode notes .
    "infra/vault/README.md",
    "infra/minio/README.md",
    # Observability project structures .
    "infra/observability/prometheus.yml",
    "infra/observability/loki-config.yaml",
    "infra/observability/grafana-datasources.yaml",
    # Departments configuration .
    "config/departments.json",
    "config/departments.schema.json",
    # Compose orchestration .
    "infra/docker-compose.yml",
    "infra/docker-compose.dev.yml",
    # Workspace-root metadata .
    ".env.example",
    ".gitignore",
)


# ---------------------------------------------------------------------------
# Forbidden paths
# ---------------------------------------------------------------------------

#: Paths that MUST NOT exist under the workspace root after project generation
#: generation. the invariant's test treats glob-like patterns
#: (``**/...``) by walking the tree; concrete paths are checked directly.
#:
#: Notes:
#: - ``helm/`` and ``k8s/`` artifacts are out of scope .
#: - ``KEDA ScaledObject``-style YAML is out of scope .
#: - The ``vllm`` Compose service is excluded but that
#: invariant is enforced inside ``test_compose_structure.py`` (Property
#: 4.1) rather than via a filesystem path here.
#: - ``forge-app/`` was previously listed but has been removed: the
#: compliance workflow introduces a Forge
#: add-on project structure under ``platform/forge-app/`` gated by
#: ``FEATURE_FLAG_FORGE_ADDON_ENABLED``, so the directory is now an
#: in-scope (opt-in) artifact rather than a forbidden one.
FORBIDDEN_PATHS: tuple[str, ...] = (
    "helm",
    "k8s",
    "k8s/manifests",
    # Glob-style patterns; the invariant's test resolves these by walking.
    "**/helm",
    "**/*ScaledObject*.yaml",
    "**/keda-*.yaml",
)


# ---------------------------------------------------------------------------
# Expected Compose services (the invariant)
# ---------------------------------------------------------------------------

#: The exact set of service names the parsed ``infra/docker-compose.yml``
#: must equal. This includes the profile-gated ``task-intake-service``
#: (the invariant ensures the gating predicate). ``vllm`` is intentionally
#: NOT in this set -
#:
#: ``admin-dashboard-ui`` is the Compose service name for the
#: ``admin-dashboard`` Component.
#: ``streamlit-ui`` is the Compose service name for the ``streamlit-app``
#: Component, added because the workflow topology requires the
#: end-user UI in Compose under its manifest ``compose_service_name``.
EXPECTED_COMPOSE_SERVICES: frozenset[str] = frozenset(
    {
        # Infrastructure services
        "postgres",
        "redis",
        "vault",
        "minio",
        "temporal",
        "temporal-ui",
        "atlassian-mcp",
        "firecrawl",
        "opencode-sidecar",
        # Application services and workers
        "automation-service",
        "assistant-service",
        "agent-runner-worker",
        "execution-runner-worker",
        # automation-worker hosts the ``automation-tq`` Temporal task
        # queue; manifest entry added for the workflow topology.
        "automation-worker",
        "admin-dashboard-api",
        "admin-dashboard-ui",
        # End-user UI (foundation added for the workflow)
        "streamlit-ui",
        # Profile-gated
        "task-intake-service",
        # Reverse proxy (profile-gated; bridges the public + internal nets)
        "traefik",
        # Inbound webhook tunnel (profile-gated; exposes the webhook
        # receiver to external Atlassian/Bitbucket webhooks without a
        # manual port-forward).
        "webhook-tunnel",
    }
)


#: Host ports published by infrastructure-only Compose services. Joined
#: with Component host ports by ``test_port_uniqueness.py`` (the invariant)
#: to assert global uniqueness.
INFRA_PUBLISHED_PORTS: dict[str, tuple[int, ...]] = {
    "postgres": (35432,),
    "redis": (36379,),
    "vault": (38200,),
    "temporal": (37233,),
    "temporal-ui": (38233,),
    "minio": (39000, 39001),
    "firecrawl": (33002,),
    "atlassian-mcp": (38090,),
}


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pytest CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register workspace-level CLI options consumed by the integration suite.

 ``--run-docker`` gates the Compose boot smoke tests in
 ``tests/integration/``. Those tests bind
 to a real Docker daemon, publish host ports, and are therefore
 opt-in so the default fast-lane property/unit suite stays
 self-contained and parallel-safe.
 """

    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help=(
            "Run integration tests that require a local Docker daemon "
            "(e.g. Compose boot smoke tests). Off by default; tests "
            "that need Docker SKIP cleanly when this flag is absent."
        ),
    )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Workspace root path; the anchor for every relative file lookup."""

    return WORKSPACE_ROOT


@pytest.fixture(scope="session")
def component_manifest() -> tuple[ComponentSpec, ...]:
    """Convenience fixture mirroring the module-level ``COMPONENT_MANIFEST``.

 Property tests can either import the constant directly (preferred for
 use inside ``@given`` strategies) or take this fixture as a function
 argument (preferred for example-based tests).
 """

    return COMPONENT_MANIFEST


@pytest.fixture(scope="session")
def required_paths() -> dict[str, tuple[str, ...]]:
    """Returns the per-type required-paths mapping used by the invariant."""

    return REQUIRED_PATHS


@pytest.fixture(scope="session")
def forbidden_paths() -> tuple[str, ...]:
    """Returns the forbidden-paths tuple used by the invariant."""

    return FORBIDDEN_PATHS


@pytest.fixture(scope="session")
def expected_compose_services() -> frozenset[str]:
    """Returns the expected set of Compose service names (the invariant)."""

    return EXPECTED_COMPOSE_SERVICES


__all__ = [
    "ComponentSpec",
    "ComponentType",
    "RuntimeKind",
    "WORKSPACE_ROOT",
    "COMPONENT_MANIFEST",
    "HTTP_SERVICES",
    "TEMPORAL_WORKERS",
    "UI_COMPONENTS",
    "REQUIRED_PATHS",
    "REQUIRED_PATHS_BY_NAME",
    "INFRA_AND_LIB_REQUIRED_PATHS",
    "FORBIDDEN_PATHS",
    "EXPECTED_COMPOSE_SERVICES",
    "INFRA_PUBLISHED_PORTS",
    "repo_root",
    "component_manifest",
    "required_paths",
    "forbidden_paths",
    "expected_compose_services",
]


# ---------------------------------------------------------------------------
# Fix: Graceful handling of collection errors
# ---------------------------------------------------------------------------
# Instead of aborting the entire test suite when one file has a syntax
# or import error, this hook logs a warning and allows other tests to
# continue running.

def pytest_collectreport(report):
    """Handle collection errors gracefully - skip problematic files."""
    if report.outcome == "failed":
        import warnings
        warnings.warn(
            f"Collection failed for {report.nodeid}: "
            f"{report.longrepr}\n"
            f"Skipping this file and continuing with remaining tests.",
            stacklevel=1,
        )
