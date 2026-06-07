"""invariant for environment variable coverage.


invariant: Environment variable coverage.

Every Component declared in:data:`COMPONENT_MANIFEST` must have its
``required_env`` set fully covered by the union of:

* ``<component.path>/.env.example`` (Component_Env_Example,
 - Standalone_Mode), and
* ``./.env.example`` at the workspace root (Root_Env_Example,
 - Compose_Stack default lookup or per-service
 ``env_file:`` directives).

Both files are dotenv-formatted; this test parses their LHS keys and
treats the variable as "covered" when it appears in either file. The
two-file model is intentional and explicitly documented in the design
(``§"Ortam Değişkeni Modeli"``): duplication between the local and
root files is not a DRY violation - each file serves a different
deployment mode.

Layered checks
--------------

1. **Union coverage (5a)** - for every Component × every
 ``v ∈ c.required_env``, ``v`` appears as an LHS key in either the
 Component's local ``.env.example`` or the root ``.env.example``.
2. **HTTP service local minimum (5b)** - ``PORT`` and ``LOG_LEVEL`` are
 always present in the *local* ``.env.example`` of every
 ``http_service`` Component, regardless of whether the root file
 carries them.
3. **LLM block (5c)** - every Component whose ``required_env`` contains
 ``LLM_PROVIDER`` (an "LLM consumer") has the full 5-variable LLM
 block (``LLM_PROVIDER``, ``VLLM_BASE_URL``, ``LLM_MODEL_NAME``,
 ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``) covered by the local +
 root union.
4. **MCP / Firecrawl block (5d)** - every Component whose
 ``required_env`` mentions ``MCP_BASE_URL`` or ``FIRECRAWL_BASE_URL``
 has both URLs covered by the local + root union,
 16.5 for the task-intake / web-ingestion path).
5. **CLIENT_SOURCE optionality (5e)** - ``CLIENT_SOURCE`` is *never*
 listed in ``required_env``: each Component has a
 compile-time default ``client_source_id``; the env override is
 optional). This invariant is asserted directly so a future
 regression that demotes ``CLIENT_SOURCE`` to "required" surfaces
 here rather than silently failing in production.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by ``test_path_coverage``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import (  # noqa: E402
    COMPONENT_MANIFEST,
    HTTP_SERVICES,
    WORKSPACE_ROOT,
    ComponentSpec,
)


# ---------------------------------------------------------------------------
# Env file parsing
# ---------------------------------------------------------------------------

# Standard dotenv KEY=VALUE shape; KEY is `[A-Za-z_][A-Za-z0-9_]*`.
# We anchor at the start of the line (after stripping comments) and
# stop at the first ``=`` so VALUE content (URLs with ``=``, JSON
# blobs, etc.) does not interfere with LHS extraction.
_ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _parse_env_keys(path: Path) -> frozenset[str]:
    """Extract LHS variable names from a dotenv-style file.

 Skips blank lines and comments (lines whose first non-whitespace
 character is ``#``). Returns ``frozenset`` when the file does not
 exist so callers can union local + root keys without conditional
 branches; a *missing* local file makes the union check fall back
 to the root file alone, which is the intended behaviour for
 Components that rely entirely on Compose-level ``env_file:``
 propagation.
 """

    if not path.is_file():
        return frozenset()
    keys: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(raw_line)
        if match is not None:
            keys.add(match.group(1))
    return frozenset(keys)


def _component_env_keys(component: ComponentSpec) -> frozenset[str]:
    """Keys defined in the Component's local ``.env.example`` (Standalone)."""

    return _parse_env_keys(WORKSPACE_ROOT / component.path / ".env.example")


def _root_env_keys() -> frozenset[str]:
    """Keys defined in the workspace-root ``.env.example`` (Compose)."""

    return _parse_env_keys(WORKSPACE_ROOT / ".env.example")


# ---------------------------------------------------------------------------
# Component-class subsets (precomputed at import time so Hypothesis can
# sample directly via ``st.sampled_from``)
# ---------------------------------------------------------------------------

#: Full LLM provider block - says every LLM consumer
#: must carry the complete 5-variable block regardless of the currently
#: configured ``LLM_PROVIDER`` (so switching providers only flips one
#: env value, never requires editing the file's *shape*).
_LLM_ENV_BLOCK: tuple[str, ...] = (
    "LLM_PROVIDER",
    "VLLM_BASE_URL",
    "LLM_MODEL_NAME",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)

#: HTTP service local minimum.
_HTTP_LOCAL_REQUIRED: tuple[str, ...] = ("PORT", "LOG_LEVEL")

#: MCP / Firecrawl consumer block.
_MCP_FIRECRAWL_BLOCK: tuple[str, ...] = ("MCP_BASE_URL", "FIRECRAWL_BASE_URL")

#: A Component is an "LLM consumer" iff it lists ``LLM_PROVIDER`` in its
#: ``required_env`` (the canonical entry point of the LLM block).
_LLM_CONSUMERS: tuple[ComponentSpec, ...] = tuple(
    c for c in COMPONENT_MANIFEST if "LLM_PROVIDER" in c.required_env
)

#: A Component is an "MCP / Firecrawl consumer" iff its ``required_env``
#: lists at least one of the two base URLs. Once a Component talks to
#: either upstream, expects both URLs to be wired so
#: the component can be re-pointed without a rebuild.
_MCP_FIRECRAWL_CONSUMERS: tuple[ComponentSpec, ...] = tuple(
    c
    for c in COMPONENT_MANIFEST
    if any(v in c.required_env for v in _MCP_FIRECRAWL_BLOCK)
)


# ---------------------------------------------------------------------------
# invariant - required_env is covered by local ∪ root
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_required_env_covered_by_local_or_root(component: ComponentSpec) -> None:
    """invariant - every required env var is covered locally or at root.

 For each Component × each ``v ∈ component.required_env``, the
 variable must appear as an LHS key in either the local
 ``.env.example`` or the root ``.env.example``. The Compose stack
 relies on Compose's default ``.env`` lookup (root file) plus the
 explicit ``env_file:../<component>/.env`` directives in
 ``infra/docker-compose.yml`` (local file) so the union is the
 accurate model of "which variables are wired at runtime".
 """

    local_keys = _component_env_keys(component)
    root_keys = _root_env_keys()
    union = local_keys | root_keys
    missing = [v for v in component.required_env if v not in union]
    assert not missing, (
        f"Component '{component.name}' is missing required env vars in both "
        f"local ({component.path}/.env.example) and root.env.example: "
        f"{missing}"
    )


# ---------------------------------------------------------------------------
# invariant - HTTP services carry PORT and LOG_LEVEL *locally*
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(HTTP_SERVICES))
def test_http_services_have_port_and_log_level_locally(
    component: ComponentSpec,
) -> None:
    """invariant - local-minimum for HTTP services.

 Every ``http_service`` Component MUST declare ``PORT`` and
 ``LOG_LEVEL`` in its *own* ``.env.example``, not just in the root
 file. This guarantees a Standalone_Mode container started with
 ``docker run --env-file.env`` always knows which port to bind and
 at what log level - without depending on the Compose-level root
 ``.env``.
 """

    local_keys = _component_env_keys(component)
    missing = [v for v in _HTTP_LOCAL_REQUIRED if v not in local_keys]
    assert not missing, (
        f"HTTP service '{component.name}' is missing required local env vars "
        f"({component.path}/.env.example): {missing}"
    )


