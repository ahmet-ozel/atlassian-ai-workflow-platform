"""Unit tests for ``components.credential_manager``.

Drives the pure :class:`CredentialManager` slice with synthetic
``state`` dicts and a deterministic monotonic clock so the
inactivity-timeout window can be exercised without sleeping. The
Streamlit-side ``render_*`` helpers are out of scope here; the property
test suite and the CI page-presence test cover those surfaces.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import pytest

_STREAMLIT_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "ui" / "streamlit-app"
)
if str(_STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_ROOT))


try:  # pragma: no cover - guarded import (Streamlit may be missing).
    import components.credential_manager as credential_manager  # type: ignore[import-not-found]
    from components.cookie_manager import sign_cookie  # type: ignore[import-not-found]
    from components.credential_manager import (  # type: ignore[import-not-found]
        CREDENTIAL_WARNING_TEXT,
        CredentialManager,
        StoredCredential,
        _RESTORE_COOKIE_NAME,
        _default_validator,
        _restore_cookie_key,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    CredentialManager = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    CredentialManager is None,
    reason=(
        "components.credential_manager not importable "
        f"(streamlit missing?); error: {_IMPORT_ERROR!r}"
    ),
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _Clock:
    """Deterministic clock for the inactivity-window tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_manager(
    *, validator=None, clock: _Clock | None = None
) -> tuple[CredentialManager, dict, _Clock]:
    state: dict = {}
    clk = clock or _Clock()
    if validator is None:

        def validator(service, email, token):  # noqa: ARG001 - fixture seam
            return True, None

    return (
        CredentialManager(state=state, now=clk, validator=validator),
        state,
        clk,
    )


# ---------------------------------------------------------------------------
# session_state-only storage
# ---------------------------------------------------------------------------


def test_store_keeps_token_only_in_session_state() -> None:
    """Credentials live only in the injected ``state`` dict.

    The token is stored on the :class:`StoredCredential` object held
    inside ``state[_STATE_KEY]['credentials']``; nothing else in the
    return value or the manager's public surface exposes it.
    """
    mgr, state, _ = _make_manager()

    cred = mgr.store("jira", email="alice@example.com", api_token="tok-123")

    # Token lives on the dataclass…
    assert cred.api_token == "tok-123"
    # …and inside the state bucket.
    bucket = state["_credential_manager_state"]
    assert bucket["credentials"]["jira"].api_token == "tok-123"
    # The snapshot deliberately strips the token.
    snap = mgr.snapshot()
    assert "api_token" not in snap["credentials"]["jira"]
    assert snap["credentials"]["jira"]["email"].endswith("@example.com")
    assert "tok-123" not in str(snap)


def test_store_rejects_invalid_inputs() -> None:
    mgr, _, _ = _make_manager()

    with pytest.raises(ValueError):
        mgr.store("unknown", email="a@b.com", api_token="t")
    with pytest.raises(ValueError):
        mgr.store("jira", email="not-an-email", api_token="t")
    with pytest.raises(ValueError):
        mgr.store("jira", email="a@b.com", api_token="")


# ---------------------------------------------------------------------------
# 60 minute inactivity timeout  auto clear
# ---------------------------------------------------------------------------


def test_session_expires_after_sixty_minutes_of_inactivity() -> None:
    mgr, state, clk = _make_manager()
    mgr.store("jira", email="a@b.com", api_token="t1")

    # 59m59s - still alive.
    clk.advance(60 * 60 - 1)
    assert mgr.is_expired() is False
    assert mgr.get("jira") is not None

    # …re-touched by the get() call above; advance 60 minutes again to
    # cross the threshold cleanly.
    clk.advance(60 * 60 + 1)
    assert mgr.is_expired() is True

    # enforce_timeout drops the bucket; an immediate state read confirms
    # every credential has been cleared. (A subsequent get() may
    # legitimately re-create an empty bucket so the inactivity timer
    # restarts for the next interaction; the post-clear invariant we
    # care about is "no surviving tokens".)
    cleared = mgr.enforce_timeout()
    assert cleared is True
    assert "_credential_manager_state" not in state
    # Subsequent reads still return None - the credential is gone.
    assert mgr.get("jira") is None
    # The bucket may have been re-created by ``get()`` but it must be
    # empty of credentials.
    bucket = state.get("_credential_manager_state", {})
    assert bucket.get("credentials", {}) == {}


