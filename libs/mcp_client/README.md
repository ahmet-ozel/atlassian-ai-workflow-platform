# mcp_client

Foundation MCP client library for the platform.
This package owns the **single-source enforcement points** for two
critical routing rules:

- **Banned MCP tool list** - `tool_filter.filter_tools`
  removes the canonical banned tools (`bitbucket_merge_pr`,
  `confluence_delete_page`) from any tool catalog handed to an LLM.
- **PR draft enforcement** - `pr_draft.enforce_pr_draft`
  rewrites outgoing PR payloads so that `draft` is always `True`,
  regardless of what an LLM produced, and emits an audit
  `pr_draft_enforced` event when it had to flip a `False` (or absent)
  value.

The `atlassian_client` module is currently a thin skeleton. The real
HTTP wiring (Jira / Bitbucket / Confluence) lives behind the MCP
gateway; the skeleton exists here so the single-source enforcement
tests have a stable import point.

## Public API

```python
from mcp_client import (
    BANNED_TOOLS,
    enforce_pr_draft,
    filter_tools,
)

# Strip banned tools out of a tool catalog before exposing it to an LLM.
safe_tools = filter_tools(catalog)

# Coerce ``draft`` to True on every outgoing PR payload and audit the
# cases where the caller intended otherwise.
safe_payload = enforce_pr_draft(
    payload,
    audit_logger=logger,
    actor_id="bot.payment.bitbucket",
    actor_role="system",
    dept_id="payment",
)
```

The audit dependency is optional - passing `audit_logger=None` keeps
`enforce_pr_draft` usable in tests / pure-function call paths. When a
logger is supplied, the function `await`s it and writes an
`AuditEvent` with `action="pr_draft_enforced"` only when the
enforcement actually changed the payload.

## Why a separate package?

`atlassian_mcp_bitbucket` is the Atlassian MCP gateway; enforcement
helpers stay in `mcp_client` so callers share one outbound wrapper.
`mcp_client` is used by `assistant-service`,
`agent-runner-worker`, and `automation-service` whenever they need to
call into Atlassian via the MCP. Keeping the enforcement code in one
shared lib means the focused tests (`test_tool_filter.py`,
`test_pr_draft_enforcement.py`) can assert the rule with a single
import path rather than walking every caller.

## Standalone build & run

```bash
# from libs/mcp_client/
python -m pip install --upgrade build
python -m build              # produces dist/mcp_client-*.whl

# install into a target environment
python -m pip install dist/mcp_client-*.whl
```
