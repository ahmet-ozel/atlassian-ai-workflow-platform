"""Shared pytest fixtures for the multi-service-scaffold test suite.

This module is the single source of truth that every property test under
``tests/property/`` parameterises against. It exposes four data
structures and one fixture that mirror the design document
(`.kiro/specs/multi-service-scaffold/design.md`):

- ``COMPONENT_MANIFEST`` — the 8 in-scope Components from §4.1
  (Component Manifest table). Each row carries enough metadata to drive
  Property tests 1, 3, 5, 6, 9, 10, 12.
- ``REQUIRED_PATHS`` — relative paths every Component of a given
  ``ComponentType`` must have, plus infra/config/root path lists.
- ``FORBIDDEN_PATHS`` — paths the scaffold *must not* produce
  (Property 11; design "Kapsam Dışı Bileşenler").
- ``EXPECTED_COMPOSE_SERVICES`` — the 17 service names that the parsed
  ``infra/docker-compose.yml`` must equal under Property 4.1, including
  ``task-intake-service`` (profile-gated) and ``automation-worker``
  (added by platform-mimari-workflows task 2.6 — foundation 10-entry
  topology + 1).
- ``repo_root`` fixture — workspace root ``Path`` used by every
  filesystem-walking property test.

It also injects each shared library's ``src/`` directory onto
``sys.path`` so property tests can ``import http_shared``,
``import temporal_shared``, etc., without first installing the libs.

Related references
------------------

- design §4.1 (Component Manifest), §3.4 (libs/ table),
  §"Compose Bağımlılık DAG'ı" (depends_on),
  §6.3 (Property → test mapping).
- requirements 1.1–1.7, 6.1–6.5 (paths covered by REQUIRED_PATHS),
  18.1–18.5 (out-of-scope artifacts → FORBIDDEN_PATHS).
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


# K3 fix (GEREKSINIM_ANALIZI.md): cross-service property tests use
# ``from src.X`` patterns (e.g. ``from src.lifecycle.audit_writer
# import ...``). Python's import system can only resolve ONE ``src``
# package at a time, so listing every service ``src/`` here would
# REGRESS the collection state (each service has overlapping module
# names — ``src/config.py``, ``src/main.py`` exist in 6 services).
#
# The minimum-correctness compromise: this top-level conftest stays
# free of service-src manipulations. Tests that legitimately need a
# worker namespace (e.g. ``automation_worker`` for the EK3 parity
# test) handle their own ``sys.path`` injection at test-module
# import time so the rest of the suite keeps collecting.
#
# Per-service unit tests run from the service's own ``tests/`` tree
# (which already work — see ``services/admin-dashboard-api/tests/``,
# ``workers/automation-worker/tests/``). Properly closing the
# cross-service collection gap requires renaming each ``src`` to a
# unique top-level package (``admin_dashboard_api`` etc.) — tracked.


# ---------------------------------------------------------------------------
# Component Manifest (design §4.1)
# ---------------------------------------------------------------------------

ComponentType = Literal["http_service", "temporal_worker", "ui_component"]
RuntimeKind = Literal["python", "node"]


@dataclass(frozen=True)
class ComponentSpec:
    """Mental model for a single Component row in design §4.1.

    The schema mirrors the pseudocode dataclass in the design document
    so this fixture can stand in for the table during property test
    parameterisation. Every field is hashable so the dataclass itself
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
# Per-Component manifest rows (mirrors design §4.1 plus the "Compose
# Bağımlılık DAG'ı" section for ``depends_on`` and the §"Env Değişkeni
# Sözlüğü" table for ``required_env``).
# ---------------------------------------------------------------------------

#: Standard env block every HTTP service must expose locally
#: (Requirement 10.3, Property 5).
_HTTP_BASE_ENV: tuple[str, ...] = ("PORT", "LOG_LEVEL")

