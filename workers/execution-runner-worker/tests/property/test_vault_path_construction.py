"""Vault credential path construction.

For any valid department ID matching the schema pattern
``[a-z][a-z0-9-]{1,30}``, the :func:`build_vault_path` function returns a
path in the format ``atlassian/{dept_id}/bitbucket``.

This ensures the Credential_Injector always constructs the correct Vault
KV-v2 path for fetching Bitbucket credentials, regardless of the
department identifier provided.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.activities.credential_injector import build_vault_path


# Strategy: generate valid department IDs matching the schema pattern
# [a-z][a-z0-9-]{1,30} — starts with a lowercase letter, followed by
# 1-30 lowercase alphanumeric or hyphen characters.
dept_id_strategy = st.from_regex(r"[a-z][a-z0-9-]{1,30}", fullmatch=True)


@given(dept_id=dept_id_strategy)
def test_vault_path_follows_atlassian_dept_bitbucket_format(dept_id: str) -> None:
    """build_vault_path returns ``atlassian/{dept_id}/bitbucket``.

    For any valid dept_id matching the department schema pattern, the
    constructed Vault path must exactly equal the expected format with
    the dept_id interpolated between the ``atlassian/`` prefix and the
    ``/bitbucket`` suffix.
    """
    path = build_vault_path(dept_id)

    # The path must match the exact expected format
    assert path == f"atlassian/{dept_id}/bitbucket"

    # Structural invariants:
    # 1. Path starts with "atlassian/"
    assert path.startswith("atlassian/")

    # 2. Path ends with "/bitbucket"
    assert path.endswith("/bitbucket")

    # 3. The dept_id is correctly embedded between prefix and suffix
    prefix = "atlassian/"
    suffix = "/bitbucket"
    extracted_dept = path[len(prefix):-len(suffix)]
    assert extracted_dept == dept_id

    # 4. Path has exactly 3 segments separated by "/"
    segments = path.split("/")
    assert len(segments) == 3
    assert segments[0] == "atlassian"
    assert segments[1] == dept_id
    assert segments[2] == "bitbucket"
