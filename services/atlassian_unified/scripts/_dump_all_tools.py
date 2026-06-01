"""Dump every registered MCP tool grouped by service for inventory comparison."""

from __future__ import annotations

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

# Set every env var so the global lifespan exposes every service's tools.
os.environ.update(
    {
        "JIRA_URL": "https://acme.atlassian.net",
        "JIRA_USERNAME": "u@a.com",
        "JIRA_API_TOKEN": "x",
        "CONFLUENCE_URL": "https://acme.atlassian.net/wiki",
        "CONFLUENCE_USERNAME": "u@a.com",
        "CONFLUENCE_API_TOKEN": "x",
        "BITBUCKET_URL": "https://bb.acme.com",
        "BITBUCKET_PERSONAL_TOKEN": "x",
        "TOOLSETS": "all",
    }
)


async def main() -> None:
    from mcp_atlassian.servers.main import main_mcp

    tools = await main_mcp.get_tools()
    by_service: dict[str, list[str]] = {"jira": [], "confluence": [], "bitbucket": []}
    for name, t in tools.items():
        for svc in by_service:
            if svc in t.tags:
                # Strip mount prefix for readability
                clean = name.split("_", 1)[1] if name.startswith(f"{svc}_") else name
                by_service[svc].append(name)
                break

    for svc, names in by_service.items():
        print(f"\n=== {svc.upper()} ({len(names)} tools) ===")
        for n in sorted(names):
            print(f"  {n}")


asyncio.run(main())
