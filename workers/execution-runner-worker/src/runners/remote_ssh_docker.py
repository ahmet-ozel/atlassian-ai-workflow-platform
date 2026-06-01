"""Remote-SSH-Docker runner — workspace path derivation entry point.

Spec: ``platform-mimari-uyumluluk`` Requirement 11.3 (Q13 —
``RUNNER_BASE_PATH`` env standard) — task 13.3.

Scope of this module
--------------------

This module is the Docker-on-remote-host counterpart of
:mod:`src.runners.remote_ssh`. The full Docker runner implementation
(``docker run`` orchestration, volume mounts, container lifecycle) lives
under :mod:`src.activities.docker`; the surface here is intentionally
limited to *workspace path derivation*.

Task 13.3 pins the rule that both ``remote_ssh.py`` and
``remote_ssh_docker.py`` derive the host-side workspace mount point
**only** through :func:`runners.workspace_path.build_workspace_path`.
The Docker runner mounts that path into the container as a bind-mount
working directory, so any drift between the SSH runner's ``cd <path>``
and the Docker runner's ``-v <path>:<path>`` would manifest as missing
files inside the container — :func:`derive_workspace_path` makes that
drift impossible by construction.

Behaviour mirrors :func:`runners.remote_ssh.derive_workspace_path`
exactly — both flavours of remote runner share the same on-host layout
and the same ``RUNNER_BASE_PATH`` setting (with ``SSH_BASE_PATH`` as a
deprecated alias). Keeping the two thin wrappers as parallel modules
preserves the call-site grep target (a reader looking at
``runners/remote_ssh_docker.py`` should not have to follow an import
into a third helper to find out where the workspace lives).
"""

from __future__ import annotations

from src.config import Settings
from src.runners.workspace_path import (
    InvalidIssueKeyError,
    InvalidIterError,
    build_workspace_path,
)

__all__ = [
    "InvalidIssueKeyError",
    "InvalidIterError",
    "derive_workspace_path",
]


def derive_workspace_path(
    issue_key: str,
    iter_n: int,
    *,
    settings: Settings | None = None,
) -> str:
    """Return the remote-host workspace path for ``(issue_key, iter_n)``.

    Thin wrapper around
    :func:`runners.workspace_path.build_workspace_path` that binds the
    ``base`` argument to :attr:`Settings.runner_base_path` (the
    ``RUNNER_BASE_PATH`` env var, with ``SSH_BASE_PATH`` as a deprecated
    alias — see :mod:`src.config`).

    The Docker runner uses the returned string both as the bind-mount
    source on the host and as the container working directory, so the
    layout MUST stay byte-identical to the path the SSH runner would
    produce for the same ``(issue_key, iter_n)``. Both wrappers delegate
    to the same helper precisely so they cannot drift.

    Args:
        issue_key: Jira-style task key (e.g. ``PAY-4211``). Validated by
            :func:`build_workspace_path`; rejected values raise
            :class:`InvalidIssueKeyError` **before** any Docker / SSH
            command is issued (path-traversal safety, R11.3 / R11.6).
        iter_n: Iteration counter ``0..999``. Out-of-range or non-int
            values raise :class:`InvalidIterError`.
        settings: Optional :class:`Settings` instance; if omitted a
            fresh one is constructed and the env vars are resolved at
            call time. Tests typically pass an explicit instance to
            avoid touching the process environment.

    Returns:
        ``"{runner_base_path}/{issue_key}/iter-{iter_n}"`` with
        forward-slash separators.

    Raises:
        InvalidIssueKeyError: ``issue_key`` failed the regex guard.
        InvalidIterError: ``iter_n`` is not an int in ``[0, 999]``.
    """

    resolved = settings if settings is not None else Settings()
    return build_workspace_path(resolved.runner_base_path, issue_key, iter_n)
