"""Property test P6 — Normalizer round-trip preserves identity fields.

Validates Requirements 21.1, 21.2, 21.3, 21.4, 21.5 / design Property 6:

    *For any* Cloud user payload ``u``,
    ``normalize_user(u)["account_id"] == u["account_id"]``.

    *For any* DC user payload ``u``,
    ``normalize_user(u)["slug"] == u["slug"]``.

    *For any* repository payload ``r`` in either mode, the normalized
    output ``slug`` equals the input ``slug``.

    *For any* pull-request payload ``p`` in either mode, the normalized
    output ``id`` equals the input ``id``.

The normalizer is deliberately **total and pure**: DC-shaped inputs are
returned as identity (no re-keying), while Cloud-shaped inputs gain
DC-shaped alias keys without losing the Cloud originals. This property
test exercises both paths over randomly-generated payloads so the
identity-preservation contract can't regress silently under partial
payloads, unusual unicode, or extra-key passthrough.

**Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5**

Testing strategy
----------------
We generate four shape families with Hypothesis composite strategies:

1. Cloud users — dicts carrying ``account_id`` plus optional
   ``display_name``, ``uuid``, ``links``, ``type``, and nickname text.
2. DC users — dicts carrying ``slug`` (and commonly ``name`` /
   ``displayName``) and deliberately NOT ``account_id`` so the
   normalizer recognizes them as DC-shaped.
3. Repositories — both Cloud shape (``workspace``, ``full_name``,
   ``uuid``) and DC shape (``project``, ``scmId``) share the ``slug``
   key which must round-trip under ``normalize_repository``.
4. Pull requests — both Cloud shape (``source``/``destination``,
   ``created_on``/``updated_on``) and DC shape (``fromRef``/``toRef``,
   ``createdDate``/``updatedDate``) share the integer ``id`` key which
   must round-trip under ``normalize_pull_request``.

For every generated payload we assert the identity field is preserved
bit-for-bit after the normalizer runs. DC-shaped inputs additionally
check the identity-transform contract (output IS the input dict), and
Cloud-shaped inputs check that the DC alias keys appear populated with
the Cloud identity value.
"""

from __future__ import annotations

import string
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.response_normalizer import (
    normalize_pull_request,
    normalize_repository,
    normalize_user,
)

# ---------------------------------------------------------------------------
# Shared primitive strategies
# ---------------------------------------------------------------------------

# ``account_id`` on Cloud is an opaque identifier — either a UUID wrapped
# in braces (``{deadbeef-...}``) or a base64url-like token. We draw from a
# constrained alphabet that exercises both shapes without dwelling on
# pathological unicode (the normalizer never parses the value).
_ACCOUNT_ID_ALPHABET = string.ascii_letters + string.digits + ":-_"
account_ids: st.SearchStrategy[str] = st.text(
    alphabet=_ACCOUNT_ID_ALPHABET,
    min_size=1,
    max_size=32,
)

# User slugs on DC are lowercase ASCII words with dots, dashes, and
# underscores. Keeping the alphabet narrow avoids conflating "is the
# slug round-tripped" with "does the dict accept unusual keys".
_SLUG_ALPHABET = string.ascii_lowercase + string.digits + ".-_"
user_slugs: st.SearchStrategy[str] = st.text(
    alphabet=_SLUG_ALPHABET,
    min_size=1,
    max_size=20,
)

# Repository slugs follow the same shape as user slugs on both products.
repo_slugs: st.SearchStrategy[str] = user_slugs

# PR ids are positive integers on both Cloud and DC. Cloud caps at roughly
# 2^31, so an upper bound of 10_000_000 is more than sufficient.
pr_ids: st.SearchStrategy[int] = st.integers(min_value=1, max_value=10_000_000)

# Display names can contain arbitrary unicode; we exercise a small but
# varied alphabet so the round-trip contract is tested against non-ASCII.
display_names: st.SearchStrategy[str] = st.text(min_size=0, max_size=30)

# UUIDs on Cloud are ``{hexhex-...}`` — we use a simple 8-char hex stand-in
# wrapped in braces since the normalizer treats the value as opaque.
_HEX = "0123456789abcdef"
uuid_strings: st.SearchStrategy[str] = st.builds(
    lambda body: "{" + body + "}",
    st.text(alphabet=_HEX, min_size=8, max_size=36),
)

# Workspace slugs for Cloud repositories.
workspace_slugs: st.SearchStrategy[str] = st.text(
    alphabet=_SLUG_ALPHABET,
    min_size=1,
    max_size=15,
)

# Branch names for ref synthesis.
branch_names: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "/_-.",
    min_size=1,
    max_size=20,
)

