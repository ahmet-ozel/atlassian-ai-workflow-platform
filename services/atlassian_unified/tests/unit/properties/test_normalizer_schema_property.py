"""Property test P5 — Response normalizer exposes DC-shape keys with Cloud-value identity.

Validates Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 8.7, 13.5 of the
``bitbucket-cloud-dc-parity`` spec / design Property 5.

The normalizer functions in
:mod:`mcp_atlassian.bitbucket.response_normalizer` translate
Bitbucket Cloud 2.0 response payloads into the Data Center-shaped
dicts that downstream server code already consumes. The correctness
property here is that, for **every** Cloud-shaped payload, the
normalized output carries the DC-shape alias keys with values
**identical** to the corresponding Cloud source values:

* ``normalize_user(u)``:

    - ``out["account_id"] == u["account_id"]``
    - ``out["name"]       == u["account_id"]``
    - ``out["slug"]       == u["account_id"]``
    - ``out["display_name"] == u["display_name"]``   (Cloud key preserved)
    - ``out["displayName"]  == u["display_name"]``   (Req 6.4)
    - ``out["uuid"] == u["uuid"]``
    - ``out["id"]   == u["uuid"]``                   (Req 6.3)

* ``normalize_repository(r, workspace=w)``:

    - ``out["project"]["key"] == w`` (synthesized per Req 8.7)
    - Cloud passthrough fields (``slug``, ``uuid``, ``name``, ``full_name``,
      ``scm``, ``links``) preserved byte-for-byte.

* ``normalize_pull_request(pr)``:

    - ``out["id"] == pr["id"]`` (pass-through)
    - ``out["fromRef"]["displayId"] == pr["source"]["branch"]["name"]``
    - ``out["toRef"]["displayId"]   == pr["destination"]["branch"]["name"]``
    - ``out["fromRef"]["latestCommit"] == pr["source"]["commit"]["hash"]``
    - ``out["author"]["account_id"] == pr["author"]["account_id"]``
      (recursive user normalization)

* ``normalize_commit(c)``:

    - ``out["id"]        == c["hash"]``              (Req 6.1)
    - ``out["displayId"] == c["hash"][:7]``

* ``normalize_branch(b)`` / ``normalize_tag(t)``:

    - ``out["displayId"]   == b["name"]``
    - ``out["id"]          == f"refs/heads/{b['name']}"`` (branches)
    - ``out["id"]          == f"refs/tags/{t['name']}"``  (tags)
    - ``out["latestCommit"] == b["target"]["hash"]``

The property is pure: the normalizer issues zero HTTP and does not
depend on any fixture beyond the generated dict. The test exercises
the functions directly with Hypothesis-generated Cloud payloads plus a
small parametrised smoke suite for concrete examples.

Style reference: :mod:`tests.unit.properties.test_owner_scoped_property`
for the ``@given`` decorator conventions and
:mod:`tests.unit.properties.conftest` for Hypothesis profile setup.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.response_normalizer import (
    normalize_branch,
    normalize_commit,
    normalize_pull_request,
    normalize_repository,
    normalize_tag,
    normalize_user,
)


# ---------------------------------------------------------------------------
# Shared Hypothesis strategies for Cloud-shaped payloads
# ---------------------------------------------------------------------------

# Non-empty ASCII strings used as Cloud ``account_id``, ``display_name``,
# workspace / repo slugs, branch / tag names, etc. Kept bounded so shrinks
# stay fast.
_NON_EMPTY_TEXT: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        min_codepoint=ord("!"),
        max_codepoint=ord("~"),
        blacklist_characters=" \t\n\r\v\f/",
    ),
    min_size=1,
    max_size=32,
)

# Commit SHA-1 digests — 40 hex chars lowercase. The normalizer derives
# ``displayId = hash[:7]`` so we need a string at least 7 chars long. We
# use realistic 40-char shapes plus a couple of shorter edge cases.
_SHA_HEX: st.SearchStrategy[str] = st.text(
    alphabet="0123456789abcdef",
    min_size=7,
    max_size=40,
)

# Cloud UUIDs are wrapped in curly braces, e.g. ``{12345678-1234-...}``.
# We allow any non-empty text within the braces to exercise pass-through.
_CLOUD_UUID: st.SearchStrategy[str] = st.builds(
    lambda core: "{" + core + "}",
    core=_NON_EMPTY_TEXT,
)


@st.composite
def _cloud_user(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped user payload.

    Cloud users always carry ``account_id``; ``display_name`` and ``uuid``
    are present with high probability. We also mix in an arbitrary
    passthrough key (``nickname``) and a passthrough ``links`` dict to
    ensure unknown fields survive normalization.
    """
    return {
        "account_id": draw(_NON_EMPTY_TEXT),
        "display_name": draw(_NON_EMPTY_TEXT),
        "uuid": draw(_CLOUD_UUID),
        "nickname": draw(_NON_EMPTY_TEXT),
        "type": "user",
        "links": {"self": {"href": "https://api.bitbucket.org/2.0/users/x"}},
    }