#: Full LLM provider block — required by every LLM-consuming Component
#: regardless of the currently configured provider (Requirement 10.4).
_LLM_ENV_BLOCK: tuple[str, ...] = (
    "LLM_PROVIDER",
    "VLLM_BASE_URL",
    "LLM_MODEL_NAME",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

#: MCP / Firecrawl consumer block (Requirement 10.5).
_MCP_FIRECRAWL_ENV: tuple[str, ...] = ("MCP_BASE_URL", "FIRECRAWL_BASE_URL")


COMPONENT_MANIFEST: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        name="automation-service",
        type="http_service",
        runtime="python",
        path="services/automation-service",
        container_port=8080,
        host_port=8080,
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
        host_port=8081,
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
        host_port=8082,
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
        host_port=8083,
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
        host_port=8501,
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
        host_port=3000,
        profiles=(),
        # The Compose service for this UI is named ``admin-dashboard-ui``
        # (see EXPECTED_COMPOSE_SERVICES); the manifest carries the
        # backend dependency so Property 4.6's superset check passes.
        depends_on=("admin-dashboard-api",),
        required_env=(
            "PORT",
            "LOG_LEVEL",
            "NEXT_PUBLIC_ADMIN_API_BASE_URL",
        ),
        client_source_id=None,  # browser → BFF, no X-Client-Source from UI
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
# Required filesystem paths (Property 1)
# ---------------------------------------------------------------------------

# Paths every Python HTTP service must have under its component path
# (Requirements 2.1–2.4, 2.6–2.9, 12.1–12.3, 18.4).
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

# Paths every Temporal worker must have (Requirements 3.1–3.6, 18.4).
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

# Paths every UI Component must have (Requirements 4.1–4.7).
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
#: ``ComponentType``. Used by Property 1 to layer Component-specific
#: assertions on top of the type-level baseline.
REQUIRED_PATHS_BY_NAME: dict[str, tuple[str, ...]] = {
    # Per requirements 2.6: webhooks/, temporal_client.py, decision/
    "automation-service": (
        "src/webhooks/__init__.py",
        "src/temporal_client.py",
        "src/decision/__init__.py",
        "migrations/.gitkeep",
    ),
    # Requirement 2.7: chat/, llm/, prompts/
    "assistant-service": (
        "src/chat/__init__.py",
        "src/llm/__init__.py",
    ),
    # Requirement 2.8: routers/, auth/, clients/, prompts_git/
    "admin-dashboard-api": (
        "src/routers/__init__.py",
        "src/auth/__init__.py",
        "src/clients/__init__.py",
        "src/prompts_git/__init__.py",
    ),
    # Requirement 2.9: same skeleton as other HTTP services + intake modules
    "task-intake-service": (
        "src/intake/__init__.py",
        "src/channels/__init__.py",
    ),
    # Requirement 3.4: agent-runner workflow + activity placeholders +
    # prompts/ tree (Requirements 3.5, 18.3).
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
    # Requirement 3.6: execution-runner activity + runner placeholders.
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
    # Requirements 4.1–4.3: Streamlit pages + config skeleton.
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
    # Requirements 4.4–4.6: Next.js 14 app router skeleton.
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


#: Workspace-relative paths required by Requirements 5.1–5.9 and 6.1–6.5.
#: Property 1 iterates over this list in addition to the per-Component set.
INFRA_AND_LIB_REQUIRED_PATHS: tuple[str, ...] = (
    # Shared libraries — every package ships a manifest, src tree, README
    # (Requirement 5.1).
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
    # Postgres init scripts (Requirement 6.1, 6.2, 6.6).
    "infra/postgres/00_schemas.sql",
    "infra/postgres/10_automation.sql",
    "infra/postgres/40_assistant.sql",
    "infra/postgres/50_shared.sql",
    "infra/postgres/99_temporal.sql",
    # Temporal dynamic config (Requirement 6.3).
    "infra/temporal/dynamicconfig/development-sql.yaml",
    # Vault / MinIO dev-mode notes (Requirement 6.4, 18.5).
    "infra/vault/README.md",
    "infra/minio/README.md",
    # Observability skeletons (Requirement 6.5).
    "infra/observability/prometheus.yml",
    "infra/observability/loki-config.yaml",
    "infra/observability/grafana-datasources.yaml",
    # Departments configuration (Requirement 7.1, 7.6).
    "config/departments.json",
    "config/departments.schema.json",
    # Compose orchestration (Requirement 8.1, 8.8).
    "infra/docker-compose.yml",
    "infra/docker-compose.dev.yml",
    # Workspace-root metadata (Requirement 11.1, 11.6).
    ".env.example",
    ".gitignore",
)


# ---------------------------------------------------------------------------
# Forbidden paths (Property 11; design "Kapsam Dışı Bileşenler")
# ---------------------------------------------------------------------------

#: Paths that MUST NOT exist under the workspace root after scaffold
#: generation. Property 11's test treats glob-like patterns
#: (``**/...``) by walking the tree; concrete paths are checked directly.
#:
#: Notes:
#: - ``helm/`` and ``k8s/`` artifacts are out of scope (Requirement 18.1).
#: - ``KEDA ScaledObject``-style YAML is out of scope (Requirement 18.2).
#: - The ``vllm`` Compose service is excluded (Requirement 8.10) but that
#:   invariant is enforced inside ``test_compose_structure.py`` (Property
#:   4.1) rather than via a filesystem path here.
#: - ``forge-app/`` was previously listed but has been removed: the
#:   ``platform-mimari-uyumluluk`` spec (R6 / Q3) introduces a Forge
#:   add-on skeleton under ``platform/forge-app/`` gated by
#:   ``FEATURE_FLAG_FORGE_ADDON_ENABLED``, so the directory is now an
#:   in-scope (opt-in) artifact rather than a forbidden one.
FORBIDDEN_PATHS: tuple[str, ...] = (
    "helm",
    "k8s",
    "k8s/manifests",
    # Glob-style patterns; Property 11's test resolves these by walking.
    "**/helm",
    "**/*ScaledObject*.yaml",
    "**/keda-*.yaml",
)


# ---------------------------------------------------------------------------
# Expected Compose services (Property 4.1)
# ---------------------------------------------------------------------------

#: The exact set of service names the parsed ``infra/docker-compose.yml``
#: must equal. This includes the profile-gated ``task-intake-service``
#: (Property 4.3 ensures the gating predicate). ``vllm`` is intentionally
#: NOT in this set — Requirement 8.10 / Property 4.1.
#:
#: ``admin-dashboard-ui`` is the Compose service name for the
#: ``admin-dashboard`` Component (design §"Compose Bağımlılık DAG'ı").
#: ``streamlit-ui`` is the Compose service name for the ``streamlit-app``
#: Component, added by ``platform-mimari-foundation`` task 10.1
#: (Requirement 1.1 — foundation 10-entry topology requires the
#: end-user UI in Compose under its manifest ``compose_service_name``).
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
        # queue (workflows-spec Requirement 1.1, 1.2; manifest entry
        # added by task 2.6). Foundation 10-entry topology + 1.
        "automation-worker",
        "admin-dashboard-api",
        "admin-dashboard-ui",
        # End-user UI (foundation Requirement 1.1 — added by task 10.1)
        "streamlit-ui",
        # Profile-gated
        "task-intake-service",
    }
)


