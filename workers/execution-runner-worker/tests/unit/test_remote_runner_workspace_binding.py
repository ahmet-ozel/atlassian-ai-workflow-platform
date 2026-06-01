"""Unit tests for the ``derive_workspace_path`` wrappers in
:mod:`src.runners.remote_ssh` and :mod:`src.runners.remote_ssh_docker`.

Spec: ``platform-mimari-uyumluluk`` Requirement 11.3 (Q13 —
``RUNNER_BASE_PATH`` env standard) — task 13.3.

The tests pin two contracts:

1. **Single source of truth.** Each wrapper delegates to
   :func:`src.runners.workspace_path.build_workspace_path` with the
   ``base`` argument bound to ``settings.runner_base_path``. A
   monkeypatched ``build_workspace_path`` records the exact arguments
   it was called with so the test can assert the binding without
   reimplementing path math here.

2. **Error propagation.** Validation errors raised by the central
   helper (``InvalidIssueKeyError`` for path-traversal vectors,
   ``InvalidIterError`` for out-of-range iterations) propagate through
   the wrapper unchanged — the wrapper does not swallow them or wrap
   them in a different exception type, because downstream audit code
   relies on the typed exception attributes.

The exhaustive Hypothesis-based property test for the helper itself
lives in ``platform/tests/property/test_runner_workspace_path.py``
(task 13.5); these unit tests document the binding contract at the
example level so a developer can ``pytest tests/unit -k
remote_runner_workspace_binding`` and get fast feedback while editing.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.runners import remote_ssh, remote_ssh_docker
from src.runners.workspace_path import (
    InvalidIssueKeyError,
    InvalidIterError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_settings(monkeypatch: pytest.MonkeyPatch, base: str) -> Settings:
    """Construct a :class:`Settings` whose ``runner_base_path`` equals
    ``base``, bypassing the per-worker ``.env`` file so the test is
    hermetic."""

    monkeypatch.delenv("SSH_BASE_PATH", raising=False)
    monkeypatch.setenv("RUNNER_BASE_PATH", base)
    return Settings(_env_file=None)  # type: ignore[arg-type]


# Both wrappers are expected to be byte-identical at the binding level.
# Parametrising the suite avoids two near-duplicate copies and pins the
# parity guarantee (an accidental divergence between the SSH and
# Docker variants would manifest as a Docker bind-mount that points at
# a different host path than the SSH ``cd`` target).
_WRAPPERS = [
    pytest.param(remote_ssh.derive_workspace_path, id="remote_ssh"),
    pytest.param(remote_ssh_docker.derive_workspace_path, id="remote_ssh_docker"),
]


# ---------------------------------------------------------------------------
# Delegation contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("derive", _WRAPPERS)
class TestDerivationDelegatesToCentralHelper:
    """The wrappers MUST route through ``build_workspace_path`` with
    the ``base`` argument bound to ``settings.runner_base_path`` —
    nothing else."""

    def test_canonical_output_with_explicit_settings(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit Settings instance — the wrapper MUST honour it
        # rather than constructing a fresh one (so tests can pin a
        # specific base without touching the env).
        settings = _build_settings(monkeypatch, "/var/ai-runner")

        assert (
            derive("PAY-4211", 0, settings=settings)
            == "/var/ai-runner/PAY-4211/iter-0"
        )

    def test_uses_env_when_no_settings_argument(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No ``settings=`` kwarg — the wrapper builds Settings()
        # internally, which reads ``RUNNER_BASE_PATH`` from the env.
        # This is the production call site shape.
        monkeypatch.delenv("SSH_BASE_PATH", raising=False)
        monkeypatch.setenv("RUNNER_BASE_PATH", "/srv/runner")

        assert derive("OPS_CORE-12", 3) == "/srv/runner/OPS_CORE-12/iter-3"

    def test_legacy_ssh_base_path_env_alias_is_honoured(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``SSH_BASE_PATH`` is the deprecated alias preserved for
        # backwards compatibility (R11.4). When ``RUNNER_BASE_PATH`` is
        # unset the wrapper MUST still resolve the legacy variable —
        # otherwise existing deployments would silently fall back to
        # the ``/var/ai-runner`` default and write workspaces in the
        # wrong place.
        monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
        monkeypatch.setenv("SSH_BASE_PATH", "/legacy/ai-runner")

        assert derive("PAY-1", 0) == "/legacy/ai-runner/PAY-1/iter-0"

    def test_default_base_when_no_env_set(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Neither env var set — the Settings default ``/var/ai-runner``
        # wins. This pins the contract that an unconfigured deployment
        # still produces a sensible (and grep-able) path.
        monkeypatch.delenv("RUNNER_BASE_PATH", raising=False)
        monkeypatch.delenv("SSH_BASE_PATH", raising=False)

        assert derive("PAY-1", 0) == "/var/ai-runner/PAY-1/iter-0"

    def test_delegates_to_build_workspace_path(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Replace the central helper with a recording double and
        # confirm the wrapper invokes it exactly once with
        # ``(settings.runner_base_path, issue_key, iter_n)``. This is
        # the actual "single source of truth" assertion: any future
        # refactor that inlines path math in the wrapper would fail
        # this test before it could ship.
        settings = _build_settings(monkeypatch, "/var/ai-runner")
        calls: list[tuple[str, str, int]] = []

        def _spy(base: str, issue_key: str, iter_n: int) -> str:
            calls.append((base, issue_key, iter_n))
            return f"SPY::{base}/{issue_key}/iter-{iter_n}"

        # Patch the symbol the wrapper resolved at import time — the
        # ``from ... import build_workspace_path`` style means the
        # binding lives on the wrapper module itself.
        monkeypatch.setattr(
            remote_ssh, "build_workspace_path", _spy, raising=True
        )
        monkeypatch.setattr(
            remote_ssh_docker, "build_workspace_path", _spy, raising=True
        )

        result = derive("PAY-4211", 7, settings=settings)

        assert result == "SPY::/var/ai-runner/PAY-4211/iter-7"
        assert calls == [("/var/ai-runner", "PAY-4211", 7)]


# ---------------------------------------------------------------------------
# Validation error propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("derive", _WRAPPERS)
class TestValidationErrorsPropagate:
    """Path-traversal and out-of-range guards live in the central
    helper; the wrappers MUST surface them unchanged so callers can
    rely on the typed exception attributes for audit payloads."""

    @pytest.mark.parametrize(
        "bad_key",
        [
            "../etc",
            "..",
            "PAY/../OPS-1",
            "PAY-1; rm -rf /",
            "PAY-1\nOPS-1",
            "pay-1",  # lowercase — rejected
            "",
            "PAY-",
            "-1",
        ],
    )
    def test_invalid_issue_key_raises(
        self, derive, monkeypatch: pytest.MonkeyPatch, bad_key: str
    ) -> None:
        settings = _build_settings(monkeypatch, "/var/ai-runner")

        with pytest.raises(InvalidIssueKeyError) as exc_info:
            derive(bad_key, 0, settings=settings)

        # The typed attribute is preserved verbatim so audit code can
        # log the offending value without re-parsing the message.
        assert exc_info.value.issue_key == bad_key

    @pytest.mark.parametrize("bad_iter", [-1, 1000, 9999])
    def test_invalid_iter_raises(
        self, derive, monkeypatch: pytest.MonkeyPatch, bad_iter: int
    ) -> None:
        settings = _build_settings(monkeypatch, "/var/ai-runner")

        with pytest.raises(InvalidIterError) as exc_info:
            derive("PAY-1", bad_iter, settings=settings)

        assert exc_info.value.iter_n == bad_iter

    def test_bool_iter_rejected(
        self, derive, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``isinstance(True, int)`` is ``True`` in Python; the central
        # helper rejects booleans so ``iter=True`` cannot silently
        # render as ``iter-1``. The wrapper must propagate that
        # rejection unchanged.
        settings = _build_settings(monkeypatch, "/var/ai-runner")

        with pytest.raises(InvalidIterError):
            derive("PAY-1", True, settings=settings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cross-wrapper parity
# ---------------------------------------------------------------------------


class TestSshAndDockerWrappersAgree:
    """The Docker runner mounts the host path returned by its wrapper
    into the container; the SSH runner ``cd``s into the same path. Any
    drift between the two would corrupt the workspace contract, so the
    wrappers MUST return byte-identical strings for the same input."""

    @pytest.mark.parametrize(
        "issue_key,iter_n",
        [
            ("PAY-4211", 0),
            ("OPS_CORE-12", 3),
            ("A-1", 999),
        ],
    )
    def test_outputs_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
        issue_key: str,
        iter_n: int,
    ) -> None:
        settings = _build_settings(monkeypatch, "/var/ai-runner")

        ssh_path = remote_ssh.derive_workspace_path(
            issue_key, iter_n, settings=settings
        )
        docker_path = remote_ssh_docker.derive_workspace_path(
            issue_key, iter_n, settings=settings
        )

        assert ssh_path == docker_path