@st.composite
def _cloud_repository(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped repository payload."""
    workspace_slug = draw(_NON_EMPTY_TEXT)
    repo_slug = draw(_NON_EMPTY_TEXT)
    return {
        "slug": repo_slug,
        "name": draw(_NON_EMPTY_TEXT),
        "full_name": f"{workspace_slug}/{repo_slug}",
        "uuid": draw(_CLOUD_UUID),
        "scm": "git",
        "workspace": {
            "slug": workspace_slug,
            "name": draw(_NON_EMPTY_TEXT),
            "uuid": draw(_CLOUD_UUID),
        },
        "links": {"self": {"href": "https://api.bitbucket.org/2.0/repositories/x/y"}},
    }


@st.composite
def _cloud_branch(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped branch payload."""
    return {
        "name": draw(_NON_EMPTY_TEXT),
        "target": {
            "hash": draw(_SHA_HEX),
            "type": "commit",
        },
        "type": "branch",
    }


@st.composite
def _cloud_tag(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped tag payload."""
    return {
        "name": draw(_NON_EMPTY_TEXT),
        "target": {
            "hash": draw(_SHA_HEX),
            "type": "commit",
        },
        "type": "tag",
    }


@st.composite
def _cloud_commit(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped commit payload with nested author/committer."""
    return {
        "hash": draw(_SHA_HEX),
        "message": draw(_NON_EMPTY_TEXT),
        "date": "2024-01-15T10:30:00.000000+00:00",
        "author": {
            "raw": draw(_NON_EMPTY_TEXT),
            "user": draw(_cloud_user()),
        },
        "committer": {
            "raw": draw(_NON_EMPTY_TEXT),
            "user": draw(_cloud_user()),
        },
        "type": "commit",
    }


@st.composite
def _cloud_pull_request(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Cloud-shaped pull-request payload."""
    return {
        "id": draw(st.integers(min_value=1, max_value=10_000)),
        "title": draw(_NON_EMPTY_TEXT),
        "state": draw(st.sampled_from(["OPEN", "MERGED", "DECLINED", "SUPERSEDED"])),
        "author": draw(_cloud_user()),
        "reviewers": draw(st.lists(_cloud_user(), min_size=0, max_size=3)),
        "source": {
            "branch": {"name": draw(_NON_EMPTY_TEXT)},
            "commit": {"hash": draw(_SHA_HEX)},
            "repository": {"full_name": draw(_NON_EMPTY_TEXT)},
        },
        "destination": {
            "branch": {"name": draw(_NON_EMPTY_TEXT)},
            "commit": {"hash": draw(_SHA_HEX)},
            "repository": {"full_name": draw(_NON_EMPTY_TEXT)},
        },
        "created_on": "2024-01-15T10:30:00.000000+00:00",
        "updated_on": "2024-01-16T11:45:00.000000+00:00",
    }


# ---------------------------------------------------------------------------
# Property A — normalize_user exposes DC-shape keys with Cloud-value identity
# ---------------------------------------------------------------------------


@given(u=_cloud_user())
def test_normalize_user_exposes_dc_keys_with_cloud_values(u: dict[str, Any]) -> None:
    """P5.A — Cloud ``account_id`` / ``display_name`` / ``uuid`` alias to
    DC ``name`` / ``slug`` / ``displayName`` / ``id`` with identical values.

    Validates Requirements 6.3, 6.4, 6.5, 13.5.
    """
    out = normalize_user(u)
    assert out is not None

    # Req 6.5: account_id / name / slug all equal the Cloud account_id.
    assert out["account_id"] == u["account_id"]
    assert out["name"] == u["account_id"]
    assert out["slug"] == u["account_id"]

    # Req 6.4: both display_name (Cloud) and displayName (DC alias).
    assert out["display_name"] == u["display_name"]
    assert out["displayName"] == u["display_name"]

    # Req 6.3: both uuid (Cloud) and id (DC alias).
    assert out["uuid"] == u["uuid"]
    assert out["id"] == u["uuid"]

    # Passthrough: unknown Cloud keys are preserved unchanged.
    assert out["nickname"] == u["nickname"]
    assert out["type"] == u["type"]
    assert out["links"] == u["links"]


def test_normalize_user_none_returns_none() -> None:
    """P5.A — ``None`` input round-trips to ``None`` (total function)."""
    assert normalize_user(None) is None


# ---------------------------------------------------------------------------
# Property B — normalize_repository synthesizes project wrapper (Req 8.7)
# ---------------------------------------------------------------------------


@given(r=_cloud_repository(), w=_NON_EMPTY_TEXT)
def test_normalize_repository_synthesizes_project_key_from_workspace(
    r: dict[str, Any], w: str
) -> None:
    """P5.B — Cloud repo gains a synthetic ``project`` wrapper whose
    ``key`` equals the explicit workspace argument (Req 8.7).
    """
    out = normalize_repository(r, workspace=w)
    assert out is not None

    # Req 8.7: synthesized project with key=w.
    assert "project" in out
    assert out["project"]["key"] == w
    # project.name falls back to the workspace slug from the payload
    # (or workspace arg if absent); both are non-empty strings.
    assert out["project"]["name"] is not None

    # Passthrough: Cloud fields survive normalization unchanged.
    assert out["slug"] == r["slug"]
    assert out["name"] == r["name"]
    assert out["full_name"] == r["full_name"]
    assert out["uuid"] == r["uuid"]
    assert out["scm"] == r["scm"]
    assert out["links"] == r["links"]


@given(r=_cloud_repository())
def test_normalize_repository_derives_project_key_when_workspace_absent(
    r: dict[str, Any],
) -> None:
    """P5.B — when ``workspace`` arg is ``None``, project.key is derived
    from the embedded ``workspace.slug`` in the Cloud payload.
    """
    out = normalize_repository(r, workspace=None)
    assert out is not None
    assert "project" in out
    # Derived from r["workspace"]["slug"].
    assert out["project"]["key"] == r["workspace"]["slug"]


# ---------------------------------------------------------------------------
# Property C — normalize_commit exposes DC id / displayId from Cloud hash
# ---------------------------------------------------------------------------


@given(c=_cloud_commit())
def test_normalize_commit_aliases_hash_to_id_and_short_displayid(
    c: dict[str, Any],
) -> None:
    """P5.C — DC ``id`` = Cloud ``hash`` and ``displayId`` = ``hash[:7]``."""
    out = normalize_commit(c)
    assert out is not None

    assert out["id"] == c["hash"]
    assert out["displayId"] == c["hash"][:7]
    assert len(out["displayId"]) == 7

    # Nested author.user is recursively normalized (carries DC alias keys).
    assert isinstance(out["author"], dict)
    nested_user = out["author"]["user"]
    source_user = c["author"]["user"]
    assert nested_user["account_id"] == source_user["account_id"]
    assert nested_user["name"] == source_user["account_id"]
    assert nested_user["slug"] == source_user["account_id"]

    # Passthrough of unrelated fields.
    assert out["message"] == c["message"]
    assert out["date"] == c["date"]


# ---------------------------------------------------------------------------
# Property D — normalize_branch / normalize_tag expose DC ref keys
# ---------------------------------------------------------------------------


@given(b=_cloud_branch())
def test_normalize_branch_exposes_dc_ref_keys(b: dict[str, Any]) -> None:
    """P5.D — Branches expose ``displayId`` = name, ``id`` = refs/heads/<name>,
    ``latestCommit`` = target.hash.
    """
    out = normalize_branch(b)
    assert out is not None

    assert out["displayId"] == b["name"]
    assert out["id"] == f"refs/heads/{b['name']}"
    assert out["latestCommit"] == b["target"]["hash"]

    # Passthrough of the Cloud ``target`` sub-object.
    assert out["target"] == b["target"]


@given(t=_cloud_tag())
def test_normalize_tag_exposes_dc_ref_keys(t: dict[str, Any]) -> None:
    """P5.D — Tags expose the DC refs/tags/<name> shape."""
    out = normalize_tag(t)
    assert out is not None

    assert out["displayId"] == t["name"]
    assert out["id"] == f"refs/tags/{t['name']}"
    assert out["latestCommit"] == t["target"]["hash"]


# ---------------------------------------------------------------------------
# Property E — normalize_pull_request synthesizes fromRef / toRef and
# recursively normalizes user entries (Req 6.1, 6.2)
# ---------------------------------------------------------------------------


@given(pr=_cloud_pull_request())
def test_normalize_pull_request_exposes_dc_refs_and_user_aliases(
    pr: dict[str, Any],
) -> None:
    """P5.E — Cloud PR gains DC-shaped ``fromRef`` / ``toRef`` with
    ``displayId``, ``id``, ``latestCommit`` sourced from Cloud
    ``source`` / ``destination``, and recursive user normalization is
    applied to ``author`` and every entry in ``reviewers``.
    """
    out = normalize_pull_request(pr)
    assert out is not None

    # PR id is passthrough identity.
    assert out["id"] == pr["id"]
    assert out["state"] == pr["state"]
    assert out["title"] == pr["title"]

    # fromRef / toRef synthesized from Cloud source / destination.
    src_name = pr["source"]["branch"]["name"]
    dst_name = pr["destination"]["branch"]["name"]
    assert out["fromRef"]["displayId"] == src_name
    assert out["fromRef"]["id"] == f"refs/heads/{src_name}"
    assert out["fromRef"]["latestCommit"] == pr["source"]["commit"]["hash"]
    assert out["toRef"]["displayId"] == dst_name
    assert out["toRef"]["id"] == f"refs/heads/{dst_name}"
    assert out["toRef"]["latestCommit"] == pr["destination"]["commit"]["hash"]

    # Author is recursively normalized (carries DC alias keys).
    assert out["author"]["account_id"] == pr["author"]["account_id"]
    assert out["author"]["name"] == pr["author"]["account_id"]
    assert out["author"]["slug"] == pr["author"]["account_id"]
    assert out["author"]["displayName"] == pr["author"]["display_name"]

    # Every reviewer is recursively normalized.
    assert len(out["reviewers"]) == len(pr["reviewers"])
    for out_rv, src_rv in zip(out["reviewers"], pr["reviewers"]):
        assert out_rv["account_id"] == src_rv["account_id"]
        assert out_rv["name"] == src_rv["account_id"]
        assert out_rv["slug"] == src_rv["account_id"]
        assert out_rv["displayName"] == src_rv["display_name"]


# ---------------------------------------------------------------------------
# Smoke examples — hand-picked shapes that pin down the exact contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cloud_user_in", "expected_aliases"),
    [
        (
            {
                "account_id": "557058:abc-123",
                "display_name": "Alice Example",
                "uuid": "{11111111-1111-1111-1111-111111111111}",
            },
            {
                "account_id": "557058:abc-123",
                "name": "557058:abc-123",
                "slug": "557058:abc-123",
                "display_name": "Alice Example",
                "displayName": "Alice Example",
                "uuid": "{11111111-1111-1111-1111-111111111111}",
                "id": "{11111111-1111-1111-1111-111111111111}",
            },
        ),
    ],
)
def test_normalize_user_concrete_example(
    cloud_user_in: dict[str, Any], expected_aliases: dict[str, Any]
) -> None:
    """P5 smoke — exact alias values for a canonical Cloud user payload."""
    out = normalize_user(cloud_user_in)
    assert out is not None
    for key, value in expected_aliases.items():
        assert out[key] == value, f"alias {key!r} mismatch: {out.get(key)!r} != {value!r}"
