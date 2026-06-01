"""Property test for LLM call path whitelist.

**Validates: Requirement 1.4**

Property 2 (LLM subset, design §Correctness Properties): repository
altındaki her ``.py`` kaynak dosyası için LLM kütüphane çağrıları
(``openai``, ``anthropic``, ``llm_orchestrator`` import'ları) yalnızca
``services/assistant-service/`` ve ``workers/agent-runner-worker/``
ağaçlarında — ve bu kütüphanelerin kendi tanım yerleri olan
``libs/llm-orchestrator/`` ve test scaffold'ları (``tests/unit/``,
``tests/integration/``, ``tests/property/``, ``tests/fixtures/``)
içinde bulunur. Diğer her path Requirement 1.4 ihlali sayılır.

This module is a sibling of ``test_path_coverage.py``; the LLM-specific
checks live here per task 11.4 (file split). Both modules consume the
same ``_path_whitelist`` helper, but ``test_llm_call_paths.py``:

* runs the LLM scanner against the live tree and asserts zero
  findings,
* uses Hypothesis to sample one source file at a time and confirm
  the per-file invariant — same shrinking pattern as the broader
  Property 2 tests in ``test_path_coverage.py``,
* exercises the scanner against synthetic source snippets so the
  detection rules stay locked in independent of the production
  corpus.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up from ``tests/property/``.
# Mirroring the bootstrap pattern used by ``test_path_coverage`` and
# ``test_health_contract`` keeps direct ``python -m pytest
# tests/property/test_llm_call_paths.py`` invocations working.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _path_whitelist import (  # noqa: E402
    LLM_WHITELIST,
    SHARED_TEST_FIXTURE_WHITELIST,
    Finding,
    format_findings,
    iter_source_files,
    scan_llm_calls,
)

# ---------------------------------------------------------------------------
# Workspace anchor
# ---------------------------------------------------------------------------

# tests/property/test_llm_call_paths.py → platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Aggregate assertion against the live tree
# ---------------------------------------------------------------------------


def test_no_llm_imports_outside_whitelist() -> None:
    """**Validates: Requirement 1.4**

    LLM kütüphane import'ları (``openai``, ``anthropic``,
    ``llm_orchestrator``) yalnızca ``services/assistant-service/``,
    ``workers/agent-runner-worker/``, ``libs/llm-orchestrator/`` ve
    paylaşılan test scaffold'ları içinde bulunabilir. Diğer her path
    Requirement 1.4 ihlali sayılır.

    The shared-test-fixture whitelist is added to the default LLM
    whitelist because ``tests/unit/test_llm_orchestrator.py`` and any
    future scaffolds that exercise the orchestrator must legitimately
    import the package by name. The orchestrator library is the
    *single source of truth* for LLM access (design §4.1 Components,
    §6.3 Property → test mapping); its tests are not new LLM caller
    sites.
    """

    findings = scan_llm_calls(
        whitelist=tuple(LLM_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
    )
    assert not findings, (
        "Requirement 1.4 violation — LLM library import found outside "
        "assistant-service / agent-runner-worker / llm-orchestrator. "
        "Route every LLM call through libs/llm-orchestrator and import "
        "it only from those two services.\n"
        + format_findings(findings)
    )


# ---------------------------------------------------------------------------
# Hypothesis property — per-file invariant
# ---------------------------------------------------------------------------


_SOURCE_FILES: tuple[Path, ...] = tuple(iter_source_files())


def _llm_findings_for_file(path: Path) -> list[Finding]:
    """Return LLM findings produced by *path* under the live scan.

    The scanner normalises results against ``PLATFORM_ROOT``, so we
    run it once and filter rather than crafting a per-file root.
    """

    rel = path.relative_to(_PLATFORM_ROOT).as_posix()
    return [
        f
        for f in scan_llm_calls(
            whitelist=tuple(LLM_WHITELIST) + SHARED_TEST_FIXTURE_WHITELIST,
        )
        if f.path == rel
    ]


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(path=st.sampled_from(_SOURCE_FILES) if _SOURCE_FILES else st.nothing())
def test_per_file_no_llm_findings(path: Path) -> None:
    """**Validates: Requirement 1.4**

    Hypothesis property: rastgele örneklenen herhangi bir ``.py``
    kaynak dosyası için LLM scanner boş döner.

    Sampling-then-asserting gives Hypothesis tight shrinking when a
    regression slips in — the failing example is the single offending
    file rather than an aggregate across the tree.
    """

    findings = _llm_findings_for_file(path)
    rel = path.relative_to(_PLATFORM_ROOT).as_posix()
    assert not findings, (
        f"Requirement 1.4 violation in {rel}:\n" + format_findings(findings)
    )


def test_llm_source_corpus_is_non_empty() -> None:
    """**Validates: Requirement 1.4**

    Guard against a vacuous Hypothesis property: the source corpus
    must contain at least one ``.py`` file or the
    :func:`test_per_file_no_llm_findings` strategy degenerates to
    :func:`hypothesis.strategies.nothing` and the property silently
    passes.
    """

    assert _SOURCE_FILES, (
        "LLM-scan source corpus is empty; iter_source_files() must "
        "return at least one file under the platform root"
    )


# ---------------------------------------------------------------------------
# Scanner self-tests — synthetic source snippets
# ---------------------------------------------------------------------------


def _write(tmp: Path, rel: str, src: str) -> Path:
    """Write *src* to ``tmp/rel`` (creating parents) and return path."""

    p = tmp / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


class TestLlmCallPathScannerSelfChecks:
    """Self-tests guaranteeing :func:`scan_llm_calls` actually flags
    LLM imports outside the whitelist. Without these the production
    aggregate test above could pass vacuously after a scanner
    regression.
    """

    def test_openai_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**"""

        src = (
            "import openai\n"
            "def chat():\n"
            "    return openai.ChatCompletion.create(model='gpt-4', messages=[])\n"
        )
        _write(tmp_path, "services/automation-service/src/leak.py", src)
        findings = scan_llm_calls(tmp_path)
        assert any(
            f.category == "llm" and f.symbol == "openai" for f in findings
        ), findings

    def test_anthropic_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**"""

        src = (
            "import anthropic\n"
            "def chat():\n"
            "    return anthropic.Anthropic().messages.create()\n"
        )
        _write(tmp_path, "services/admin-dashboard-api/src/leak.py", src)
        findings = scan_llm_calls(tmp_path)
        assert any(
            f.category == "llm" and f.symbol == "anthropic" for f in findings
        ), findings

    def test_llm_orchestrator_import_outside_whitelist_is_flagged(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**"""

        src = (
            "from llm_orchestrator import LLMProviderFactory\n"
            "def f():\n"
            "    return LLMProviderFactory.from_env()\n"
        )
        _write(tmp_path, "services/automation-service/src/leak.py", src)
        findings = scan_llm_calls(tmp_path)
        assert any(
            f.category == "llm" and f.symbol == "llm_orchestrator"
            for f in findings
        ), findings

    def test_openai_inside_assistant_service_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**"""

        src = (
            "import openai\n"
            "def chat():\n"
            "    return openai.ChatCompletion.create(model='gpt-4', messages=[])\n"
        )
        _write(tmp_path, "services/assistant-service/src/llm.py", src)
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_openai_inside_agent_runner_worker_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**"""

        src = (
            "from openai import AsyncOpenAI\n"
            "client = AsyncOpenAI()\n"
        )
        _write(
            tmp_path,
            "workers/agent-runner-worker/src/activities/llm.py",
            src,
        )
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_llm_orchestrator_inside_lib_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**

        The orchestrator library defines ``llm_orchestrator``; it is
        the single source of truth and itself imports
        ``openai``/``anthropic`` to wrap them. ``libs/llm-orchestrator/``
        is on the default whitelist.
        """

        src = (
            "import openai\n"
            "import anthropic\n"
            "class Wrapper:\n"
            "    def __init__(self) -> None:\n"
            "        self.openai = openai\n"
            "        self.anthropic = anthropic\n"
        )
        _write(
            tmp_path,
            "libs/llm-orchestrator/src/llm_orchestrator/provider.py",
            src,
        )
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_from_import_form_is_flagged(self, tmp_path: Path) -> None:
        """**Validates: Requirement 1.4**

        ``from openai import X`` (the from-form) must be flagged just
        like ``import openai``.
        """

        src = (
            "from openai import AsyncOpenAI\n"
            "def make_client():\n"
            "    return AsyncOpenAI()\n"
        )
        _write(tmp_path, "services/admin-dashboard-api/src/leak.py", src)
        findings = scan_llm_calls(tmp_path)
        assert any(
            f.category == "llm" and f.symbol == "openai" for f in findings
        ), findings

    def test_unrelated_module_is_not_flagged(self, tmp_path: Path) -> None:
        """**Validates: Requirement 1.4**

        Top-level modules that share a substring with LLM module names
        (e.g. ``openai_compatibility_layer``) but resolve to a
        different root are not flagged. The scanner matches the *root*
        of dotted names exactly.
        """

        src = (
            "import openai_compatibility_layer as ocl\n"
            "import some_pkg.openai\n"
            "from anthropic_helper import x\n"
        )
        _write(tmp_path, "services/automation-service/src/clean.py", src)
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_string_literal_with_provider_name_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**

        Sabit ``"openai"`` string literali (config key, log mesajı,
        provider seçici) flag'lenmez — yalnızca import edilen modül
        adları kontrol edilir.
        """

        src = (
            "PROVIDER = 'openai'\n"
            "def select():\n"
            "    return PROVIDER == 'anthropic'\n"
        )
        _write(tmp_path, "services/automation-service/src/cfg.py", src)
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_relative_import_is_not_flagged(self, tmp_path: Path) -> None:
        """**Validates: Requirement 1.4**

        Relative import'lar (``from . import x``) third-party LLM
        modüllerine ulaşamaz; scanner ``ast.ImportFrom.level == 0``
        koşuluyla bu durumu zaten dışlar.
        """

        src = (
            "from . import openai_helper\n"
            "from ..common import anthropic_helper\n"
        )
        _write(
            tmp_path,
            "services/automation-service/src/pkg/x.py",
            src,
        )
        findings = scan_llm_calls(tmp_path)
        assert findings == [], findings

    def test_finding_path_is_workspace_relative_forward_slash(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirement 1.4**

        Finding objelerinin ``path`` alanı forward-slash workspace-
        relative formattadır; bu :func:`format_findings` çıktısının
        platform-bağımsız olmasını garanti eder.
        """

        src = "import openai\n"
        _write(
            tmp_path,
            "services/automation-service/src/sub/leak.py",
            src,
        )
        findings = scan_llm_calls(tmp_path)
        assert findings, "expected at least one finding"
        f = findings[0]
        assert f.path == "services/automation-service/src/sub/leak.py"
        assert f.category == "llm"
        assert f.symbol == "openai"
        assert f.lineno == 1