def test_get_implicitly_clears_expired_session() -> None:
    """``get()`` MUST itself clear an expired bucket."""
    mgr, state, clk = _make_manager()
    mgr.store("jira", email="a@b.com", api_token="t1")

    clk.advance(60 * 60 + 5)  # one step past the threshold
    assert mgr.get("jira") is None
    assert "_credential_manager_state" not in state


def test_active_interaction_keeps_session_alive() -> None:
    mgr, _, clk = _make_manager()
    mgr.store("jira", email="a@b.com", api_token="t1")

    # Twenty five-minute interaction bursts: the session should stay
    # alive indefinitely as long as someone keeps touching it.
    for _ in range(10):
        clk.advance(25 * 60)
        assert mgr.get("jira") is not None
        assert mgr.is_expired() is False


# ---------------------------------------------------------------------------
# Verbatim warning copy
# ---------------------------------------------------------------------------


def test_warning_text_matches_requirements_doc() -> None:
    """The warning copy must match the product text verbatim.

    The exact Turkish phrasing is asserted to catch any future drift
    introduced by a well-meaning copy edit.
    """
    expected = (
        "Bu bilgiler yalnızca bu tarayıcı sekmesinde, bu oturum süresince "
        "saklanır. Sekme kapatıldığında veya 60 dakika işlem yapılmadığında "
        "otomatik silinir."
    )
    assert CREDENTIAL_WARNING_TEXT == expected


# ---------------------------------------------------------------------------
# Credential validation via injected validator
# ---------------------------------------------------------------------------


def test_validate_invokes_validator_and_records_result() -> None:
    calls: list[tuple[str, str, str]] = []

    def validator(service: str, email: str, token: str) -> tuple[bool, str | None]:
        calls.append((service, email, token))
        return True, None

    mgr, _, _ = _make_manager(validator=validator)
    mgr.store("jira", email="alice@example.com", api_token="tok-xyz")

    ok, err = mgr.validate("jira")

    assert ok is True
    assert err is None
    assert calls == [("jira", "alice@example.com", "tok-xyz")]
    cred = mgr.get("jira")
    assert cred is not None
    assert cred.is_valid is True
    assert cred.last_validated_at is not None


def test_validate_marks_failure_but_keeps_token() -> None:
    def validator(service: str, email: str, token: str) -> tuple[bool, str | None]:
        return False, "auth rejected"

    mgr, _, _ = _make_manager(validator=validator)
    mgr.store("confluence", email="bob@example.com", api_token="tok-abc")

    ok, err = mgr.validate("confluence")

    assert ok is False
    assert err == "auth rejected"
    cred = mgr.get("confluence")
    assert cred is not None
    assert cred.is_valid is False
    # Token survives so the user can retry without retyping.
    assert cred.api_token == "tok-abc"


def test_validate_returns_friendly_error_when_no_credential() -> None:
    mgr, _, _ = _make_manager()
    ok, err = mgr.validate("jira")
    assert ok is False
    assert "bulunamadı" in (err or "")


# ---------------------------------------------------------------------------
# Auth header building
# ---------------------------------------------------------------------------


