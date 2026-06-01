"""Pure Cloud→DC response-shape normalizers for Bitbucket.

This module converts Bitbucket Cloud 2.0 API payloads into the Data Center
REST-shaped dicts that downstream server-layer code already consumes. Every
function in this module is:

- **Pure**: no HTTP, no I/O, no side effects. Input dicts are never mutated;
  a new dict is returned.
- **Total**: all functions accept partial / missing keys without raising.
  ``normalize_user`` additionally accepts ``None`` and returns ``None``.
- **Mode-detecting**: when given a DC-shaped input, the function returns the
  input unchanged (identity). Cloud shapes are transformed; the normalizer
  therefore becomes a no-op on already-DC payloads.
- **Passthrough-preserving**: unknown keys are carried through to the output
  so callers keep access to Cloud-only fields (``links``, ``type``, ...).

These functions implement the contract from Section "Components and
Interfaces / 3. bitbucket/response_normalizer.py" of the
bitbucket-cloud-dc-parity design document. They are invoked by mixin Cloud
branches (see task 5.2 and downstream mixin tasks); they are **not** wired
into any mixin by this task.

Secret redaction for webhook payloads is handled by the existing server-layer
``redact_secrets()`` helper, not by ``normalize_webhook``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dateutil import parser as _date_parser

__all__ = [
    "normalize_user",
    "normalize_repository",
    "normalize_pull_request",
    "normalize_commit",
    "normalize_branch",
    "normalize_tag",
    "normalize_webhook",
    "normalize_pagination_values",
]


# ---------------------------------------------------------------------------
# Shape detection helpers
# ---------------------------------------------------------------------------


def _is_cloud_user(u: dict[str, Any]) -> bool:
    """A user dict is Cloud-shaped iff it carries ``account_id``.

    DC user dicts never carry ``account_id``; they expose ``slug`` / ``name``.
    """
    return "account_id" in u


def _is_cloud_repository(r: dict[str, Any]) -> bool:
    """A repository dict is Cloud-shaped iff it carries ``workspace`` or ``full_name``.

    DC repository dicts never carry these; they expose ``project``.
    """
    if "workspace" in r or "full_name" in r:
        return True
    # If DC ``project`` is present, it is DC-shaped regardless.
    if "project" in r:
        return False
    # Fall back: ``uuid`` without ``project`` implies Cloud.
    return "uuid" in r


def _is_cloud_pull_request(pr: dict[str, Any]) -> bool:
    """A PR dict is Cloud-shaped iff it carries ``source`` / ``destination``.

    DC PR dicts expose ``fromRef`` / ``toRef``.
    """
    if "source" in pr or "destination" in pr:
        return True
    if "fromRef" in pr or "toRef" in pr:
        return False
    # Cloud PRs use ``created_on`` / ``updated_on``; DC uses ``createdDate`` / ``updatedDate``.
    return "created_on" in pr or "updated_on" in pr


def _is_cloud_commit(c: dict[str, Any]) -> bool:
    """A commit dict is Cloud-shaped iff it carries ``hash``.

    DC commit dicts expose ``id`` / ``displayId``.
    """
    return "hash" in c


def _is_cloud_branch(b: dict[str, Any]) -> bool:
    """A branch/tag dict is Cloud-shaped iff it lacks DC-specific ``id``/``displayId``.

    Cloud exposes ``name`` + ``target.hash``; DC exposes ``id`` (refs/heads/<n>),
    ``displayId``, and ``latestCommit``.
    """
    # DC always carries ``displayId`` and ``latestCommit``.
    if "displayId" in b and "latestCommit" in b:
        return False
    # Cloud carries ``target`` as a dict with ``hash``.
    target = b.get("target")
    if isinstance(target, dict) and "hash" in target:
        return True
    # Default: treat as Cloud if no DC-specific keys.
    return "displayId" not in b


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def _iso_to_epoch_ms(value: Any) -> int | None:
    """Convert an ISO 8601 timestamp to epoch milliseconds.

    Returns ``None`` for anything that cannot be parsed. Never raises.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = _date_parser.isoparse(value)
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_user(u: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket user payload to a DC-shaped dict.

    The output preserves every key from the input and additionally exposes
    both the Cloud and DC field names with identical values:

    - ``account_id``, ``name``, ``slug`` all equal the Cloud ``account_id``
    - ``display_name`` and ``displayName`` both equal the Cloud ``display_name``
    - ``uuid`` and ``id`` both equal the Cloud ``uuid``

    DC-shaped inputs are returned unchanged (identity). ``None`` input returns
    ``None``.
    """
    if u is None:
        return None
    if not isinstance(u, dict):
        return u  # type: ignore[unreachable]
    if not _is_cloud_user(u):
        return u

    out: dict[str, Any] = dict(u)  # passthrough unknown keys

    account_id = u.get("account_id")
    if account_id is not None:
        out["account_id"] = account_id
        out.setdefault("name", account_id)
        out.setdefault("slug", account_id)

    display_name = u.get("display_name")
    if display_name is not None:
        out["display_name"] = display_name
        out.setdefault("displayName", display_name)

    uuid = u.get("uuid")
    if uuid is not None:
        out["uuid"] = uuid
        out.setdefault("id", uuid)

    return out


def normalize_repository(
    r: dict[str, Any] | None,
    *,
    workspace: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a Bitbucket repository payload to a DC-shaped dict.

    On Cloud input, synthesize a DC-style ``project`` wrapper::

        project = {
            "key": workspace,
            "name": r.get("workspace", {}).get("slug", workspace),
        }

    Pass-through fields (``links``, ``uuid``, ``name``, ``full_name``,
    ``scm``, ``slug``, ...) are preserved. DC-shaped inputs return identity.
    ``None`` input returns ``None``.
    """
    if r is None:
        return None
    if not isinstance(r, dict):
        return r  # type: ignore[unreachable]
    if not _is_cloud_repository(r):
        return r

    out: dict[str, Any] = dict(r)

    # Synthesize DC-shaped project wrapper.
    ws = r.get("workspace")
    ws_slug: str | None = None
    if isinstance(ws, dict):
        ws_slug = ws.get("slug") or ws.get("name")
    project_key = workspace if workspace is not None else ws_slug
    project_name = ws_slug if ws_slug is not None else workspace

    # Only synthesize when we have at least one piece of information; never
    # overwrite a pre-existing ``project`` key.
    if "project" not in out and (project_key is not None or project_name is not None):
        out["project"] = {"key": project_key, "name": project_name}

    return out


def _normalize_ref_side(side: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build a DC-shaped ``fromRef``/``toRef`` dict from a Cloud source/destination.

    Cloud side shape::

        {"branch": {"name": "feature/x"}, "commit": {"hash": "abc"}, "repository": {...}}

    DC side shape (minimum fields we synthesize)::

        {"id": "refs/heads/feature/x", "displayId": "feature/x",
         "latestCommit": "abc", "repository": {...}}
    """
    if not isinstance(side, dict):
        return None
    branch = side.get("branch")
    commit = side.get("commit")
    name: str | None = None
    if isinstance(branch, dict):
        name = branch.get("name")
    latest_commit: str | None = None
    if isinstance(commit, dict):
        latest_commit = commit.get("hash")

    out: dict[str, Any] = dict(side)
    if name is not None:
        out.setdefault("displayId", name)
        out.setdefault("id", f"refs/heads/{name}")
    if latest_commit is not None:
        out.setdefault("latestCommit", latest_commit)
    return out


def normalize_pull_request(pr: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket pull-request payload to a DC-shaped dict.

    - Synthesize ``fromRef`` from Cloud ``source``; ``toRef`` from ``destination``.
    - Normalize ``author``, ``reviewers``, ``participants`` through :func:`normalize_user`.
    - Convert ``created_on`` / ``updated_on`` (ISO 8601) to ``createdDate`` /
      ``updatedDate`` (epoch millis) while preserving the ISO originals.
    - Pull-request ``id`` is already identical between Cloud and DC; passed through.

    DC-shaped inputs return identity. ``None`` input returns ``None``.
    """
    if pr is None:
        return None
    if not isinstance(pr, dict):
        return pr  # type: ignore[unreachable]
    if not _is_cloud_pull_request(pr):
        return pr

    out: dict[str, Any] = dict(pr)

    # Ref synthesis.
    source = pr.get("source")
    if source is not None and "fromRef" not in out:
        from_ref = _normalize_ref_side(source)
        if from_ref is not None:
            out["fromRef"] = from_ref
    destination = pr.get("destination")
    if destination is not None and "toRef" not in out:
        to_ref = _normalize_ref_side(destination)
        if to_ref is not None:
            out["toRef"] = to_ref

    # User normalization.
    author = pr.get("author")
    if isinstance(author, dict):
        out["author"] = normalize_user(author)

    reviewers = pr.get("reviewers")
    if isinstance(reviewers, list):
        out["reviewers"] = [
            normalize_user(rv) if isinstance(rv, dict) else rv for rv in reviewers
        ]

    participants = pr.get("participants")
    if isinstance(participants, list):
        normalized_participants: list[Any] = []
        for p in participants:
            if isinstance(p, dict):
                # Participants embed a ``user`` subobject on Cloud.
                np = dict(p)
                user = p.get("user")
                if isinstance(user, dict):
                    np["user"] = normalize_user(user)
                normalized_participants.append(np)
            else:
                normalized_participants.append(p)
        out["participants"] = normalized_participants

    # Timestamp conversion.
    created_ms = _iso_to_epoch_ms(pr.get("created_on"))
    if created_ms is not None:
        out.setdefault("createdDate", created_ms)
    updated_ms = _iso_to_epoch_ms(pr.get("updated_on"))
    if updated_ms is not None:
        out.setdefault("updatedDate", updated_ms)

    return out


def normalize_commit(c: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket commit payload to a DC-shaped dict.

    - DC ``id`` = Cloud ``hash``.
    - ``displayId`` = ``hash[:7]``.
    - Normalize ``author`` and ``committer`` through :func:`normalize_user`
      when they embed a ``user`` subobject (Cloud shape).

    DC-shaped inputs return identity. ``None`` input returns ``None``.
    """
    if c is None:
        return None
    if not isinstance(c, dict):
        return c  # type: ignore[unreachable]
    if not _is_cloud_commit(c):
        return c

    out: dict[str, Any] = dict(c)

    hash_value = c.get("hash")
    if isinstance(hash_value, str) and hash_value:
        out.setdefault("id", hash_value)
        out.setdefault("displayId", hash_value[:7])

    for key in ("author", "committer"):
        entry = c.get(key)
        if isinstance(entry, dict):
            # Cloud commit author is ``{"raw": "...", "user": {...}}``.
            new_entry = dict(entry)
            user = entry.get("user")
            if isinstance(user, dict):
                new_entry["user"] = normalize_user(user)
            out[key] = new_entry

    return out


def normalize_branch(b: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket branch payload to a DC-shaped dict.

    Exposes ``displayId``, ``id`` (``refs/heads/<name>``), and ``latestCommit``.
    DC-shaped inputs return identity. ``None`` input returns ``None``.
    """
    return _normalize_ref(b, kind="branch")


def normalize_tag(t: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket tag payload to a DC-shaped dict.

    Exposes ``displayId``, ``id`` (``refs/tags/<name>``), and ``latestCommit``.
    DC-shaped inputs return identity. ``None`` input returns ``None``.
    """
    return _normalize_ref(t, kind="tag")


def _normalize_ref(
    ref: dict[str, Any] | None,
    *,
    kind: str,
) -> dict[str, Any] | None:
    """Internal ref normalizer for branches and tags."""
    if ref is None:
        return None
    if not isinstance(ref, dict):
        return ref  # type: ignore[unreachable]
    if not _is_cloud_branch(ref):
        return ref

    out: dict[str, Any] = dict(ref)
    name = ref.get("name")
    if isinstance(name, str) and name:
        out.setdefault("displayId", name)
        prefix = "refs/heads/" if kind == "branch" else "refs/tags/"
        out.setdefault("id", f"{prefix}{name}")

    target = ref.get("target")
    if isinstance(target, dict):
        hash_value = target.get("hash")
        if isinstance(hash_value, str) and hash_value:
            out.setdefault("latestCommit", hash_value)

    return out


def normalize_webhook(w: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a Bitbucket webhook payload.

    Webhooks are returned as passthrough: the shapes between Cloud and DC are
    close enough that downstream code does not require re-keying. Secret
    redaction is the responsibility of the existing server-layer
    ``redact_secrets()`` helper and is intentionally **not** applied here.

    ``None`` input returns ``None``; other inputs return a shallow copy to
    preserve the pure / non-mutating contract.
    """
    if w is None:
        return None
    if not isinstance(w, dict):
        return w  # type: ignore[unreachable]
    return dict(w)


def normalize_pagination_values(
    values: list[Any] | None,
    normalizer: Callable[[Any], Any],
) -> list[Any]:
    """Apply ``normalizer`` to each item in ``values`` (or return ``[]`` on ``None``).

    A thin helper for use by the Cloud pagination path; kept here so every
    shape-aware transformation lives in one module.
    """
    if not values:
        return []
    return [normalizer(v) for v in values]