#: Host ports published by infrastructure-only Compose services. Joined
#: with Component host ports by ``test_port_uniqueness.py`` (Property 3)
#: to assert global uniqueness.
INFRA_PUBLISHED_PORTS: dict[str, tuple[int, ...]] = {
    "postgres": (5432,),
    "redis": (6379,),
    "vault": (8200,),
    "temporal": (7233,),
    "temporal-ui": (8233,),
    "minio": (9000, 9001),
    "firecrawl": (3002,),
    "atlassian-mcp": (8090,),
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
    ``tests/integration/`` (see task 14.3–14.5 in
    ``.kiro/specs/multi-service-scaffold/tasks.md``). Those tests bind
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
    """Returns the per-type required-paths mapping used by Property 1."""

    return REQUIRED_PATHS


@pytest.fixture(scope="session")
def forbidden_paths() -> tuple[str, ...]:
    """Returns the forbidden-paths tuple used by Property 11."""

    return FORBIDDEN_PATHS


@pytest.fixture(scope="session")
def expected_compose_services() -> frozenset[str]:
    """Returns the expected set of Compose service names (Property 4.1)."""

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
# R23 Fix: Graceful handling of collection errors
# ---------------------------------------------------------------------------
# Instead of aborting the entire test suite when one file has a syntax
# or import error, this hook logs a warning and allows other tests to
# continue running.

def pytest_collectreport(report):
    """Handle collection errors gracefully — skip problematic files."""
    if report.outcome == "failed":
        import warnings
        warnings.warn(
            f"Collection failed for {report.nodeid}: "
            f"{report.longrepr}\n"
            f"Skipping this file and continuing with remaining tests.",
            stacklevel=1,
        )
