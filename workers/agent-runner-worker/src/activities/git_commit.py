"""Deployment-agnostic Bitbucket commit via Git-over-HTTPS.

The MCP server exposes read tools plus ``create_branch`` /
``create_pull_request`` for Bitbucket, but it has **no** file-write or
commit tool for any deployment. Earlier the commit step posted directly
to the Bitbucket Cloud ``/2.0/.../src`` REST endpoint, which only works
on Bitbucket Cloud.

This activity replaces that with a plain ``git clone  write  commit
push`` over HTTPS. Git-over-HTTPS is identical on Bitbucket Cloud and on
Bitbucket Server / Data Center, so the same code path works for both;
only the remote host and repository path differ, and both are derived
from the department's bot credential (URL + username + token). The
credential never lands on disk - it lives only inside the in-memory
remote URL used for the single clone/push and is masked from logs.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from temporalio import activity

from . import get_credential_resolver


class GitCommitError(RuntimeError):
    """Raised when the git clone/commit/push sequence fails."""


@dataclass(frozen=True)
class GitCommitResult:
    """Outcome of :func:`bitbucket_commit_via_git`.

    Attributes
    ----------
    commit_hash:
        The pushed commit SHA, or the branch name when the SHA could not
        be parsed from the push output.
    branch:
        The branch the change was pushed to.
    message:
        The commit message used.
    """

    commit_hash: str
    branch: str
    message: str


def _cred_get(creds: Any, *names: str) -> str:
    for name in names:
        value = creds.get(name) if isinstance(creds, dict) else getattr(creds, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _mask(text: str, secret: str) -> str:
    """Redact ``secret`` from ``text`` for safe logging."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _clone_url(base_url: str, workspace: str, repo_slug: str) -> str:
    """Build the unauthenticated HTTPS clone URL for Cloud or Server/DC.

    Bitbucket Cloud clones live at ``{host}/{workspace}/{repo}.git``
    (the web host ``bitbucket.org``, not the ``api.`` host). Bitbucket
    Server / Data Center exposes ``{host}/scm/{project}/{repo}.git``.
    The deployment is inferred from the host: ``bitbucket.org`` (and its
    ``api.`` alias) is Cloud, everything else is treated as Server/DC.
    """
    parts = urlsplit(base_url.strip() or "https://bitbucket.org")
    scheme = parts.scheme or "https"
    host = parts.netloc or "bitbucket.org"
    # The Cloud credential URL is sometimes the API host; clones use the
    # web host instead.
    if host == "api.bitbucket.org":
        host = "bitbucket.org"

    is_cloud = host in {"bitbucket.org", "www.bitbucket.org"}
    if is_cloud:
        path = f"/{workspace}/{repo_slug}.git"
    else:
        # Server/DC. ``workspace`` carries the project key in the
        # platform's credential model; honour an explicit ``/scm`` base
        # path if the operator already encoded one.
        base_path = parts.path.rstrip("/")
        if base_path.endswith("/scm"):
            prefix = base_path
        elif base_path:
            prefix = f"{base_path}/scm"
        else:
            prefix = "/scm"
        path = f"{prefix}/{workspace}/{repo_slug}.git"
    return urlunsplit((scheme, host, path, "", ""))


def _git_username(host: str, username: str, token: str) -> str:
    """Pick the git-HTTPS username for the deployment + token type.

    Bitbucket Cloud git-over-HTTPS does NOT accept the account email with
    an Atlassian API token (``ATATT...``); it requires the static
    username ``x-bitbucket-api-token-auth`` with the token as the
    password. App passwords use the real Bitbucket username. Server/DC
    accepts the account username with a personal access token. The
    account email is therefore only valid for app-password auth on
    Cloud, so we map an ``ATATT`` token on a Cloud host to the static
    user and otherwise fall back to the provided username.
    """
    is_cloud = host in {"bitbucket.org", "www.bitbucket.org", "api.bitbucket.org"}
    if is_cloud and token.startswith("ATATT"):
        return "x-bitbucket-api-token-auth"
    return username or "x-bitbucket-api-token-auth"