# ---------------------------------------------------------------------------
# invariant - LLM consumers carry the full 5-variable LLM block
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(_LLM_CONSUMERS))
def test_llm_consumers_have_full_llm_block(component: ComponentSpec) -> None:
    """invariant - full LLM block coverage.

 Every Component that consumes the LLM provider abstraction (i.e.
 has ``LLM_PROVIDER`` in ``required_env``) must have the complete
 5-variable LLM block - ``LLM_PROVIDER``, ``VLLM_BASE_URL``,
 ``LLM_MODEL_NAME``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY`` -
 covered by the local + root union. The block stays *shape-stable*
 across providers so flipping ``LLM_PROVIDER`` between supported providers
 ``vllm`` / ``openai`` / ``anthropic`` is a one-line change.
 """

    local_keys = _component_env_keys(component)
    root_keys = _root_env_keys()
    union = local_keys | root_keys
    missing = [v for v in _LLM_ENV_BLOCK if v not in union]
    assert not missing, (
        f"LLM-consuming component '{component.name}' is missing LLM block "
        f"variables in both local ({component.path}/.env.example) and root "
        f".env.example: {missing}"
    )


# ---------------------------------------------------------------------------
# invariant - MCP / Firecrawl consumers carry both base URLs
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(_MCP_FIRECRAWL_CONSUMERS))
def test_mcp_firecrawl_consumers_have_both_base_urls(
    component: ComponentSpec,
) -> None:
    """invariant - dual-URL coverage.

 Components that talk to either upstream (``atlassian-mcp`` or
 ``firecrawl``) must have both ``MCP_BASE_URL`` and
 ``FIRECRAWL_BASE_URL`` covered by the local + root union, so the
 consumer can be retargeted at either URL without touching code
 or rebuilding the image. ``task-intake-service`` and
 ``agent-runner-worker`` both carry both URLs locally; the four
 other MCP consumers (automation, assistant, admin-dashboard-api,
 streamlit-app) rely on the root file for ``FIRECRAWL_BASE_URL``.
 """

    local_keys = _component_env_keys(component)
    root_keys = _root_env_keys()
    union = local_keys | root_keys
    missing = [v for v in _MCP_FIRECRAWL_BLOCK if v not in union]
    assert not missing, (
        f"MCP/Firecrawl consumer '{component.name}' is missing base URL "
        f"variables in both local ({component.path}/.env.example) and root "
        f".env.example: {missing}"
    )


# ---------------------------------------------------------------------------
# invariant - CLIENT_SOURCE is optional
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_client_source_is_optional(component: ComponentSpec) -> None:
    """invariant - ``CLIENT_SOURCE`` MUST NOT be required.

 says ``CLIENT_SOURCE`` is an *optional* override -
 each Component carries a compile-time default
 (``client_source_id`` in:data:`COMPONENT_MANIFEST`) that takes
 effect when the env var is absent. A regression that demotes
 ``CLIENT_SOURCE`` to "required" would break Standalone_Mode
 Components whose operators never need to set it manually; this
 test pins the optionality contract.

 The check is straightforward: ``CLIENT_SOURCE`` MUST NOT appear in
 the Component's ``required_env``. Whether the variable happens to
 be *defined* in the local or root ``.env.example`` is irrelevant
 to this property - its presence or absence MUST NOT cause this
 test to fail.
 """

    assert "CLIENT_SOURCE" not in component.required_env, (
        f"Component '{component.name}' lists CLIENT_SOURCE in required_env, "
        "but the operational rule mandates that CLIENT_SOURCE is an optional "
        "override with the Component's static client_source_id as the "
        "default."
    )
