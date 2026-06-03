"""Directory tree completeness checks.

Every Component declared in :data:`COMPONENT_MANIFEST` must ship the
type-level baseline of files (``src/main.py``, ``Dockerfile``,
``.env.example`` etc.) listed in :data:`REQUIRED_PATHS` plus the
Component-specific extras in :data:`REQUIRED_PATHS_BY_NAME`. In addition,
every workspace-relative path in :data:`INFRA_AND_LIB_REQUIRED_PATHS`
(shared libs, infra init scripts, departments config, Compose files,
root metadata) must exist under :data:`WORKSPACE_ROOT`.

The Hypothesis check samples ``(component, relative_path)`` pairs and
asserts existence; the parametrised pytest function exhaustively walks
the infra / lib path list so any single missing artefact surfaces as a
clearly named test ID rather than a Hypothesis shrink.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by ``test_health_contract``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import (  # noqa: E402
    COMPONENT_MANIFEST,
    INFRA_AND_LIB_REQUIRED_PATHS,
    REQUIRED_PATHS,
    REQUIRED_PATHS_BY_NAME,
    WORKSPACE_ROOT,
    ComponentSpec,
)


# ---------------------------------------------------------------------------
# Per-component required paths (type baseline + name-specific extras)
# ---------------------------------------------------------------------------


def _component_required_paths(component: ComponentSpec) -> tuple[str, ...]:
    """All relative paths a single Component must ship.

    Combines the ``ComponentType`` baseline from :data:`REQUIRED_PATHS`
    with the Component-specific extras in
    :data:`REQUIRED_PATHS_BY_NAME` (defaulting to an empty tuple when
    no per-name overrides exist).
    """

    base = REQUIRED_PATHS[component.type]
    extras = REQUIRED_PATHS_BY_NAME.get(component.name, ())
    return tuple(base) + tuple(extras)


# Pre-compute the per-component path universe so the Hypothesis strategy
# can sample a single ``(component, path)`` pair in one step. Sampling
# from a flat list keeps shrinking deterministic and avoids the
# ``flatmap`` cost when the manifest grows.
_COMPONENT_PATH_PAIRS: tuple[tuple[ComponentSpec, str], ...] = tuple(
    (component, relative_path)
    for component in COMPONENT_MANIFEST
    for relative_path in _component_required_paths(component)
)


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_every_component_has_required_type_paths(component: ComponentSpec) -> None:
    """Type-level baseline files exist for every Component.

    For every Component, every relative path listed under
    ``REQUIRED_PATHS[component.type]`` must resolve to an existing
    filesystem entry under ``WORKSPACE_ROOT / component.path``.
    """

    component_root: Path = WORKSPACE_ROOT / component.path
    missing: list[str] = []
    for relative_path in REQUIRED_PATHS[component.type]:
        candidate = component_root / relative_path
        if not candidate.exists():
            missing.append(str(candidate.relative_to(WORKSPACE_ROOT)))
    assert not missing, (
        f"Component '{component.name}' is missing required {component.type} "
        f"paths: {missing}"
    )


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(COMPONENT_MANIFEST))
def test_every_component_has_required_name_specific_paths(
    component: ComponentSpec,
) -> None:
    """Component-specific extras also exist.

    Layered on top of the type baseline, every entry in
    ``REQUIRED_PATHS_BY_NAME[component.name]`` (e.g. the Streamlit
    page set, the agent-runner prompt files, the Next.js app router
    pages) must also resolve to an existing path under the Component
    root.
    """

    component_root: Path = WORKSPACE_ROOT / component.path
    extras = REQUIRED_PATHS_BY_NAME.get(component.name, ())
    missing: list[str] = []
    for relative_path in extras:
        candidate = component_root / relative_path
        if not candidate.exists():
            missing.append(str(candidate.relative_to(WORKSPACE_ROOT)))
    assert not missing, (
        f"Component '{component.name}' is missing name-specific paths: {missing}"
    )


@settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(pair=st.sampled_from(_COMPONENT_PATH_PAIRS))
def test_sampled_component_path_exists(pair: tuple[ComponentSpec, str]) -> None:
    """A single sampled ``(component, path)`` pair exists.

    Samples one Component × one required relative path at a time. This makes
    Hypothesis shrink down to the single missing artefact when a
    regression occurs, which is more actionable than the per-Component
    aggregations above.
    """

    component, relative_path = pair
    candidate: Path = WORKSPACE_ROOT / component.path / relative_path
    assert candidate.exists(), (
        f"Required path missing for component '{component.name}': "
        f"{candidate.relative_to(WORKSPACE_ROOT)}"
    )


# ---------------------------------------------------------------------------
# Infra / lib / config / root required paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative_path", INFRA_AND_LIB_REQUIRED_PATHS)
def test_infra_and_lib_required_path_exists(relative_path: str) -> None:
    """Every infra / lib / config / root path exists.

    Each entry in :data:`INFRA_AND_LIB_REQUIRED_PATHS` is a workspace-
    relative path that must resolve under :data:`WORKSPACE_ROOT`.
    Parametrising (instead of looping inside a single test) gives a
    distinct test ID per missing artefact, which is the diagnostic
    signal these checks are designed to surface.
    """

    candidate: Path = WORKSPACE_ROOT / relative_path
    assert candidate.exists(), (
        f"Required workspace path missing: {relative_path}"
    )


# ===========================================================================
# Hassas çağrı path whitelist'i
#
# Repository altındaki her
# ``.py`` kaynak dosyası için (a) Jira/Bitbucket/Confluence host'larına
# yönelik ``httpx``/``requests``/``aiohttp`` çağrıları yalnızca
# ``services/atlassian_mcp_bitbucket/`` ağacında bulunur, (b) ``paramiko``/
# ``asyncssh``/``subprocess`` ile başlatılan SSH komutları ve Docker
# socket erişimi yalnızca ``workers/execution-runner-worker/``
# ağacında bulunur, (c) LLM kütüphane çağrıları yalnızca
# ``services/assistant-service/`` ve ``workers/agent-runner-worker/``
# ağaçlarında bulunur, (d) ``workers/*/activities/`` altındaki dosyalar
# ``client.start_workflow`` veya eşdeğer çağrı içermez.
#
# This block extends ``test_path_coverage.py`` with:
#
# * an aggregated full-scan assertion that every scanner returns zero
#   findings against the live source tree,
# * a Hypothesis property that samples a single source file at a time
#   and confirms the per-file invariant — this gives fine-grained
#   shrinking when a single file regresses,
# * scanner self-checks against synthetic source snippets so the
#   detection logic itself is exercised even when the production
#   source is invariant-clean.
#
# The LLM-specific subset lives in a sibling module
# ``test_llm_call_paths.py``. That module
# imports the same helper and adds LLM-focused synthetic-source self
# tests to keep the scanners' detection logic locked in.
# ===========================================================================

import ast as _ast  # noqa: E402  -- imported here so the path checks above stay untouched

from _path_whitelist import (  # noqa: E402
    ATLASSIAN_HTTP_WHITELIST,
    LLM_WHITELIST,
    SHARED_TEST_FIXTURE_WHITELIST,
    SSH_DOCKER_WHITELIST,
    Finding,
    format_findings,
    iter_source_files,
    scan_activities_start_workflow,
    scan_atlassian_http_calls,
    scan_llm_calls,
    scan_ssh_docker_calls,
)


# ---------------------------------------------------------------------------
# Aggregate assertions — one assertion per scanner against the live tree
# ---------------------------------------------------------------------------
#
# Each scanner is run against the platform root with the default
# whitelist plus the shared-test-fixture whitelist (so e.g. unit tests
# for ``libs/llm-orchestrator`` itself can import the package by name
# without violating the LLM path boundary — the orchestrator library is the
# single source of truth and its tests legitimately exercise it).
#
# Scanner-level findings are aggregated into a list and the assertion
# message uses :func:`format_findings` so a regression surfaces every
# offending file at once.


def test_property2_no_atlassian_http_outside_whitelist() -> None:
    """Atlassian HTTP calls stay inside the approved gateway paths.

    Atlassian host'larına yönelik ``httpx``/``requests``/``aiohttp``
    çağrıları yalnızca ``services/atlassian_mcp_bitbucket/``,
    ``libs/http-shared/`` ve ``libs/mcp_client/`` ağaçlarında
    bulunabilir. Diğer her path violation sayılır.
    """

    findings = scan_atlassian_http_calls(
        whitelist=tuple(ATLASSIAN_HTTP_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
    )
    assert not findings, (
        "Atlassian HTTP call violation — call "
        "found outside the atlassian_mcp_bitbucket MCP whitelist. Route every "
        "Jira/Bitbucket/Confluence call through the MCP.\n"
        + format_findings(findings)
    )


def test_property2_no_ssh_docker_outside_execution_runner() -> None:
    """SSH and Docker access stay inside the execution runner.

    SSH istemcileri (``paramiko``/``asyncssh``) ve ``subprocess`` ile
    başlatılan ``ssh``/``scp`` komutları ile Docker SDK kullanımı
    yalnızca ``workers/execution-runner-worker/`` ağacında bulunabilir.
    """

    findings = scan_ssh_docker_calls(
        whitelist=tuple(SSH_DOCKER_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
    )
    assert not findings, (
        "SSH or Docker access violation — access "
        "found outside execution-runner-worker.\n"
        + format_findings(findings)
    )


def test_property2_no_activity_start_workflow_calls() -> None:
    """Activity modules do not start workflows directly.

    ``workers/*/activities/`` altındaki dosyalar
    ``client.start_workflow`` veya ``execute_workflow`` /
    ``start_child_workflow`` çağrısı içermez; workflow karar mantığı
    yalnız workflow modüllerinde olmalıdır.
    """

    findings = scan_activities_start_workflow()
    assert not findings, (
        "Workflow-start violation — call "
        "inside activity module. Move workflow-decision logic to "
        "workers/*/workflows/.\n"
        + format_findings(findings)
    )