# Commit hashes for PR source/destination.
commit_hashes: st.SearchStrategy[str] = st.text(
    alphabet=_HEX,
    min_size=7,
    max_size=40,
)

# ISO 8601 timestamps (Cloud) — a fixed pool keeps the test focused on
# the identity-preservation invariant rather than date parsing.
iso_timestamps: st.SearchStrategy[str] = st.sampled_from(
    (
        "2024-01-15T10:30:00.000000+00:00",
        "2023-06-01T08:15:42.123456Z",
        "2025-12-31T23:59:59+00:00",
        "2022-03-14T15:09:26.535897Z",
    )
)

# Epoch milliseconds (DC) — matching shape family.
epoch_millis: st.SearchStrategy[int] = st.integers(
    min_value=1_000_000_000_000,
    max_value=2_000_000_000_000,
)


# ---------------------------------------------------------------------------
# Cloud / DC user strategies
# ---------------------------------------------------------------------------


@st.composite
def cloud_users(draw: st.DrawFn) -> dict[str, Any]:
    """Build a Cloud-shape user dict carrying ``account_id`` (Req 21.1).

    Cloud users ALWAYS carry ``account_id``; this is the shape-detection
    key used by :func:`normalize_user`. We optionally include
    ``display_name``, ``uuid``, ``nickname``, ``links`` to exercise the
    passthrough branch of the normalizer.
    """
    user: dict[str, Any] = {
        "account_id": draw(account_ids),
        "type": "user",
    }
    if draw(st.booleans()):
        user["display_name"] = draw(display_names)
    if draw(st.booleans()):
        user["uuid"] = draw(uuid_strings)
    if draw(st.booleans()):
        user["nickname"] = draw(
            st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=12)
        )
    if draw(st.booleans()):
        user["links"] = {"self": {"href": "https://api.bitbucket.org/2.0/users/x"}}
    return user


@st.composite
def dc_users(draw: st.DrawFn) -> dict[str, Any]:
    """Build a DC-shape user dict carrying ``slug`` (Req 21.2).

    DC users NEVER carry ``account_id`` — the absence of that key is what
    causes :func:`normalize_user` to take the identity branch.
    """
    slug = draw(user_slugs)
    user: dict[str, Any] = {
        "slug": slug,
        "name": slug,
        "type": "NORMAL",
    }
    if draw(st.booleans()):
        user["displayName"] = draw(display_names)
    if draw(st.booleans()):
        user["id"] = draw(st.integers(min_value=1, max_value=100_000))
    if draw(st.booleans()):
        user["emailAddress"] = f"{slug}@example.com"
    if draw(st.booleans()):
        user["active"] = draw(st.booleans())
    return user


# ---------------------------------------------------------------------------
# Cloud / DC repository strategies
# ---------------------------------------------------------------------------


@st.composite
def cloud_repositories(draw: st.DrawFn) -> tuple[dict[str, Any], str]:
    """Build a Cloud-shape repository dict plus the workspace to pass in.

    Returns ``(repo_dict, workspace)`` so the caller can thread the
    workspace into :func:`normalize_repository`. Cloud repos carry
    ``workspace`` / ``full_name`` (shape-detection keys) and the shared
    ``slug`` that must be preserved.
    """
    workspace = draw(workspace_slugs)
    slug = draw(repo_slugs)
    repo: dict[str, Any] = {
        "slug": slug,
        "full_name": f"{workspace}/{slug}",
        "workspace": {"slug": workspace, "name": workspace},
        "scm": "git",
    }
    if draw(st.booleans()):
        repo["uuid"] = draw(uuid_strings)
    if draw(st.booleans()):
        repo["name"] = slug
    if draw(st.booleans()):
        repo["is_private"] = draw(st.booleans())
    if draw(st.booleans()):
        repo["links"] = {"self": {"href": f"https://api.bitbucket.org/2.0/repositories/{workspace}/{slug}"}}
    return repo, workspace


@st.composite
def dc_repositories(draw: st.DrawFn) -> dict[str, Any]:
    """Build a DC-shape repository dict.

    DC repos carry ``project`` (shape-detection key) and NEVER carry
    ``workspace`` / ``full_name``. The ``slug`` must round-trip under
    identity.
    """
    project_key = draw(
        st.text(
            alphabet=string.ascii_uppercase + string.digits,
            min_size=2,
            max_size=10,
        )
    )
    slug = draw(repo_slugs)
    repo: dict[str, Any] = {
        "slug": slug,
        "name": slug,
        "scmId": "git",
        "project": {"key": project_key, "name": project_key.title()},
    }
    if draw(st.booleans()):
        repo["id"] = draw(st.integers(min_value=1, max_value=100_000))
    if draw(st.booleans()):
        repo["public"] = draw(st.booleans())
    if draw(st.booleans()):
        repo["state"] = "AVAILABLE"
    return repo


