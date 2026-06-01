"""
Dynamic Confluence space discovery helper.

Implements R22 fix: instead of hardcoding a space key, this module
discovers available spaces dynamically or creates a test space.
"""

from __future__ import annotations

from typing import Optional
import httpx


class ConfluenceSpaceHelper:
    """Discovers or creates Confluence spaces dynamically."""

    def __init__(self, base_url: str, username: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, api_token)

    def discover_space(self) -> Optional[str]:
        """Find an available space key dynamically.

        Strategy:
        1. List all spaces the user has access to
        2. Return the first available space key
        3. If no spaces exist, attempt to create a test space

        Returns
        -------
        str or None
            The space key to use, or None if no space is available.
        """
        # Try to list available spaces
        spaces = self._list_spaces()
        if spaces:
            return spaces[0]

        # Try to create a test space
        created = self._create_test_space()
        if created:
            return created

        return None

    def _list_spaces(self) -> list[str]:
        """List available Confluence space keys."""
        try:
            url = f"{self.base_url}/wiki/rest/api/space"
            resp = httpx.get(
                url,
                auth=self.auth,
                params={"limit": 10, "type": "global"},
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                return [s["key"] for s in results if "key" in s]
        except (httpx.HTTPError, KeyError, ValueError):
            pass
        return []

    def _create_test_space(self) -> Optional[str]:
        """Attempt to create a test space for E2E testing."""
        try:
            url = f"{self.base_url}/wiki/rest/api/space"
            payload = {
                "key": "E2ETEST",
                "name": "E2E Test Space",
                "description": {
                    "plain": {
                        "value": "Automated test space for E2E testing",
                        "representation": "plain",
                    }
                },
            }
            resp = httpx.post(
                url,
                auth=self.auth,
                json=payload,
                timeout=15.0,
            )
            if resp.status_code in (200, 201):
                return "E2ETEST"
        except (httpx.HTTPError, ValueError):
            pass
        return None