# ---------------------------------------------------------------------------
# Hypothesis property — per-file invariant
# ---------------------------------------------------------------------------
#
# Sampling a single source file at a time gives Hypothesis tight
# shrinking: when an invariant breaks, the failing example is the
# shortest path to one offending file. The strategy materialises the
# corpus once at module import to keep generation cheap.


_SOURCE_FILES: tuple[Path, ...] = tuple(iter_source_files())


def _findings_for_file(path: Path) -> list[Finding]:
    """Return every whitelist finding produced by *path*.

    Runs each scanner against the live tree and filters its results
    to those originating from *path*. Filtering is preferred over
    re-running the scanners against a single-file root because the
    scanners normalise paths against ``PLATFORM_ROOT`` and the
    whitelist comparison is path-prefix-based.
    """

    rel = path.relative_to(_PLATFORM_ROOT_FOR_PROP2).as_posix()

    aggregated: list[Finding] = []
    aggregated.extend(
        f
        for f in scan_atlassian_http_calls(
            whitelist=tuple(ATLASSIAN_HTTP_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
        )
        if f.path == rel
    )
    aggregated.extend(
        f
        for f in scan_ssh_docker_calls(
            whitelist=tuple(SSH_DOCKER_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
        )
        if f.path == rel
    )
    aggregated.extend(
        f
        for f in scan_llm_calls(
            whitelist=tuple(LLM_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
        )
        if f.path == rel
    )
    aggregated.extend(
        f for f in scan_activities_start_workflow() if f.path == rel
    )
    return aggregated


# Re-derive the platform root the same way ``_path_whitelist`` does,
# without re-importing the private constant.
_PLATFORM_ROOT_FOR_PROP2: Path = Path(__file__).resolve().parents[2]


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(path=st.sampled_from(_SOURCE_FILES) if _SOURCE_FILES else st.nothing())
def test_property2_per_file_no_findings(path: Path) -> None:
    """Sampled source files have no whitelist findings.

    Hypothesis property: rastgele örneklenen herhangi bir ``.py``
    kaynak dosyası için whitelist scanner'ları boş döner.

    Sampling rather than enumerating gives Hypothesis a chance to
    minimise the failing example to a single file when a regression
    occurs — the report points at exactly one offending file even
    when the aggregate ``test_property2_*`` assertions above would
    otherwise lump multiple findings together.
    """

    findings = _findings_for_file(path)
    rel = path.relative_to(_PLATFORM_ROOT_FOR_PROP2).as_posix()
    assert not findings, (
        f"Path whitelist violation in {rel}:\n"
        + format_findings(findings)
    )


# ---------------------------------------------------------------------------
# Scanner self-tests — synthetic source snippets
# ---------------------------------------------------------------------------
#
# The aggregate / per-file assertions above are only as strong as the
# scanners they delegate to. These self-tests exercise the scanner
# logic against synthetic ``.py`` files dropped into a tmp_path so the
# detection rules stay locked in even if the production source tree is
# invariant-clean.


def _write(tmp: Path, rel: str, src: str) -> Path:
    """Write *src* to ``tmp/rel`` (creating parents) and return the path."""

    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


class TestProperty2ScannerSelfChecks:
    """Self-tests guaranteeing the path whitelist scanners actually flag
    violations on synthetic source — without this layer the production
    tests above could pass vacuously after a scanner regression.
    """

    def test_atlassian_http_call_outside_mcp_is_flagged(self, tmp_path: Path) -> None:
        """Direct Atlassian HTTP calls outside the gateway are flagged."""

        src = (
            "import httpx\n"
            "async def fetch():\n"
            "    async with httpx.AsyncClient() as c:\n"
            "        return await c.get('https://acme.atlassian.net/rest/api/3/myself')\n"
        )
        _write(tmp_path, "services/automation-service/src/leaky.py", src)
        findings = scan_atlassian_http_calls(tmp_path)
        assert any(
            f.category == "atlassian_http"
            and "leaky.py" in f.path
            and f.symbol.startswith("httpx")
            for f in findings
        ), findings

    def test_atlassian_http_inside_mcp_is_allowed(self, tmp_path: Path) -> None:
        """Direct Atlassian HTTP calls inside approved paths are allowed.

        Aynı kod ``services/atlassian_mcp_bitbucket/`` altında (whitelist'in
        kendisi excluded_dirs içinde) bulunduğunda finding üretmez —
        bu zaten varsayılan exclude'lu yürüyüşle örtülür ve
        gateway subtree'i ayrı tutulur.
        ``libs/http-shared/`` whitelist içinde olduğundan benzer şekilde
        izin verilir.
        """

        src = (
            "import httpx\n"
            "async def fetch():\n"
            "    async with httpx.AsyncClient() as c:\n"
            "        return await c.get('https://acme.atlassian.net/rest/api/3/myself')\n"
        )
        _write(tmp_path, "libs/http-shared/src/http_shared/atlassian.py", src)
        findings = scan_atlassian_http_calls(tmp_path)
        assert findings == [], findings

    def test_atlassian_host_without_http_client_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Host literals without HTTP client calls are ignored.

        Salt host literal'i (docstring, sabit URL, log mesajı)
        flag'lenmez — yalnızca **HTTP çağrısı + host**
        kombinasyonunu yasaklar.
        """

        src = (
            '"""Doc reference: https://acme.atlassian.net/rest/api/3/myself."""\n'
            "ATLASSIAN_DOC_URL = 'https://acme.atlassian.net/rest/api/3/myself'\n"
        )
        _write(tmp_path, "services/automation-service/src/notes.py", src)
        findings = scan_atlassian_http_calls(tmp_path)
        assert findings == [], findings

    def test_http_client_without_atlassian_host_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Generic HTTP clients without Atlassian hosts are ignored.

        Genel ``httpx`` kullanımı (Atlassian host'u referansı
        olmadan) flag'lenmez — hedefli host'lara
        çağrıyı yasaklar, generic HTTP istemcisini değil.
        """

        src = (
            "import httpx\n"
            "async def health_probe(url: str) -> int:\n"
            "    async with httpx.AsyncClient() as c:\n"
            "        return (await c.get(url)).status_code\n"
        )
        _write(tmp_path, "services/automation-service/src/health.py", src)
        findings = scan_atlassian_http_calls(tmp_path)
        assert findings == [], findings

    def test_paramiko_outside_execution_runner_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Paramiko outside the execution runner is flagged."""

        src = (
            "import paramiko\n"
            "def connect():\n"
            "    return paramiko.SSHClient()\n"
        )
        _write(tmp_path, "services/automation-service/src/ssh_helper.py", src)
        findings = scan_ssh_docker_calls(tmp_path)
        assert any(
            f.category == "ssh" and f.symbol == "paramiko" for f in findings
        ), findings

    def test_paramiko_inside_execution_runner_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """Paramiko inside the execution runner is allowed."""

        src = (
            "import paramiko\n"
            "def connect():\n"
            "    return paramiko.SSHClient()\n"
        )
        _write(
            tmp_path,
            "workers/execution-runner-worker/src/activities/ssh.py",
            src,
        )
        findings = scan_ssh_docker_calls(tmp_path)
        assert findings == [], findings

    def test_subprocess_ssh_string_is_flagged(self, tmp_path: Path) -> None:
        """SSH shell commands are flagged."""

        src = (
            "import subprocess\n"
            "def run_remote():\n"
            "    return subprocess.run('ssh user@host echo hi', shell=True)\n"
        )
        _write(tmp_path, "services/automation-service/src/shellout.py", src)
        findings = scan_ssh_docker_calls(tmp_path)
        assert any(
            f.category == "ssh" and f.symbol.startswith("subprocess+ssh")
            for f in findings
        ), findings

    def test_subprocess_scp_list_is_flagged(self, tmp_path: Path) -> None:
        """SCP shell commands are flagged."""

        src = (
            "import subprocess\n"
            "def copy_remote():\n"
            "    return subprocess.run(['scp', 'src', 'host:dst'])\n"
        )
        _write(tmp_path, "services/automation-service/src/cp.py", src)
        findings = scan_ssh_docker_calls(tmp_path)
        assert any(
            f.category == "ssh" and f.symbol.startswith("subprocess+scp")
            for f in findings
        ), findings

    def test_subprocess_non_ssh_command_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Non-SSH subprocess commands are ignored.

        ``subprocess.run(['ls', '-la'])`` gibi normal shell-out'lar
        bu scanner kapsamında değildir.
        """

        src = (
            "import subprocess\n"
            "def list_files():\n"
            "    return subprocess.run(['ls', '-la'])\n"
        )
        _write(tmp_path, "services/automation-service/src/ls.py", src)
        findings = scan_ssh_docker_calls(tmp_path)
        assert findings == [], findings

    def test_docker_sdk_outside_execution_runner_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Docker SDK use outside the execution runner is flagged."""

        src = (
            "import docker\n"
            "def run_box():\n"
            "    return docker.from_env()\n"
        )
        _write(tmp_path, "services/automation-service/src/docker_helper.py", src)
        findings = scan_ssh_docker_calls(tmp_path)
        assert any(f.category == "docker" for f in findings), findings

    def test_activity_start_workflow_is_flagged(self, tmp_path: Path) -> None:
        """Activity modules that start workflows are flagged."""

        src = (
            "from temporalio import activity\n"
            "@activity.defn\n"
            "async def some_activity(client) -> str:\n"
            "    handle = await client.start_workflow(SomeWf.run, id='wid', task_queue='q')\n"
            "    return handle.id\n"
        )
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/leaky.py",
            src,
        )
        findings = scan_activities_start_workflow(tmp_path)
        assert any(
            f.category == "activity_start_workflow"
            and "leaky.py" in f.path
            and f.symbol.endswith("start_workflow")
            for f in findings
        ), findings

    def test_activity_execute_workflow_is_flagged(self, tmp_path: Path) -> None:
        """Activity modules using ``execute_workflow`` are flagged.

        ``execute_workflow`` (wait-for-result variant) ve
        ``start_child_workflow`` da activity dosyalarında yasaktır.
        """

        src = (
            "async def helper(client):\n"
            "    return await client.execute_workflow(Wf.run, id='wid', task_queue='q')\n"
        )
        _write(
            tmp_path,
            "workers/execution-runner-worker/src/activities/leak2.py",
            src,
        )
        findings = scan_activities_start_workflow(tmp_path)
        assert any(
            f.symbol.endswith("execute_workflow") for f in findings
        ), findings

    def test_workflow_start_in_workflow_dir_is_not_flagged_by_activity_scan(
        self, tmp_path: Path
    ) -> None:
        """Workflow-start calls inside workflow directories are ignored.

        ``workers/*/workflows/`` altında ``start_workflow``
        çağrılarına bu scanner dokunmaz — workflow karar mantığının
        yeri burasıdır.
        """

        src = (
            "async def kick(client):\n"
            "    return await client.start_workflow(Wf.run, id='wid', task_queue='q')\n"
        )
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/workflows/wf.py",
            src,
        )
        findings = scan_activities_start_workflow(tmp_path)
        assert findings == [], findings

    def test_method_named_start_workflow_in_random_path_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Workflow-start method names outside activity paths are ignored.

        Activity dışı yollarda ``start_workflow`` adı flag'lenmez —
        scanner yalnız ``/activities/`` fragment içeren path'leri
        inspect eder.
        """

        src = (
            "async def kick(client):\n"
            "    return await client.start_workflow(Wf.run, id='wid', task_queue='q')\n"
        )
        _write(
            tmp_path,
            "services/automation-service/src/temporal_glue.py",
            src,
        )
        findings = scan_activities_start_workflow(tmp_path)
        assert findings == [], findings

    def test_scanner_skips_files_with_syntax_errors(
        self, tmp_path: Path
    ) -> None:
        """Syntax-broken files do not crash the scanners.

        Syntax-broken bir dosya scan'i çökertmez — scanner bu dosyayı
        sessizce atlar; production `test_workflow_file_parses` gibi
        ayrı bir test bu dosyaları yakalar.
        """

        _write(
            tmp_path,
            "services/automation-service/src/broken.py",
            "def f(:\n    pass\n",
        )
        # All three scanners should silently no-op on this file.
        assert scan_atlassian_http_calls(tmp_path) == []
        assert scan_ssh_docker_calls(tmp_path) == []
        assert scan_activities_start_workflow(tmp_path) == []

    def test_excluded_dirs_are_pruned_from_scan(self, tmp_path: Path) -> None:
        """Excluded directories are pruned from scans.

        ``__pycache__`` / ``.venv`` / ``atlassian_mcp_bitbucket`` gibi
        dizinler ``SCAN_EXCLUDED_DIRS`` ile baştan budanır; içlerinde
        oluşturulan yapay ihlaller scanner tarafından raporlanmaz.
        """

        bad_src = (
            "import paramiko\n"
            "def x():\n"
            "    return paramiko.SSHClient()\n"
        )
        # Place in an excluded dir; should NOT be reported.
        _write(tmp_path, "services/x/__pycache__/cached.py", bad_src)
        _write(tmp_path, "services/x/.venv/lib/site-packages/foo.py", bad_src)
        # Verify the excluded files exist but are not flagged.
        findings = scan_ssh_docker_calls(tmp_path)
        assert findings == [], findings

    def test_findings_contain_relative_path_and_lineno(
        self, tmp_path: Path
    ) -> None:
        """Findings include relative path and line number.

        Finding objesinin ``path`` alanı forward-slash workspace-
        relative formattadır ve ``lineno`` 1-indexed kaynak satırına
        işaret eder. Bu sözleşme whitelist hata mesajlarının
        ``format_findings`` çıktısının okunabilir olması için
        gereklidir.
        """

        src = (
            "\n"  # line 1 = blank to push the import to line 2
            "import paramiko\n"
            "def x():\n"
            "    return paramiko.SSHClient()\n"
        )
        _write(
            tmp_path,
            "services/automation-service/src/sub/sshy.py",
            src,
        )
        findings = scan_ssh_docker_calls(tmp_path)
        assert findings, "expected at least one finding"
        f = findings[0]
        assert f.path == "services/automation-service/src/sub/sshy.py", f
        assert f.lineno == 2, f
        assert f.category == "ssh", f
        assert f.symbol == "paramiko", f


# ---------------------------------------------------------------------------
# Self-test for the source-corpus iterator
# ---------------------------------------------------------------------------
#
# The Hypothesis property above samples from ``_SOURCE_FILES``; if that
# tuple is ever empty the property silently passes. This guard makes
# the failure mode explicit.


def test_property2_source_corpus_is_non_empty() -> None:
    """The source corpus is non-empty.

    The Hypothesis sample-from-corpus strategy is only meaningful if
    the corpus has at least one ``.py`` file under the platform root.
    """

    assert _SOURCE_FILES, (
        "Path whitelist source corpus is empty; iter_source_files() must "
        "return at least one file under the platform root"
    )


# Sanity-import to surface ImportErrors loudly during test collection
# rather than masking them as hypothesis-strategy failures.
del _ast  # `ast` is used by the helper, not by this module directly.