# ---------------------------------------------------------------------------
# Cloud / DC pull-request strategies
# ---------------------------------------------------------------------------


@st.composite
def cloud_pull_requests(draw: st.DrawFn) -> dict[str, Any]:
    """Build a Cloud-shape pull-request dict carrying integer ``id``.

    Cloud PRs carry ``source`` / ``destination`` (shape-detection keys)
    and ISO-8601 ``created_on`` / ``updated_on``. The ``id`` is the
    round-trip identity field.
    """
    from_branch = draw(branch_names)
    to_branch = draw(branch_names)
    pr: dict[str, Any] = {
        "id": draw(pr_ids),
        "title": draw(st.text(min_size=0, max_size=40)),
        "state": draw(st.sampled_from(("OPEN", "MERGED", "DECLINED", "SUPERSEDED"))),
        "source": {
            "branch": {"name": from_branch},
            "commit": {"hash": draw(commit_hashes)},
        },
        "destination": {
            "branch": {"name": to_branch},
            "commit": {"hash": draw(commit_hashes)},
        },
        "author": draw(cloud_users()),
    }
    if draw(st.booleans()):
        pr["created_on"] = draw(iso_timestamps)
    if draw(st.booleans()):
        pr["updated_on"] = draw(iso_timestamps)
    if draw(st.booleans()):
        pr["reviewers"] = draw(st.lists(cloud_users(), min_size=0, max_size=3))
    return pr


@st.composite
def dc_pull_requests(draw: st.DrawFn) -> dict[str, Any]:
    """Build a DC-shape pull-request dict carrying integer ``id``.

    DC PRs carry ``fromRef`` / ``toRef`` (shape-detection keys) and
    NEVER carry ``source`` / ``destination`` / ``created_on`` /
    ``updated_on`` (those are Cloud-only shape markers).
    """
    from_branch = draw(branch_names)
    to_branch = draw(branch_names)
    pr: dict[str, Any] = {
        "id": draw(pr_ids),
        "title": draw(st.text(min_size=0, max_size=40)),
        "state": draw(st.sampled_from(("OPEN", "MERGED", "DECLINED"))),
        "fromRef": {
            "id": f"refs/heads/{from_branch}",
            "displayId": from_branch,
            "latestCommit": draw(commit_hashes),
        },
        "toRef": {
            "id": f"refs/heads/{to_branch}",
            "displayId": to_branch,
            "latestCommit": draw(commit_hashes),
        },
        "author": {"user": draw(dc_users())},
    }
    if draw(st.booleans()):
        pr["createdDate"] = draw(epoch_millis)
    if draw(st.booleans()):
        pr["updatedDate"] = draw(epoch_millis)
    if draw(st.booleans()):
        pr["reviewers"] = [
            {"user": draw(dc_users())}
            for _ in range(draw(st.integers(min_value=0, max_value=3)))
        ]
    return pr


# ---------------------------------------------------------------------------
# Property A — Cloud user round-trips account_id  (Requirement 21.1)
# ---------------------------------------------------------------------------


@given(user=cloud_users())
def test_cloud_user_roundtrips_account_id(user: dict[str, Any]) -> None:
    """For any Cloud user ``u``, ``normalize_user(u)["account_id"] == u["account_id"]``.

    Validates: Requirements 21.1
    """
    normalized = normalize_user(user)

    # The normalizer must not return ``None`` for a valid Cloud payload.
    assert normalized is not None
    # The account_id round-trips bit-for-bit.
    assert "account_id" in normalized
    assert normalized["account_id"] == user["account_id"]
    # The DC-shaped alias keys are populated with the same value so
    # downstream code that reads ``slug`` / ``name`` on a normalized
    # Cloud user sees the account_id identity.
    assert normalized.get("name") == user["account_id"]
    assert normalized.get("slug") == user["account_id"]


# ---------------------------------------------------------------------------
# Property B — DC user normalizer is identity on slug  (Requirement 21.2)
# ---------------------------------------------------------------------------


@given(user=dc_users())
def test_dc_user_normalizer_preserves_slug(user: dict[str, Any]) -> None:
    """For any DC user ``u``, ``normalize_user(u)["slug"] == u["slug"]``.

    On DC-shaped input, the normalizer returns the input dict unchanged
    (identity branch). This test pins both facets:

    * The ``slug`` field survives bit-for-bit.
    * The normalizer is an identity transform — no Cloud alias keys
      (``account_id``) sneak in, since they would mis-shape downstream
      DC-mode code paths.

    Validates: Requirements 21.2
    """
    normalized = normalize_user(user)

    assert normalized is not None
    assert normalized["slug"] == user["slug"]
    # Identity: DC inputs never gain ``account_id`` (that key is the
    # shape-detection marker for Cloud and must stay absent on DC).
    assert "account_id" not in normalized
    # Identity on DC inputs is exact — the returned object IS the input.
    assert normalized is user


