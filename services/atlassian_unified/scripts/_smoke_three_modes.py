"""Empirical smoke test of the three deployment modes.

Run as: python scripts/_smoke_three_modes.py

Mode 1 — IDE/stdio with env vars (single-tenant local)
Mode 2 — server with env vars (single-tenant HTTP)
Mode 3 — server without env vars (multi-user, per-request creds)
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager

# Ensure the package is importable when run from repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


SERVICE_ENV_VARS = (
    "JIRA_URL",
    "JIRA_USERNAME",
    "JIRA_API_TOKEN",
    "JIRA_PERSONAL_TOKEN",
    "CONFLUENCE_URL",
    "CONFLUENCE_USERNAME",
    "CONFLUENCE_API_TOKEN",
    "CONFLUENCE_PERSONAL_TOKEN",
    "BITBUCKET_URL",
    "BITBUCKET_PERSONAL_TOKEN",
    "BITBUCKET_USERNAME",
    "BITBUCKET_PASSWORD",
    "ATLASSIAN_OAUTH_ENABLE",
    "ATLASSIAN_OAUTH_CLIENT_ID",
    "ATLASSIAN_OAUTH_CLIENT_SECRET",
    "ATLASSIAN_OAUTH_CLOUD_ID",
    "ATLASSIAN_OAUTH_ACCESS_TOKEN",
)


@contextmanager
def env(**values: str | None):
    """Apply env values, clearing variables we want absent (None)."""
    saved: dict[str, str | None] = {}
    for k in SERVICE_ENV_VARS:
        saved[k] = os.environ.get(k)
    try:
        for k in SERVICE_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k in SERVICE_ENV_VARS:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]


async def collect_tools(headers: dict[str, str] | None = None) -> dict[str, int]:
    from mcp_atlassian.servers.main import main_mcp, main_lifespan
    from mcp_atlassian.utils.environment import get_available_services

    async with main_lifespan(main_mcp) as ctx:
        s = ctx["app_lifespan_context"]
        all_tools = await main_mcp.get_tools()

        # Replicate the per-request filtering (header_based_services + global config)
        header_services = get_available_services(headers or {})
        result: dict[str, int] = {"jira": 0, "confluence": 0, "bitbucket": 0}

        for tool in all_tools.values():
            tags = tool.tags
            for svc in result:
                if svc in tags:
                    global_ok = getattr(s, f"full_{svc}_config", None) is not None
                    header_ok = header_services.get(svc, False)
                    if global_ok or header_ok:
                        result[svc] += 1

        return {
            "global_jira": s.full_jira_config is not None,
            "global_confluence": s.full_confluence_config is not None,
            "global_bitbucket": s.full_bitbucket_config is not None,
            "header_services": header_services,
            "exposed_tools": result,
            "total_registered": len(all_tools),
        }


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


async def main() -> int:
    banner("MODE 1: IDE / stdio with env vars (single-tenant)")
    with env(
        JIRA_URL="https://acme.atlassian.net",
        JIRA_USERNAME="user@acme.com",
        JIRA_API_TOKEN="fake-jira",
        CONFLUENCE_URL="https://acme.atlassian.net/wiki",
        CONFLUENCE_USERNAME="user@acme.com",
        CONFLUENCE_API_TOKEN="fake-conf",
        BITBUCKET_URL="https://bb.acme.com",
        BITBUCKET_PERSONAL_TOKEN="fake-bb",
    ):
        info = await collect_tools()
        for k, v in info.items():
            print(f"  {k}: {v}")
        ok1 = (
            info["global_jira"]
            and info["global_confluence"]
            and info["global_bitbucket"]
            and info["exposed_tools"]["jira"] > 0
            and info["exposed_tools"]["confluence"] > 0
            and info["exposed_tools"]["bitbucket"] > 0
        )
        print(f"  PASS: {ok1}")

    banner("MODE 2: HTTP server with env vars (single-tenant, Server/DC PATs)")
    # PAT is a Server/Data Center auth mechanism — use non-cloud URLs so the
    # detection logic in get_available_services() recognises them.
    with env(
        JIRA_URL="https://jira.acme.corp",
        JIRA_PERSONAL_TOKEN="fake-jira-pat",
        CONFLUENCE_URL="https://confluence.acme.corp",
        CONFLUENCE_PERSONAL_TOKEN="fake-conf-pat",
        BITBUCKET_URL="https://bb.acme.corp",
        BITBUCKET_PERSONAL_TOKEN="fake-bb-pat",
    ):
        info = await collect_tools()
        for k, v in info.items():
            print(f"  {k}: {v}")
        ok2 = (
            info["global_jira"]
            and info["global_confluence"]
            and info["global_bitbucket"]
            and info["exposed_tools"]["jira"] > 0
            and info["exposed_tools"]["confluence"] > 0
            and info["exposed_tools"]["bitbucket"] > 0
        )
        print(f"  PASS: {ok2}")

    banner("MODE 3a: HTTP server with NO env vars + NO request headers")
    with env():
        info = await collect_tools(headers=None)
        for k, v in info.items():
            print(f"  {k}: {v}")
        ok3a = (
            not info["global_jira"]
            and not info["global_confluence"]
            and not info["global_bitbucket"]
            and info["exposed_tools"]["jira"] == 0
            and info["exposed_tools"]["confluence"] == 0
            and info["exposed_tools"]["bitbucket"] == 0
        )
        print(f"  PASS: {ok3a}  (no creds anywhere -> no service tools exposed)")

    banner("MODE 3b: HTTP server NO env, request brings X-Atlassian-Bitbucket-* headers")
    with env():
        headers = {
            "X-Atlassian-Bitbucket-Url": "https://bb.acme.com",
            "X-Atlassian-Bitbucket-Personal-Token": "user-bb-pat",
        }
        info = await collect_tools(headers=headers)
        for k, v in info.items():
            print(f"  {k}: {v}")
        ok3b = (
            not info["global_bitbucket"]
            and info["header_services"]["bitbucket"]
            and info["exposed_tools"]["bitbucket"] > 0
            and info["exposed_tools"]["jira"] == 0
            and info["exposed_tools"]["confluence"] == 0
        )
        print(f"  PASS: {ok3b}  (only Bitbucket headers -> only Bitbucket tools exposed)")

    banner("MODE 3c: HTTP server NO env, request brings ALL THREE service header sets")
    with env():
        headers = {
            "X-Atlassian-Jira-Url": "https://acme.atlassian.net",
            "X-Atlassian-Jira-Personal-Token": "user-jira-pat",
            "X-Atlassian-Confluence-Url": "https://acme.atlassian.net/wiki",
            "X-Atlassian-Confluence-Personal-Token": "user-conf-pat",
            "X-Atlassian-Bitbucket-Url": "https://bb.acme.com",
            "X-Atlassian-Bitbucket-Personal-Token": "user-bb-pat",
        }
        info = await collect_tools(headers=headers)
        for k, v in info.items():
            print(f"  {k}: {v}")
        ok3c = (
            info["header_services"]["jira"]
            and info["header_services"]["confluence"]
            and info["header_services"]["bitbucket"]
            and info["exposed_tools"]["jira"] > 0
            and info["exposed_tools"]["confluence"] > 0
            and info["exposed_tools"]["bitbucket"] > 0
        )
        print(f"  PASS: {ok3c}  (all 3 services available via per-request headers)")

    banner("VERDICT")
    all_ok = ok1 and ok2 and ok3a and ok3b and ok3c
    print(f"  Mode 1 (IDE/stdio + env):                {'PASS' if ok1 else 'FAIL'}")
    print(f"  Mode 2 (HTTP server + env):              {'PASS' if ok2 else 'FAIL'}")
    print(f"  Mode 3a (HTTP no env, no headers):       {'PASS' if ok3a else 'FAIL'}")
    print(f"  Mode 3b (HTTP no env, BB headers only):  {'PASS' if ok3b else 'FAIL'}")
    print(f"  Mode 3c (HTTP no env, all 3 headers):    {'PASS' if ok3c else 'FAIL'}")
    print(f"\n  Overall: {'ALL MODES PASS' if all_ok else 'SOMETHING FAILED'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
