"""Static test for Streamlit chat proxying through assistant-service.

Static AST scan of the Streamlit pages directory. The chat page
(``pages/1_chat.py``) and the task creator page
(``pages/2_task_creator.py``) MUST NOT import ``mcp_client`` (or
its submodules) directly - every LLM-related call MUST go through
``assistant-service``. Direct ``mcp_client`` use here would let the
chat surface bypass the foundation banned-tool list / capability
gate / audit chain.

The test reads each Streamlit page's source as text + AST, and
fails the build when an offending import is found. Hypothesis is
not strictly necessary - the property is universal over the file
set - but we still write it as a pytest module so it surfaces in
the same `tests/property/` lane as the rest of the audit gates.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_STREAMLIT_PAGES = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
    / "pages"
)

_FORBIDDEN_IMPORTS = ("mcp_client", "atlassian_mcp_bitbucket")
_GUARDED_PAGES = ("1_chat.py", "2_task_creator.py")


@pytest.mark.parametrize("page_name", _GUARDED_PAGES)
def test_chat_pages_do_not_import_mcp_client(page_name: str) -> None:
    page = _STREAMLIT_PAGES / page_name
    if not page.is_file():
        pytest.skip(
            f"{page_name} not present yet"
        )
    source = page.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(forbidden) for forbidden in _FORBIDDEN_IMPORTS):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod.startswith(forbidden) for forbidden in _FORBIDDEN_IMPORTS):
                offenders.append(mod)

    assert not offenders, (
        f"{page_name} imports forbidden module(s) {offenders!r}; the "
        "chat surface must proxy via assistant-service. "
        "Move the call site behind the assistant-service client."
    )
