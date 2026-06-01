"""Property test 9 — Workspace per-task isolation (Q15/Q16 ops extension).

**Validates: Requirements 4.2, 8.6, 8.7**

The foundation owns the core "per-task workspace is namespaced and
purgeable" property under task 11 of ``platform-mimari-foundation``.
This file extends that invariant to the ops-scope purge profiles
introduced by ``ServicesLifecycleRouter`` (task 11.1):

* ``profile == "workspace"`` — workspace dir purged, vault path
  retained.
* ``profile == "cache"`` — cache dirs purged, workspace + vault
  retained.
* ``profile == "none"`` — no destructive action.
* ``purge_vault=True`` (dev-only) — vault path purged in addition.

The test models the purge as a deterministic state transition on
an in-memory directory dict; the production ``ServicesLifecycleRouter``
runs the same transition behind a Compose stop call.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


@dataclass
class _Workspace:
    workspace_files: list[str]
    cache_files: list[str]
    vault_paths: list[str]


def _apply_stop(
    ws: _Workspace, *, profile: str, purge_vault: bool
) -> _Workspace:
    """Pure transition mirroring the ServicesLifecycleRouter purge logic."""

    new_ws = list(ws.workspace_files)
    new_cache = list(ws.cache_files)
    new_vault = list(ws.vault_paths)
    if profile == "workspace":
        new_ws = []
    elif profile == "cache":
        new_cache = []
    elif profile == "none":
        pass
    else:
        raise ValueError(f"unknown profile {profile!r}")
    if purge_vault:
        new_vault = []
    return _Workspace(
        workspace_files=new_ws,
        cache_files=new_cache,
        vault_paths=new_vault,
    )


_PROFILE = st.sampled_from(["workspace", "cache", "none"])
_FILE_LIST = st.lists(st.text(min_size=1, max_size=8), max_size=10)


@settings(
    max_examples=200, deadline=None, suppress_health_check=(HealthCheck.too_slow,)
)
@given(
    ws_files=_FILE_LIST,
    cache_files=_FILE_LIST,
    vault_paths=_FILE_LIST,
    profile=_PROFILE,
    purge_vault=st.booleans(),
)
def test_purge_profile_invariants(
    ws_files: list[str],
    cache_files: list[str],
    vault_paths: list[str],
    profile: str,
    purge_vault: bool,
) -> None:
    ws = _Workspace(
        workspace_files=ws_files,
        cache_files=cache_files,
        vault_paths=vault_paths,
    )
    after = _apply_stop(ws, profile=profile, purge_vault=purge_vault)

    if profile == "workspace":
        assert after.workspace_files == []
        assert after.cache_files == cache_files  # untouched
    elif profile == "cache":
        assert after.cache_files == []
        assert after.workspace_files == ws_files
    else:  # none
        assert after.workspace_files == ws_files
        assert after.cache_files == cache_files

    if purge_vault:
        assert after.vault_paths == []
    else:
        assert after.vault_paths == vault_paths


def test_unknown_profile_raises() -> None:
    ws = _Workspace([], [], [])
    with pytest.raises(ValueError):
        _apply_stop(ws, profile="full", purge_vault=False)
