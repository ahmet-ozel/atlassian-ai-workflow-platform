"""Unit tests for the ``tests/property/_path_whitelist`` helper module.

The helper is a *reusable* AST scanner consumed by the property tests
in :mod:`tests.property.test_path_coverage`,
:mod:`tests.property.test_llm_call_paths`, and
:mod:`tests.property.test_workflow_determinism_static`. These unit
tests pin down the behaviour of each scanner against synthetic source
trees so future contributors can refactor the scanner with
confidence.

The tests are example-based (no Hypothesis) because the helper is a
piece of static infrastructure - its surface is small and the
interesting cases are concrete code shapes (``import paramiko``,
``httpx.AsyncClient(base_url="https://acme.atlassian.net")``,
``client.start_workflow(...)`` inside an activity, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# tests/unit/test_path_whitelist_helper.py  platform/
_PLATFORM_ROOT = Path(__file__).resolve().parents[2]
_PROPERTY_DIR = _PLATFORM_ROOT / "tests" / "property"
if str(_PROPERTY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROPERTY_DIR))

import _path_whitelist as pw  # noqa: E402  (sys.path set above)


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, source: str) -> Path:
    """Write *source* to ``root/rel``, creating parent directories.

    Returns the absolute path of the written file. Used by every
    test below to populate a synthetic workspace inside ``tmp_path``.
    """

    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# scan_atlassian_http_calls
# ---------------------------------------------------------------------------


class TestAtlassianHttpScanner:
    """Direct Atlassian HTTP calls
    outside the ``atlassian_mcp_bitbucket`` MCP must be detected."""

    def test_direct_atlassian_call_via_httpx_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Direct Atlassian calls via httpx are flagged."""
        _write(
            tmp_path,
            "services/some-service/src/client.py",
            (
                "import httpx\n"
                "def fetch():\n"
                '    return httpx.get("https://acme.atlassian.net/rest/api/3/myself")\n'
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        finding = findings[0]
        assert finding.category == "atlassian_http"
        assert finding.path.endswith("client.py")
        assert finding.symbol.startswith("httpx")

    def test_atlassian_call_inside_whitelist_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Files inside the whitelist
        (here, ``libs/http-shared/``) are inspected but never reported.
        """
        _write(
            tmp_path,
            "libs/http-shared/src/http_shared/client.py",
            (
                "import httpx\n"
                "def make_client():\n"
                '    return httpx.AsyncClient(base_url="https://acme.atlassian.net")\n'
            ),
        )
        findings = pw.scan_atlassian_http_calls(
            tmp_path, whitelist=("libs/http-shared/",)
        )
        assert findings == []

    def test_call_to_mcp_proxy_is_not_flagged(self, tmp_path: Path) -> None:
        """Calls to ``atlassian-mcp:8090``
        (the proxy) are the *allowed* path; only direct upstream
        ``*.atlassian.net`` / ``bitbucket.org`` calls are forbidden.
        """
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/jira.py",
            (
                "import httpx\n"
                "def fetch():\n"
                '    return httpx.get("http://atlassian-mcp:8090/jira/myself")\n'
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert findings == []

    def test_pure_httpx_without_atlassian_host_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Generic HTTP clients used
        for non-Atlassian hosts are ignored.
        """
        _write(
            tmp_path,
            "services/automation-service/src/vault.py",
            (
                "import httpx\n"
                'def read_secret():\n'
                '    return httpx.get("http://vault:8200/v1/secret/foo")\n'
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert findings == []

    def test_pure_atlassian_string_without_http_client_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """A docstring or constant
        mentioning an Atlassian URL without importing httpx/requests/
        aiohttp is ignored.
        """
        _write(
            tmp_path,
            "services/automation-service/src/decision/credential_resolver.py",
            (
                '"""Resolves credentials for https://acme.atlassian.net Cloud."""\n'
                "URL = 'https://acme.atlassian.net'\n"
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert findings == []

    def test_requests_library_is_also_flagged(self, tmp_path: Path) -> None:
        """``requests`` is on equal
        footing with ``httpx``.
        """
        _write(
            tmp_path,
            "services/foo/src/main.py",
            (
                "import requests\n"
                "def f():\n"
                '    return requests.post("https://acme.atlassian.net/rest/api/2/issue")\n'
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol.startswith("requests")

    def test_aiohttp_library_is_also_flagged(self, tmp_path: Path) -> None:
        """``aiohttp`` completes the
        triad of forbidden direct HTTP clients.
        """
        _write(
            tmp_path,
            "services/foo/src/main.py",
            (
                "import aiohttp\n"
                "async def f():\n"
                "    async with aiohttp.ClientSession() as s:\n"
                '        async with s.get("https://api.bitbucket.org/2.0/repositories/x") as r:\n'
                "            return await r.text()\n"
            ),
        )
        findings = pw.scan_atlassian_http_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol.startswith("aiohttp")


# ---------------------------------------------------------------------------
# scan_ssh_docker_calls
# ---------------------------------------------------------------------------


class TestSshDockerScanner:
    """SSH and Docker usage outside
    ``execution-runner-worker`` must be detected."""

    def test_paramiko_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Paramiko outside the whitelist is flagged."""
        _write(
            tmp_path,
            "services/automation-service/src/runner.py",
            (
                "import paramiko\n"
                "def connect(host: str) -> None:\n"
                "    client = paramiko.SSHClient()\n"
                "    client.connect(host)\n"
            ),
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert len(findings) >= 1
        assert any(f.symbol == "paramiko" for f in findings)
        assert all(f.category == "ssh" for f in findings if f.symbol == "paramiko")

    def test_paramiko_inside_whitelist_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """``execution-runner-worker``
        is the legitimate home of paramiko."""
        _write(
            tmp_path,
            "workers/execution-runner-worker/src/activities/ssh.py",
            "import paramiko\n",
        )
        findings = pw.scan_ssh_docker_calls(
            tmp_path, whitelist=("workers/execution-runner-worker/",)
        )
        assert findings == []

    def test_asyncssh_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """``asyncssh`` is on the same
        footing as ``paramiko``."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            "import asyncssh\n",
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol == "asyncssh"

    def test_subprocess_ssh_call_is_flagged(self, tmp_path: Path) -> None:
        """Shell-out to ``ssh`` via
        subprocess is functionally equivalent to importing paramiko."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            (
                "import subprocess\n"
                "def run() -> None:\n"
                '    subprocess.run("ssh user@host echo hi")\n'
            ),
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].category == "ssh"
        assert "subprocess" in findings[0].symbol

    def test_subprocess_scp_list_form_is_flagged(self, tmp_path: Path) -> None:
        """``subprocess.run(["scp", ...])``
        is detected the same as the string form."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            (
                "import subprocess\n"
                "def f() -> None:\n"
                '    subprocess.Popen(["scp", "a", "b"])\n'
            ),
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert "scp" in findings[0].symbol

    def test_subprocess_unrelated_call_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """``subprocess.run(["ls"])``
        and similar non-ssh shell-outs must not produce findings."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            (
                "import subprocess\n"
                "def f() -> None:\n"
                '    subprocess.run(["ls", "-la"])\n'
            ),
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert findings == []

    def test_docker_sdk_import_is_flagged_with_docker_category(
        self, tmp_path: Path
    ) -> None:
        """Docker SDK use outside
        ``execution-runner-worker`` is flagged."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            "import docker\n",
        )
        findings = pw.scan_ssh_docker_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].category == "docker"
        assert findings[0].symbol == "docker"


# ---------------------------------------------------------------------------
# scan_llm_calls
# ---------------------------------------------------------------------------


class TestLlmScanner:
    """LLM library use outside
    ``assistant-service`` and ``agent-runner-worker``."""

    def test_openai_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """OpenAI imports outside the whitelist are flagged."""
        _write(
            tmp_path,
            "services/automation-service/src/llm.py",
            "import openai\n",
        )
        findings = pw.scan_llm_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol == "openai"
        assert findings[0].category == "llm"

    def test_anthropic_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Anthropic imports outside the whitelist are flagged."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            "from anthropic import AsyncAnthropic\n",
        )
        findings = pw.scan_llm_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol == "anthropic"

    def test_llm_orchestrator_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """``libs/llm-orchestrator`` is
        the in-house LLM facade; importing it outside the LLM-allowed
        components is also a violation."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            "from llm_orchestrator import LLMProviderFactory\n",
        )
        findings = pw.scan_llm_calls(tmp_path, whitelist=())
        assert len(findings) == 1
        assert findings[0].symbol == "llm_orchestrator"

    def test_assistant_service_import_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """``assistant-service`` is
        explicitly allowed to call LLM providers."""
        _write(
            tmp_path,
            "services/assistant-service/src/llm/provider.py",
            "import openai\n",
        )
        findings = pw.scan_llm_calls(
            tmp_path, whitelist=("services/assistant-service/",)
        )
        assert findings == []

    def test_agent_runner_worker_import_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """``agent-runner-worker`` is
        explicitly allowed to call LLM providers."""
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/llm.py",
            "from llm_orchestrator import LLMProviderFactory\n",
        )
        findings = pw.scan_llm_calls(
            tmp_path, whitelist=("workers/agent-runner-worker/",)
        )
        assert findings == []


# ---------------------------------------------------------------------------
# scan_activities_start_workflow
# ---------------------------------------------------------------------------


class TestActivityStartWorkflowScanner:
    """Activity files must not start
    Temporal workflows directly."""

    def test_client_start_workflow_in_activity_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """``client.start_workflow`` in an activity is flagged."""
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/jira.py",
            (
                "from temporalio import activity\n"
                "@activity.defn\n"
                "async def fetch_then_start(client) -> None:\n"
                '    await client.start_workflow("Foo", id="x", task_queue="q")\n'
            ),
        )
        findings = pw.scan_activities_start_workflow(tmp_path)
        assert len(findings) == 1
        assert findings[0].category == "activity_start_workflow"
        assert findings[0].symbol.endswith("start_workflow")

    def test_execute_workflow_in_activity_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """``execute_workflow`` is the
        wait-for-result variant of ``start_workflow`` and is equally
        forbidden inside activity code."""
        _write(
            tmp_path,
            "workers/execution-runner-worker/src/activities/runner.py",
            (
                "async def f(client) -> None:\n"
                '    await client.execute_workflow("Foo", id="x", task_queue="q")\n'
            ),
        )
        findings = pw.scan_activities_start_workflow(tmp_path)
        assert len(findings) == 1
        assert findings[0].symbol.endswith("execute_workflow")

    def test_start_child_workflow_in_activity_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """Child-workflow starts belong
        in the parent workflow, not in activities."""
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/foo.py",
            (
                "async def f(client) -> None:\n"
                '    await client.start_child_workflow("Foo", id="x")\n'
            ),
        )
        findings = pw.scan_activities_start_workflow(tmp_path)
        assert len(findings) == 1
        assert findings[0].symbol.endswith("start_child_workflow")

    def test_start_workflow_in_workflow_module_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Calls inside
        ``workers/<w>/src/workflows/`` are not the scanner's concern."""
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/workflows/parent.py",
            (
                "async def f(client) -> None:\n"
                '    await client.start_workflow("Child", id="c", task_queue="q")\n'
            ),
        )
        findings = pw.scan_activities_start_workflow(tmp_path)
        assert findings == []

    def test_unrelated_method_in_activity_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """Only the three start_*
        methods are flagged; ``client.get_workflow_handle`` etc. are
        permitted (they read state, not start new executions)."""
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/foo.py",
            (
                "async def f(client) -> None:\n"
                '    handle = client.get_workflow_handle("Foo")\n'
                "    await handle.signal('go')\n"
            ),
        )
        findings = pw.scan_activities_start_workflow(tmp_path)
        assert findings == []


# ---------------------------------------------------------------------------
# Walk and aggregator
# ---------------------------------------------------------------------------


class TestWalkAndAggregator:
    """Smoke tests for the file walker and ``run_full_scan``."""

    def test_iter_source_files_skips_excluded_dirs(self, tmp_path: Path) -> None:
        """``__pycache__``,
        ``node_modules``, etc. are pruned at the walk level so they
        cannot contribute false positives."""
        _write(tmp_path, "src/main.py", "x = 1\n")
        _write(tmp_path, "src/__pycache__/main.cpython-312.pyc", "")
        _write(tmp_path, "node_modules/pkg/index.py", "import paramiko\n")
        _write(tmp_path, ".venv/site/lib.py", "import openai\n")

        files = list(pw.iter_source_files(tmp_path))
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in files)
        assert rels == ["src/main.py"]

    def test_iter_source_files_skips_atlassian_mcp_bitbucket_subtree(
        self, tmp_path: Path
    ) -> None:
        """The
        ``atlassian_mcp_bitbucket`` subtree is the source of legitimate
        Atlassian HTTP calls and must be pruned globally."""
        _write(tmp_path, "services/atlassian_mcp_bitbucket/src/jira/api.py", "import requests\n")
        _write(tmp_path, "services/foo/src/main.py", "x = 1\n")

        files = list(pw.iter_source_files(tmp_path))
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in files)
        assert rels == ["services/foo/src/main.py"]

    def test_run_full_scan_aggregates_categories(self, tmp_path: Path) -> None:
        """``run_full_scan``
        wires every scanner together and exposes results via the
        :class:`ScanReport` dataclass."""
        _write(
            tmp_path,
            "services/foo/src/main.py",
            "import paramiko\nimport openai\n",
        )
        _write(
            tmp_path,
            "services/bar/src/atlassian.py",
            (
                "import httpx\n"
                'def f(): return httpx.get("https://acme.atlassian.net/rest/api/3/x")\n'
            ),
        )
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/foo.py",
            (
                "async def g(client) -> None:\n"
                '    await client.start_workflow("X", id="i", task_queue="q")\n'
            ),
        )

        report = pw.run_full_scan(tmp_path)
        assert len(report.atlassian_http) >= 1
        assert len(report.ssh_docker) >= 1
        assert len(report.llm) >= 1
        assert len(report.activity_start_workflow) >= 1
        assert len(report.all_findings) == (
            len(report.atlassian_http)
            + len(report.ssh_docker)
            + len(report.llm)
            + len(report.activity_start_workflow)
        )

    def test_format_findings_renders_one_line_per_finding(self) -> None:
        """``format_findings``
        produces a multi-line bullet list usable in assert messages."""
        findings = [
            pw.Finding(
                path="services/foo/src/main.py",
                lineno=10,
                category="ssh",
                symbol="paramiko",
                detail="SSH library imported outside execution-runner-worker.",
            ),
            pw.Finding(
                path="services/bar/src/main.py",
                lineno=3,
                category="llm",
                symbol="openai",
                detail="LLM library imported outside allowed roots.",
            ),
        ]
        rendered = pw.format_findings(findings)
        lines = rendered.splitlines()
        assert len(lines) == 2
        assert "services/foo/src/main.py:10" in lines[0]
        assert "services/bar/src/main.py:3" in lines[1]

    def test_format_findings_empty_list_returns_empty_string(self) -> None:
        """Empty finding lists render as an empty string."""
        assert pw.format_findings([]) == ""


# ---------------------------------------------------------------------------
# Module-level smoke test against the live workspace
# ---------------------------------------------------------------------------


def test_full_scan_against_workspace_runs_without_error() -> None:
    """Smoke test: invoking :func:`run_full_scan` against the live
    platform tree must complete without raising. The test does not
    assert specific finding counts (those belong to property tests
    dedicated property tests) - it only guards against scanner crashes triggered
    by real-world source shapes (multi-line strings, walrus operators,
    pattern matching, async comprehensions, etc.).
    """

    report = pw.run_full_scan(pw.PLATFORM_ROOT)
    # Every list must be a list of Finding (not None, not exception).
    assert isinstance(report.atlassian_http, list)
    assert isinstance(report.ssh_docker, list)
    assert isinstance(report.llm, list)
    assert isinstance(report.activity_start_workflow, list)
    for f in report.all_findings:
        assert isinstance(f, pw.Finding)
        assert f.path  # non-empty
        assert f.lineno >= 1
        assert f.category in {
            "atlassian_http",
            "ssh",
            "docker",
            "llm",
            "activity_start_workflow",
        }
