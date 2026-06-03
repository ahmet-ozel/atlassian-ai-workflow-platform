"""BITBUCKET_CREATE_PR_CLOUD importable from mcp_client.

This file contains an invariant that locks in the public import behavior:
``from mcp_client import BITBUCKET_CREATE_PR_CLOUD`` raises
``ImportError`` at collection time even though the constant exists in the
canonical submodule ``mcp_client.deployment_router``.

=============================================================================
CANONICAL SUBMODULE LOCATION
=============================================================================

``BITBUCKET_CREATE_PR_CLOUD`` is defined at:

 platform/libs/mcp_client/src/mcp_client/deployment_router.py (line 39)

 BITBUCKET_CREATE_PR_CLOUD: Final[str] = "bitbucket_create_pull_request_cloud"

It is NOT re-exported from:

 platform/libs/mcp_client/src/mcp_client/__init__.py

The ``__init__.py`` exports only:
 AtlassianClient, BANNED_TOOLS, EgressBlocked, FirecrawlClient,
 FirecrawlResult, FirecrawlSuccess, FirecrawlTransportError,
 PR_DRAFT_AUDIT_ACTION, PayloadOverflow, effective_allowlist,
 enforce_pr_draft, filter_tools

``BITBUCKET_CREATE_PR_CLOUD`` is absent from both the explicit imports and
the ``__all__`` list in ``__init__.py``.

=============================================================================
FAILURE MODE DOCUMENTATION
=============================================================================

Failure Mode A — package-level public surface:
----------------------------------------------------------------------
When run on UNFIXED code, the test fails with:

 AttributeError: module 'mcp_client' has no attribute 'BITBUCKET_CREATE_PR_CLOUD'

 OR (if the import is attempted directly):

 ImportError: cannot import name 'BITBUCKET_CREATE_PR_CLOUD' from 'mcp_client'

 The test checks the public import behavior:
 - importlib.import_module("mcp_client") succeeds (package loads fine)
 - getattr(mcp_client_pkg, "BITBUCKET_CREATE_PR_CLOUD") raises AttributeError
 (symbol not on the package surface)
 - mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD succeeds
 (symbol exists in the submodule)
 → The assertion that both accesses succeed AND yield the same object FAILS.

Failure Mode B — Collection-time error in test_deployment_router.py:
----------------------------------------------------------------------
When pytest collects platform/tests/unit/test_deployment_router.py on UNFIXED
code, it raises:

 ImportError: cannot import name 'BITBUCKET_CREATE_PR_CLOUD' from 'mcp_client'
 (platform/libs/mcp_client/src/mcp_client/__init__.py)

 This causes every test in test_deployment_router.py to be reported as
 ERRORED (not FAILED) before any test body runs. The collection-time error
 is confirmed by:

 pytest platform/tests/unit/test_deployment_router.py --collect-only

 which outputs:
 ERROR collecting platform/tests/unit/test_deployment_router.py
 ImportError: cannot import name 'BITBUCKET_CREATE_PR_CLOUD' from 'mcp_client'

=============================================================================
PUBLIC IMPORT CHECK
=============================================================================

The invariant below encodes this behavior via:
 1. importlib.import_module("mcp_client") — package-level access
 2. getattr(mcp_client_pkg, "BITBUCKET_CREATE_PR_CLOUD") — symbol on package surface
 3. mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD — submodule access
 4. Assert both (2) and (3) succeed AND yield the same object (``is`` identity)

On UNFIXED code: step (2) raises AttributeError → test FAILS (bug confirmed).
After fix: all steps succeed → test PASSES.

=============================================================================
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# invariant: BITBUCKET_CREATE_PR_CLOUD importable from mcp_client
#
# ---------------------------------------------------------------------------


def test_surface3_bitbucket_create_pr_cloud_reexported() -> None:
    """invariant: BITBUCKET_CREATE_PR_CLOUD importable from mcp_client.

 Asserts that:
 1. ``importlib.import_module("mcp_client")`` resolves (package loads).
 2. ``getattr(mcp_client_pkg, "BITBUCKET_CREATE_PR_CLOUD")`` succeeds
 (symbol is on the package public surface).
 3. ``mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD`` succeeds
 (symbol exists in the canonical submodule).
 4. Both accesses yield the same object (``is`` identity — the re-export
 points to the same constant, not a copy).

 On UNFIXED code: this test FAILS at step (2) with:
 AttributeError: module 'mcp_client' has no attribute 'BITBUCKET_CREATE_PR_CLOUD'

 This confirms the constant is in the submodule but NOT re-exported
 from ``mcp_client/__init__.py``.

 After fix — add re-export to __init__.py): this test PASSES.


 """
    # Step 1: Package-level import must succeed (the package itself is importable)
    mcp_client_pkg = importlib.import_module("mcp_client")
    assert mcp_client_pkg is not None, (
        "importlib.import_module('mcp_client') returned None — "
        "the mcp_client package is not installed or not on sys.path"
    )

    # Step 3: Submodule access must succeed (the constant exists in the submodule)
    # This is the "canonical" location confirmed by:
    # grep -r "BITBUCKET_CREATE_PR_CLOUD" platform/libs/mcp_client/
    # → platform/libs/mcp_client/src/mcp_client/deployment_router.py:39
    deployment_router = importlib.import_module("mcp_client.deployment_router")
    submodule_value = getattr(deployment_router, "BITBUCKET_CREATE_PR_CLOUD", None)
    assert submodule_value is not None, (
        "mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD is None or missing — "
        "the constant was removed from the canonical submodule. "
        "Expected: Final[str] = 'bitbucket_create_pull_request_cloud' "
        "at platform/libs/mcp_client/src/mcp_client/deployment_router.py:39"
    )

    # Step 2: Package-level access must succeed (the symbol is re-exported from __init__.py)
    # On UNFIXED code this raises AttributeError, confirming the missing export.
    package_value = getattr(mcp_client_pkg, "BITBUCKET_CREATE_PR_CLOUD", None)
    assert package_value is not None, (
        "BUG DETECTED: "
        "mcp_client.BITBUCKET_CREATE_PR_CLOUD is None or missing at the package surface. "
        "The constant exists in mcp_client.deployment_router "
        f"(value={submodule_value!r}) but is NOT re-exported from mcp_client/__init__.py. "
        " getattr(mcp_client, 'BITBUCKET_CREATE_PR_CLOUD') → AttributeError/None "
        " mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD → 'bitbucket_create_pull_request_cloud' "
        "Fix: add to mcp_client/__init__.py: "
        " from.deployment_router import BITBUCKET_CREATE_PR_CLOUD # re-export for downstream callers "
        " and append 'BITBUCKET_CREATE_PR_CLOUD' to __all__"
    )

    # Step 4: Both accesses must yield the same object (identity check)
    # This ensures the re-export is a direct reference, not a copy.
    assert package_value is submodule_value, (
        f"BUG DETECTED (identity mismatch): "
        f"mcp_client.BITBUCKET_CREATE_PR_CLOUD ({package_value!r}) is not the same "
        f"object as mcp_client.deployment_router.BITBUCKET_CREATE_PR_CLOUD "
        f"({submodule_value!r}). "
        f"The re-export must use 'from.deployment_router import BITBUCKET_CREATE_PR_CLOUD' "
        f"(direct re-export), not a copy or redefinition."
    )