def test_default_bitbucket_validator_probes_cloud_user(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _Response:
        status_code = 401

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> _Response:
            calls.append({"url": url, "headers": headers})
            return _Response()

    class _HTTPX:
        RequestError = OSError
        Client = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    mgr, _, _ = _make_manager(validator=_default_validator)
    mgr.store("bitbucket", email="user@example.com", api_token="bad-token")

    ok, err = mgr.validate("bitbucket")

    assert ok is False
    assert "HTTP 401" in (err or "")
    assert "Streamlit'e girilen username/token" in (err or "")
    assert calls[0]["url"] == "https://api.bitbucket.org/2.0/user"
    assert str(calls[0]["headers"]["Authorization"]).startswith("Basic ")
    cred = mgr.get("bitbucket")
    assert cred is not None
    assert cred.is_valid is False


def test_default_bitbucket_validator_probes_workspace_scope(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _Response:
        status_code = 200

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def get(self, url: str, *, headers: dict[str, str]) -> _Response:
            calls.append({"url": url, "headers": headers})
            return _Response()

    class _HTTPX:
        RequestError = OSError
        Client = _Client

    monkeypatch.setitem(sys.modules, "httpx", _HTTPX)
    mgr, _, _ = _make_manager(validator=_default_validator)
    mgr.store(
        "bitbucket",
        email="user@example.com",
        api_token="ok-token",
        workspace="example_workspace",
    )

    ok, err = mgr.validate("bitbucket")

    assert ok is True
    assert err is None
    assert calls[0]["url"] == (
        "https://api.bitbucket.org/2.0/repositories/example_workspace?pagelen=1"
    )
    assert str(calls[0]["headers"]["Authorization"]).startswith("Basic ")
    cred = mgr.get("bitbucket")
    assert cred is not None
    assert cred.is_valid is True


def test_restore_cookie_key_decodes_url_encoded_cookie_header(monkeypatch) -> None:
    secret = "restore-test-secret"
    monkeypatch.setenv("COOKIE_SECRET", secret)
    signed = sign_cookie("restore-key", secret)

    class _Context:
        cookies = None
        headers = {"Cookie": f"{_RESTORE_COOKIE_NAME}={quote(signed)}"}

    monkeypatch.setattr(credential_manager.st, "query_params", {}, raising=False)
    monkeypatch.setattr(credential_manager.st, "context", _Context(), raising=False)
    monkeypatch.setattr(credential_manager.st, "session_state", {}, raising=False)

    assert _restore_cookie_key() == "restore-key"


def test_get_auth_header_returns_basic_base64_value() -> None:
    """The manager builds a Basic auth header on the fly."""
    import base64

    mgr, _, _ = _make_manager()
    mgr.store("jira", email="alice@example.com", api_token="tok-123")

    header = mgr.get_auth_header("jira")
    assert header is not None
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    assert decoded == "alice@example.com:tok-123"


def test_get_auth_header_returns_none_for_unknown_service() -> None:
    mgr, _, _ = _make_manager()
    assert mgr.get_auth_header("jira") is None


def test_get_auth_header_returns_none_after_expiry() -> None:
    mgr, _, clk = _make_manager()
    mgr.store("jira", email="a@b.com", api_token="t")
    clk.advance(60 * 60 + 5)
    assert mgr.get_auth_header("jira") is None


# ---------------------------------------------------------------------------
# Explicit logout / clear-all
# ---------------------------------------------------------------------------


def test_clear_all_removes_every_credential_and_state_bucket() -> None:
    mgr, state, _ = _make_manager()
    mgr.store("jira", email="a@b.com", api_token="t1")
    mgr.store("confluence", email="a@b.com", api_token="t2")

    mgr.clear_all()

    assert "_credential_manager_state" not in state
    assert mgr.get("jira") is None
    assert mgr.get("confluence") is None
    assert mgr.get_active_services() == []


def test_get_active_services_preserves_canonical_order() -> None:
    mgr, _, _ = _make_manager()
    mgr.store("bitbucket", email="a@b.com", api_token="t", workspace="example_workspace")
    mgr.store("jira", email="a@b.com", api_token="t")

    assert mgr.get_active_services() == ["jira", "bitbucket"]


def test_bitbucket_cloud_allows_optional_workspace() -> None:
    mgr, _, _ = _make_manager()

    cred = mgr.store("bitbucket", email="bitbucket-user", api_token="t")

    assert cred.deployment == "cloud"
    assert cred.email == "bitbucket-user"
    assert cred.workspace == ""


def test_bitbucket_cloud_requires_username() -> None:
    mgr, _, _ = _make_manager()

    with pytest.raises(ValueError, match="BITBUCKET_USERNAME"):
        mgr.store("bitbucket", email="", api_token="t")


def test_bitbucket_server_allows_optional_project_key() -> None:
    mgr, _, _ = _make_manager()

    cred = mgr.store(
        "bitbucket",
        email="",
        api_token="t",
        deployment="server",
        url="https://bitbucket.example.com",
    )

    assert cred.deployment == "server"
    assert cred.workspace == ""


# ---------------------------------------------------------------------------
# Snapshot / observability - never echo raw tokens
# ---------------------------------------------------------------------------


def test_snapshot_never_contains_raw_token() -> None:
    mgr, _, _ = _make_manager()
    mgr.store("jira", email="alice@example.com", api_token="SECRET_TOK")
    snap = mgr.snapshot()
    assert "SECRET_TOK" not in str(snap)
    assert snap["credentials"]["jira"]["email"].startswith("ali")


def test_stored_credential_masked_email_hides_local_part() -> None:
    cred = StoredCredential(
        service="jira",
        email="averylongname@example.com",
        api_token="t",
        stored_at=0.0,
    )
    masked = cred.masked_email()
    assert masked == "ave***@example.com"
    assert "averylongname" not in masked