# ---------------------------------------------------------------------------
# Property C — Repository slug round-trips in either mode  (Requirement 21.3)
# ---------------------------------------------------------------------------


@given(cloud=cloud_repositories())
def test_cloud_repository_roundtrips_slug(cloud: tuple[dict[str, Any], str]) -> None:
    """Cloud repo ``slug`` survives :func:`normalize_repository`.

    Validates: Requirements 21.3 (Cloud branch)
    """
    repo, workspace = cloud
    normalized = normalize_repository(repo, workspace=workspace)

    assert normalized is not None
    assert normalized["slug"] == repo["slug"]


@given(repo=dc_repositories())
def test_dc_repository_roundtrips_slug(repo: dict[str, Any]) -> None:
    """DC repo ``slug`` survives :func:`normalize_repository` (identity branch).

    Validates: Requirements 21.3 (DC branch)
    """
    normalized = normalize_repository(repo, workspace=None)

    assert normalized is not None
    assert normalized["slug"] == repo["slug"]
    # Identity transform on DC shape — object returned as-is.
    assert normalized is repo


# ---------------------------------------------------------------------------
# Property D — Pull request id round-trips in either mode  (Requirement 21.4)
# ---------------------------------------------------------------------------


@given(pr=cloud_pull_requests())
def test_cloud_pull_request_roundtrips_id(pr: dict[str, Any]) -> None:
    """Cloud PR ``id`` survives :func:`normalize_pull_request`.

    Validates: Requirements 21.4 (Cloud branch)
    """
    normalized = normalize_pull_request(pr)

    assert normalized is not None
    assert normalized["id"] == pr["id"]


@given(pr=dc_pull_requests())
def test_dc_pull_request_roundtrips_id(pr: dict[str, Any]) -> None:
    """DC PR ``id`` survives :func:`normalize_pull_request` (identity branch).

    Validates: Requirements 21.4 (DC branch)
    """
    normalized = normalize_pull_request(pr)

    assert normalized is not None
    assert normalized["id"] == pr["id"]
    # Identity transform on DC shape — object returned as-is.
    assert normalized is pr


# ---------------------------------------------------------------------------
# Property E — Combined round-trip across the four identity fields.
# ---------------------------------------------------------------------------
#
# Requirement 21.5 mandates the presence of a single property-based test
# covering criteria 21.1–21.4. The four ``test_*_roundtrips_*`` functions
# above each isolate one criterion for pinpoint diagnostics; this
# combined test closes 21.5 by asserting all four identity fields
# survive inside a single Hypothesis example, catching any cross-
# cutting regression where two normalizers share state.


@given(
    cloud_user=cloud_users(),
    dc_user=dc_users(),
    cloud_repo_pair=cloud_repositories(),
    dc_repo=dc_repositories(),
    cloud_pr=cloud_pull_requests(),
    dc_pr=dc_pull_requests(),
)
def test_all_identity_fields_survive_normalization_across_modes(
    cloud_user: dict[str, Any],
    dc_user: dict[str, Any],
    cloud_repo_pair: tuple[dict[str, Any], str],
    dc_repo: dict[str, Any],
    cloud_pr: dict[str, Any],
    dc_pr: dict[str, Any],
) -> None:
    """All four identity fields round-trip together across both modes.

    Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5
    """
    cloud_repo, workspace = cloud_repo_pair

    # 21.1 — Cloud user account_id
    cu_out = normalize_user(cloud_user)
    assert cu_out is not None
    assert cu_out["account_id"] == cloud_user["account_id"]

    # 21.2 — DC user slug (identity)
    du_out = normalize_user(dc_user)
    assert du_out is not None
    assert du_out["slug"] == dc_user["slug"]

    # 21.3 — Repository slug in both modes
    cr_out = normalize_repository(cloud_repo, workspace=workspace)
    dr_out = normalize_repository(dc_repo, workspace=None)
    assert cr_out is not None and cr_out["slug"] == cloud_repo["slug"]
    assert dr_out is not None and dr_out["slug"] == dc_repo["slug"]

    # 21.4 — Pull request id in both modes
    cp_out = normalize_pull_request(cloud_pr)
    dp_out = normalize_pull_request(dc_pr)
    assert cp_out is not None and cp_out["id"] == cloud_pr["id"]
    assert dp_out is not None and dp_out["id"] == dc_pr["id"]
