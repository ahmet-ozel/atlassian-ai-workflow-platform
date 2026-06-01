# mcp_client

Foundation MCP client library for the platform-mimari-foundation spec.
This package owns the **single-source enforcement points** for two of
the MIMARI §1 critical rules:

- **Kural 9 — banned MCP tool list (R1.8)** — `tool_filter.filter_tools`
  removes the canonical banned tools (`bitbucket_merge_pr`,
  `confluence_delete_page`) from any tool catalog handed to an LLM.
- **Kural 10 — PR draft enforcement (R1.9)** — `pr_draft.enforce_pr_draft`
  rewrites outgoing PR payloads so that `draft` is always `True`,
  regardless of what an LLM produced, and emits an audit
  `pr_draft_enforced` event when it had to flip a `False` (or absent)
  value.

The `atlassian_client` module is currently a thin skeleton. The real
HTTP wiring (Jira / Bitbucket / Confluence) is delivered by later
specs; the skeleton lives here in this spec only so that the
single-source enforcement tests have a stable import point.

## Public API

```python
from mcp_client import (
    BANNED_TOOLS,
    enforce_pr_draft,
    filter_tools,
)

# R1.8 — strip banned tools out of a tool catalog before exposing it
# to an LLM.
safe_tools = filter_tools(catalog)

# R1.9 — coerce ``draft`` to True on every outgoing PR payload and
# audit the cases where the caller intended otherwise.
safe_payload = enforce_pr_draft(
    payload,
    audit_logger=logger,
    actor_id="bot.payment.bitbucket",
    actor_role="system",
    dept_id="payment",
)
```

The audit dependency is optional — passing `audit_logger=None` keeps
`enforce_pr_draft` usable in tests / pure-function call paths. When a
logger is supplied, the function `await`s it and writes an
`AuditEvent` with `action="pr_draft_enforced"` only when the
enforcement actually changed the payload.

## Why a separate package?

`atlassian_unified` is treated as immutable in this spec (R2.4); we
cannot push enforcement helpers into it. `mcp_client` is therefore the
**outbound** wrapper used by `assistant-service`,
`agent-runner-worker`, and `automation-service` whenever they need to
call into Atlassian via the MCP. Keeping the enforcement code in one
shared lib means the property tests
(`test_tool_filter.py`, `test_pr_draft_enforcement.py` — task 2.8) can
assert the rule with a single import path rather than walking every
caller.

## Standalone build & run

```bash
# from libs/mcp_client/
python -m pip install --upgrade build
python -m build              # produces dist/mcp_client-*.whl

# install into a target environment
python -m pip install dist/mcp_client-*.whl
```
