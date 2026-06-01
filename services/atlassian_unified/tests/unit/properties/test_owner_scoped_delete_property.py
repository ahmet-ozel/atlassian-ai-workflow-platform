"""Property test P13 — Owner-scoped delete uses mode-appropriate ownership identifier.

Validates Requirement 16.5 of the ``bitbucket-cloud-dc-parity`` spec /
design Property 13:

    For every Owner_Scoped_Delete tool and for every
    ``(object_owner, authenticated_user)`` pair where the mode-appropriate
    identifier of ``authenticated_user`` does not equal that of
    ``object_owner``, the tool SHALL return ``error_code == "not_owner"``
    with zero outbound write (DELETE) HTTP. The mode-appropriate identifier
    is ``BitbucketConfig.username`` (DC ``name``) in DCMode, and the Cloud
    ``account_id`` (via ``get_current_user_account_id()``) in CloudMode.

Test shape
----------
The test exercises :func:`mcp_atlassian.utils.dc_guards.require_owner`
directly against a shim that mirrors the Bitbucket webhook owner-scoped
delete path. This avoids spinning up the full FastMCP server while still
testing the real guard composition that the server tool would invoke.

* **Property A (Hypothesis, DC mode)** — for any ``(webhook_owner_username,
  authenticated_username)`` pair where the two differ (case-insensitive,
  whitespace-stripped), ``require_owner`` yields a :class:`StructuredError`
  with ``error_code == "not_owner"`` AND the mocked HTTP session records
  zero DELETE calls. The DC path uses ``config.username`` for comparison.

* **Property B (Hypothesis, Cloud mode)** — for any
  ``(webhook_owner_account_id, authenticated_account_id)`` pair where the
  two differ, ``require_owner`` yields ``error_code == "not_owner"`` AND
  the mocked HTTP session records zero DELETE calls. The Cloud path uses
  ``get_current_user_account_id()`` for comparison.

* **Property C (smoke, parametrized)** — when the owner and the
  authenticated user match in the mode-appropriate identifier, the path
  issues exactly one DELETE (plus one GET for owner resolution) and yields
  no structured error.

**Validates: Requirements 16.5**
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.utils import dc_guards


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# DC usernames: short ASCII identifiers compared case-insensitively.
_DC_USERNAME_ALPHABET: str = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789._-"
)

dc_usernames: st.SearchStrategy[str] = st.text(
    alphabet=_DC_USERNAME_ALPHABET,
    min_size=1,
    max_size=24,
)

# Cloud account IDs: UUID-shaped strings like "{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}"
# or colon-prefixed identifiers like "557058:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx".
_HEX_CHARS = "0123456789abcdef"


@st.composite
def _cloud_account_id(draw: st.DrawFn) -> str:
    """Generate a Cloud-style account_id (UUID in braces or colon-prefixed)."""
    style = draw(st.sampled_from(["uuid", "colon"]))
    hex_part = draw(st.text(alphabet=_HEX_CHARS, min_size=32, max_size=32))
    formatted = (
        f"{hex_part[:8]}-{hex_part[8:12]}-{hex_part[12:16]}"
        f"-{hex_part[16:20]}-{hex_part[20:32]}"
    )
    if style == "uuid":
        return f"{{{formatted}}}"
    else:
        prefix = draw(st.text(alphabet="0123456789", min_size=4, max_size=8))
        return f"{prefix}:{formatted}"


cloud_account_ids: st.SearchStrategy[str] = _cloud_account_id()


@st.composite
def _mismatched_dc_pair(draw: st.DrawFn) -> tuple[str, str]:
    """Draw two DC usernames that differ under require_owner's compare.

    ``require_owner`` strips whitespace and lower-cases both sides before
    equality is tested. We filter on that normalization to ensure every
    generated pair actually triggers the mismatch branch.
    """
    owner = draw(dc_usernames)
    auth_user = draw(dc_usernames)
    assume(owner.strip().lower() != auth_user.strip().lower())
    assume(owner.strip())
    assume(auth_user.strip())
    return owner, auth_user


@st.composite
def _mismatched_cloud_pair(draw: st.DrawFn) -> tuple[str, str]:
    """Draw two Cloud account_ids that differ.

    Cloud account IDs are compared case-insensitively and stripped by
    ``require_owner``. We ensure the two are distinct after normalization.
    """
    owner_id = draw(cloud_account_ids)
    auth_id = draw(cloud_account_ids)
    assume(owner_id.strip().lower() != auth_id.strip().lower())
    assume(owner_id.strip())
    assume(auth_id.strip())
    return owner_id, auth_id


# ---------------------------------------------------------------------------
# Shim helpers
# ---------------------------------------------------------------------------


def _build_get_webhook_side_effect(
    webhook_owner_id: str, webhook_id: int, *, is_cloud: bool, workspace: str = "ws"
) -> Any:
    """Return a side-effect for the GET that resolves webhook ownership.

    In DC mode, the webhook response includes a ``creator`` field with
    ``name`` (the DC username). In Cloud mode, the webhook response
    includes a ``creator`` field with ``account_id``.
    """
    if is_cloud:
        expected_path = f"/2.0/repositories/{workspace}/repo/hooks/{webhook_id}"
    else:
        expected_path = (
            f"/rest/api/latest/projects/PROJ/repos/repo/webhooks/{webhook_id}"
        )

    def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Return a webhook payload with the owner info.
        if is_cloud:
            return {
                "uuid": f"{{{webhook_id}}}",
                "description": "test hook",
                "url": "https://ci.example.com/hook",
                "active": True,
                "events": ["repo:push"],
                "creator": {
                    "account_id": webhook_owner_id,
                    "display_name": "Owner User",
                    "nickname": "owner",
                    "uuid": f"{{{webhook_owner_id}}}",
                },
            }
        else:
            return {
                "id": webhook_id,
                "name": "test hook",
                "url": "https://ci.example.com/hook",
                "active": True,
                "events": ["repo:refs_changed"],
                "configuration": {},
                "creator": {
                    "name": webhook_owner_id,
                    "displayName": "Owner User",
                    "slug": webhook_owner_id,
                },
            }

    return _get


def _make_fake_fetcher(
    mock_session: Any,
    *,
    is_cloud: bool,
    webhook_owner_id: str,
    authenticated_user: str,
    authenticated_account_id: str | None,
    webhook_id: int,
    workspace: str = "ws",
) -> SimpleNamespace:
    """Build a fetcher shim compatible with ``require_owner`` + webhook ops.

    Contracts satisfied:

    * ``require_owner`` reads ``fetcher.config.username`` for DC comparison.
    * ``require_owner`` calls ``fetcher.get_current_user_account_id()``
      for Cloud comparison.
    * The ``bitbucket.get`` / ``bitbucket.delete`` methods are wired to
      the mock session for HTTP call-count assertions.
    """
    mock_session.get.side_effect = _build_get_webhook_side_effect(
        webhook_owner_id, webhook_id, is_cloud=is_cloud, workspace=workspace
    )

    fake_fetcher = SimpleNamespace(
        is_cloud=is_cloud,
        config=SimpleNamespace(
            is_cloud=is_cloud,
            url=(
                "https://api.bitbucket.org"
                if is_cloud
                else "https://stash.corp.local"
            ),
            workspace=workspace if is_cloud else None,
            username=authenticated_user,
        ),
        bitbucket=SimpleNamespace(
            get=mock_session.get,
            delete=mock_session.delete,
        ),
    )

    # Cloud path: require_owner calls get_current_user_account_id()
    if authenticated_account_id is not None:
        fake_fetcher.get_current_user_account_id = (
            lambda: authenticated_account_id
        )

    return fake_fetcher


def _resolve_webhook_owner(
    fetcher: SimpleNamespace, webhook_id: int, *, project_key: str, repo_slug: str
) -> str:
    """Resolve the webhook owner by reading the webhook object.

    Mirrors the pattern a server tool would use: read the webhook first,
    extract the owner identifier from the ``creator`` field.
    """
    if fetcher.is_cloud:
        workspace = project_key or fetcher.config.workspace
        url = f"/2.0/repositories/{workspace}/{repo_slug}/hooks/{webhook_id}"
    else:
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/webhooks/{webhook_id}"
        )

    webhook = fetcher.bitbucket.get(url)

    creator = webhook.get("creator", {})
    if fetcher.is_cloud:
        # Cloud uses account_id as the ownership identifier (Req 16.5)
        return creator.get("account_id", "")
    else:
        # DC uses username (name) as the ownership identifier
        return creator.get("name", "")


def _run_owner_scoped_webhook_delete(
    fetcher: SimpleNamespace,
    webhook_id: int,
    *,
    project_key: str = "PROJ",
    repo_slug: str = "repo",
) -> dc_guards.StructuredError | None:
    """Reproduce the owner-scoped webhook delete path.

    Mirrors the call order that a server tool would use:
    1. Resolve webhook owner via a read endpoint
    2. Call require_owner to compare against authenticated user
    3. Only issue DELETE if ownership matches

    Returns:
        ``None`` on the happy path (DELETE issued).
        A :class:`StructuredError` with ``error_code="not_owner"``
        on the owner-mismatch branch (zero DELETE issued).
    """
    # 1. Resolve webhook owner (single read-side HTTP call).
    owner_id = _resolve_webhook_owner(
        fetcher, webhook_id, project_key=project_key, repo_slug=repo_slug
    )

    # 2. Owner gate.
    owner_err = dc_guards.require_owner(fetcher, owner_id)
    if owner_err is not None:
        return owner_err

    # 3. Issue the DELETE only after the owner gate passes.
    if fetcher.is_cloud:
        workspace = project_key or fetcher.config.workspace
        delete_url = (
            f"/2.0/repositories/{workspace}/{repo_slug}/hooks/{webhook_id}"
        )
    else:
        delete_url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/webhooks/{webhook_id}"
        )
    fetcher.bitbucket.delete(delete_url)
    return None


# ---------------------------------------------------------------------------
# Property A — DC mode: mismatch on username yields not_owner, zero DELETEs
# ---------------------------------------------------------------------------


@given(
    pair=_mismatched_dc_pair(),
    webhook_id=st.integers(min_value=1, max_value=999999),
)
def test_dc_mode_owner_mismatch_blocks_delete_with_not_owner(
    mock_requests_session, pair: tuple[str, str], webhook_id: int
) -> None:
    """P13.A: DC mode — owner username ≠ authenticated username → ``not_owner`` + zero DELETE.

    In DC mode, ``require_owner`` compares ``config.username`` against the
    webhook creator's ``name`` field. When they differ, the guard returns
    ``not_owner`` and no DELETE is issued.

    **Validates: Requirements 16.5**
    """
    mock_requests_session.reset_mock(side_effect=True)

    webhook_owner, authenticated_user = pair
    fetcher = _make_fake_fetcher(
        mock_requests_session,
        is_cloud=False,
        webhook_owner_id=webhook_owner,
        authenticated_user=authenticated_user,
        authenticated_account_id=None,
        webhook_id=webhook_id,
    )

    result = _run_owner_scoped_webhook_delete(fetcher, webhook_id)

    # Structured-error contract.
    assert isinstance(result, dc_guards.StructuredError), (
        f"expected StructuredError on DC owner mismatch, got {result!r}"
    )
    assert result.error_code == "not_owner", (
        f"expected error_code='not_owner', got {result.error_code!r}"
    )
    assert result.details.get("object_owner_id") == webhook_owner
    assert result.details.get("authenticated_user") == authenticated_user

    # Zero DELETE contract — the core safety property.
    assert mock_requests_session.delete.call_count == 0, (
        f"expected zero DELETE calls on DC owner mismatch, got "
        f"{mock_requests_session.delete.call_count}"
    )
    # Exactly one GET (webhook owner lookup).
    assert mock_requests_session.get.call_count == 1


# ---------------------------------------------------------------------------
# Property B — Cloud mode: mismatch on account_id yields not_owner, zero DELETEs
# ---------------------------------------------------------------------------


@given(
    pair=_mismatched_cloud_pair(),
    webhook_id=st.integers(min_value=1, max_value=999999),
)
def test_cloud_mode_owner_mismatch_blocks_delete_with_not_owner(
    mock_requests_session, pair: tuple[str, str], webhook_id: int
) -> None:
    """P13.B: Cloud mode — owner account_id ≠ authenticated account_id → ``not_owner`` + zero DELETE.

    In Cloud mode, ``require_owner`` calls ``get_current_user_account_id()``
    and compares the result against the webhook creator's ``account_id``
    field. When they differ, the guard returns ``not_owner`` and no DELETE
    is issued.

    **Validates: Requirements 16.5**
    """
    mock_requests_session.reset_mock(side_effect=True)

    webhook_owner_account_id, authenticated_account_id = pair
    fetcher = _make_fake_fetcher(
        mock_requests_session,
        is_cloud=True,
        webhook_owner_id=webhook_owner_account_id,
        # DC username is intentionally set to something that won't match
        # the Cloud account_id — proving Cloud uses account_id, not username.
        authenticated_user="dc_user_irrelevant",
        authenticated_account_id=authenticated_account_id,
        webhook_id=webhook_id,
    )

    result = _run_owner_scoped_webhook_delete(
        fetcher, webhook_id, project_key="ws"
    )

    # Structured-error contract.
    assert isinstance(result, dc_guards.StructuredError), (
        f"expected StructuredError on Cloud owner mismatch, got {result!r}"
    )
    assert result.error_code == "not_owner", (
        f"expected error_code='not_owner', got {result.error_code!r}"
    )
    assert result.details.get("object_owner_id") == webhook_owner_account_id

    # Zero DELETE contract — the core safety property.
    assert mock_requests_session.delete.call_count == 0, (
        f"expected zero DELETE calls on Cloud owner mismatch, got "
        f"{mock_requests_session.delete.call_count}"
    )
    # Exactly one GET (webhook owner lookup).
    assert mock_requests_session.get.call_count == 1


# ---------------------------------------------------------------------------
# Property C (smoke) — matching owner proceeds to exactly one DELETE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_cloud", "owner_id", "auth_user", "auth_account_id"),
    [
        # DC mode: username match (case-insensitive)
        (False, "alice", "alice", None),
        (False, "BOB", "bob", None),
        # Cloud mode: account_id match
        (True, "{12345678-1234-1234-1234-123456789abc}", "irrelevant", "{12345678-1234-1234-1234-123456789abc}"),
        (True, "557058:abcdef12-3456-7890-abcd-ef1234567890", "irrelevant", "557058:abcdef12-3456-7890-abcd-ef1234567890"),
    ],
    ids=["dc-exact", "dc-case-insensitive", "cloud-uuid", "cloud-colon-prefix"],
)
def test_matching_owner_issues_exactly_one_delete(
    mock_requests_session,
    is_cloud: bool,
    owner_id: str,
    auth_user: str,
    auth_account_id: str | None,
) -> None:
    """P13.C: owner matches authenticated user → one GET + one DELETE, no error.

    **Validates: Requirements 16.5**
    """
    webhook_id = 42
    fetcher = _make_fake_fetcher(
        mock_requests_session,
        is_cloud=is_cloud,
        webhook_owner_id=owner_id,
        authenticated_user=auth_user,
        authenticated_account_id=auth_account_id,
        webhook_id=webhook_id,
    )

    result = _run_owner_scoped_webhook_delete(
        fetcher, webhook_id, project_key=("ws" if is_cloud else "PROJ")
    )

    assert result is None, f"expected happy-path (no error), got {result!r}"

    # Exactly one GET (owner lookup) + exactly one DELETE.
    assert mock_requests_session.get.call_count == 1
    assert mock_requests_session.delete.call_count == 1