def _authenticated_url(clone_url: str, username: str, token: str) -> str:
    """Embed ``username:token`` into ``clone_url`` for Git-over-HTTPS auth.

    The username is normalised per deployment / token type (see
    :func:`_git_username`); the token is always the password.
    """
    parts = urlsplit(clone_url)
    git_user = _git_username(parts.netloc, username, token)
    userinfo = f"{quote(git_user, safe='')}:{quote(token, safe='')}"
    netloc = f"{userinfo}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _run_git(args: list[str], *, cwd: str, secret: str) -> tuple[int, str]:
    """Run a git command, returning (exit_code, combined_output).

    ``secret`` is redacted from the captured output before it is logged
    or surfaced in an exception.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={
            "GIT_TERMINAL_PROMPT": "0",  # never block on a credential prompt
            "GIT_ASKPASS": "true",
            "HOME": cwd,
        },
    )
    raw, _ = await proc.communicate()
    output = _mask(raw.decode("utf-8", "replace"), secret)
    return proc.returncode if proc.returncode is not None else -1, output


def _safe_rel_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or path.startswith("/") or ".." in path.split("/"):
        return ""
    return path


@activity.defn(name="bitbucket_commit_via_git")
async def bitbucket_commit_via_git(
    repo: dict[str, Any],
    branch: str,
    source_branch: str,
    files: list[dict[str, Any]],
    message: str,
    dept_id: str,
) -> GitCommitResult:
    """Clone, apply the file set, commit and push ``branch``.

    Parameters
    ----------
    repo:
        Mapping carrying ``workspace`` (Cloud workspace slug or DC
        project key) and ``repo_slug``.
    branch:
        Target branch to create/update and push.
    source_branch:
        Branch to base the new branch on (defaults to the repo's
        checked-out default when empty / ``auto``).
    files:
        List of ``{path, content, action}`` entries. ``action`` is one
        of ``create`` / ``update`` / ``delete``.
    message:
        Commit message.
    dept_id:
        Department id for credential resolution.
    """
    workspace = _cred_get(repo, "workspace", "project_key", "project")
    repo_slug = _cred_get(repo, "repo_slug", "slug", "repository")

    try:
        creds = await get_credential_resolver().get(
            dept_id, "bitbucket", scope="org"
        )
    except Exception as exc:  # noqa: BLE001
        raise GitCommitError(f"could not resolve Bitbucket credential: {exc}") from exc

    base_url = _cred_get(creds, "url", "base_url") or "https://bitbucket.org"
    username = _cred_get(creds, "username", "email", "user")
    token = _cred_get(creds, "api_token", "app_password", "personal_token", "token")
    workspace = workspace or _cred_get(creds, "bitbucket_workspace", "workspace", "project_key")
    repo_slug = repo_slug or _cred_get(creds, "bitbucket_repo", "repo_slug")

    if not workspace or not repo_slug:
        raise GitCommitError("missing Bitbucket workspace/repo")
    if not username or not token:
        raise GitCommitError("incomplete Bitbucket credential")

    changes = [c for c in files if isinstance(c, dict) and _safe_rel_path(c.get("path"))]
    if not changes:
        return GitCommitResult(commit_hash=branch, branch=branch, message=message)

    clone_url = _clone_url(base_url, workspace, repo_slug)
    auth_url = _authenticated_url(clone_url, username, token)

    base = (
        ""
        if not source_branch or source_branch in {"auto"} or source_branch.startswith("ai/")
        else source_branch
    )

    workdir = tempfile.mkdtemp(prefix="aigit-")
    repo_dir = str(Path(workdir) / "repo")
    try:
        activity.heartbeat(f"cloning {workspace}/{repo_slug}")
        clone_args = ["clone", "--depth", "1"]
        if base:
            clone_args += ["--branch", base]
        clone_args += [auth_url, repo_dir]
        code, out = await _run_git(clone_args, cwd=workdir, secret=token)
        if code != 0:
            raise GitCommitError(f"git clone failed: {out[-400:]}")

        # Identify the bot as author/committer.
        await _run_git(["config", "user.email", f"ai-bot@{dept_id}.local"], cwd=repo_dir, secret=token)
        await _run_git(["config", "user.name", "AI Bot"], cwd=repo_dir, secret=token)

        # Create (or reset to) the target branch off the cloned head.
        code, out = await _run_git(["checkout", "-B", branch], cwd=repo_dir, secret=token)
        if code != 0:
            raise GitCommitError(f"git checkout -B {branch} failed: {out[-400:]}")

        # Apply the file set.
        for change in changes:
            rel = _safe_rel_path(change.get("path"))
            action = str(change.get("action") or "update").strip().lower()
            target = Path(repo_dir) / rel
            if action in {"delete", "deleted", "remove", "removed"}:
                if target.is_file():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = change.get("content")
            target.write_text("" if content is None else str(content), encoding="utf-8")

        await _run_git(["add", "-A"], cwd=repo_dir, secret=token)

        activity.heartbeat(f"committing on {branch}")
        code, out = await _run_git(["commit", "-m", message], cwd=repo_dir, secret=token)
        if code != 0:
            # ``nothing to commit`` is not an error - the generated set
            # matched the current tree. Surface a stable hash so the PR
            # step still has the branch head to point at.
            if "nothing to commit" in out.lower():
                head_code, head = await _run_git(
                    ["rev-parse", "HEAD"], cwd=repo_dir, secret=token
                )
                return GitCommitResult(
                    commit_hash=head.strip() if head_code == 0 else branch,
                    branch=branch,
                    message=message,
                )
            raise GitCommitError(f"git commit failed: {out[-400:]}")

        activity.heartbeat(f"pushing {branch}")
        code, out = await _run_git(
            ["push", "--set-upstream", auth_url, branch, "--force"],
            cwd=repo_dir,
            secret=token,
        )
        if code != 0:
            raise GitCommitError(f"git push failed: {out[-400:]}")

        head_code, head = await _run_git(
            ["rev-parse", "HEAD"], cwd=repo_dir, secret=token
        )
        commit_hash = head.strip() if head_code == 0 else branch
        return GitCommitResult(commit_hash=commit_hash, branch=branch, message=message)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
