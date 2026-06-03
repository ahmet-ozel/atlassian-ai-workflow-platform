"""Streamlit dept switcher.

Dept seçimi zorunlu; dept seçilmeden hiçbir sayfa açılmaz. Dropdown
changes trigger an automatic probe and tooltip update. Multi-dept users
persist their selection in a signed cookie, and dept changes reset the
dept-scoped session state. The default dept comes from the OIDC claim,
the cookie fallback, or the first allowed dept.

The component is intentionally pure: every collaborator (cookie reader,
signed cookie writer, probe runner, API client) is injected through
``streamlit.session_state`` keys so unit tests can drive it via
:class:`streamlit.testing.v1.AppTest` without standing up the
production HTTP / cookie stack.

When ``selected != active_dept_id``, the component:

1. Snapshots ``user`` and ``auth_token`` from ``st.session_state``.
2. Deletes every other key (chat history, workflow cache, credential
   cache, dept_probe).
3. Re-installs the snapshot.
4. Stores the new ``active_dept_id`` and writes a 30-day signed cookie.
5. Re-runs the probe and triggers ``st.rerun()`` so the page reloads
   under the new dept context.

Anything that wants to survive a dept switch MUST live in
:data:`_KEEP_KEYS` (``user`` + ``auth_token``); everything else is
considered dept-scoped state and dropped on switch.
"""

from __future__ import annotations

from typing import Any, Final, Iterable, Mapping

import streamlit as st

__all__ = ["render_dept_switcher", "clear_session_except_user"]


#: Session-state keys that survive a dept switch. Keeping the auth
#: surface intact lets the user stay logged in; everything else is
#: dept-scoped and must be reset to prevent dept-A data leaking into
#: dept-B.
_KEEP_KEYS: Final[frozenset[str]] = frozenset({"user", "auth_token"})


#: Cookie name used for multi-dept persistence. The value is
#: a signed JWS so a tampered cookie can never reroute the user to a
#: dept they were not granted (RBAC check still runs server-side; the
#: signature only protects the *default selection*).
_DEPT_COOKIE_NAME: Final[str] = "active_dept_id"

#: Cookie TTL — long enough that a returning user lands on the same
#: dept they last used, short enough that a stale cookie does not
#: outlive a typical role rotation.
_DEPT_COOKIE_TTL_DAYS: Final[int] = 30


# ---------------------------------------------------------------------------
# Cookie / probe seams (kept tiny so AppTest can inject fakes)
# ---------------------------------------------------------------------------


def _read_cookie(name: str) -> str | None:
    """Read a signed cookie via the helper installed on session state.

    Production wiring populates ``st.session_state["_cookie_reader"]``
    at app boot with a callable that consumes ``streamlit-cookies-controller``
    or an equivalent signed-cookie store. Tests inject a dict-backed
    fake. Returning ``None`` is the safe default — the caller falls
    back to OIDC default_dept_id, then to the first allowed dept.

    For the department cookie (COOKIE_NAME), this reads and verifies
    the HMAC signature using the cookie_manager module. If the primary
    cookie is not found, falls back to the ``dept_selection`` cookie
    written by ``write_department_cookie``.
    """
    from components.cookie_manager import (
        COOKIE_NAME,
        verify_cookie,
        _get_secret,
    )

    reader = st.session_state.get("_cookie_reader")
    if reader is None:
        return None

    # For department cookies, try the requested name first, then fall
    # back to the dept_selection cookie for cross-component compatibility.
    names_to_try = [name]
    if name == _DEPT_COOKIE_NAME and COOKIE_NAME != _DEPT_COOKIE_NAME:
        names_to_try.append(COOKIE_NAME)

    for cookie_name in names_to_try:
        try:
            raw_value = reader(cookie_name)
        except Exception:  # noqa: BLE001 — best-effort; missing cookie is fine
            continue

        if not raw_value:
            continue

        # If reading the department cookie, verify the HMAC signature
        if cookie_name in (COOKIE_NAME, _DEPT_COOKIE_NAME):
            secret = _get_secret()
            verified = verify_cookie(raw_value, secret)
            if verified is None:
                # Invalid signature — delete the tampered cookie (Req 10.5)
                try:
                    reader.delete(cookie_name)
                except Exception:  # noqa: BLE001
                    pass
                continue
            return verified

        return raw_value

    return None


def _write_cookie(name: str, value: str, *, ttl_days: int) -> None:
    """Write a signed cookie via the helper installed on session state.

    For the department cookie, signs the value with HMAC-SHA256 before
    writing. Also writes to the ``dept_selection``
    cookie via ``write_department_cookie`` to keep both cookie stores
    in sync.
    """
    from components.cookie_manager import (
        COOKIE_NAME,
        sign_cookie,
        write_department_cookie,
        _get_secret,
    )

    writer = st.session_state.get("_cookie_writer")
    if writer is None:
        return

    # Sign the cookie value before writing (Req 10.2)
    if name in (COOKIE_NAME, _DEPT_COOKIE_NAME):
        secret = _get_secret()
        signed_value = sign_cookie(value, secret)
    else:
        signed_value = value

    try:
        writer(name, signed_value, ttl_days=ttl_days)
    except Exception:  # noqa: BLE001 — non-fatal
        pass

    # Also write to the dept_selection cookie so app.py's initial
    # load picks up the latest selection (Req 10.2, 10.3).
    if name == _DEPT_COOKIE_NAME:
        try:
            write_department_cookie(value)
        except Exception:  # noqa: BLE001 — non-fatal
            pass


def _run_probe(dept_id: str) -> Mapping[str, Any]:
    """Run the dept connectivity probe.

    Production wiring posts to ``automation-service /admin/probe/dry-run``;
    the result is a small dict keyed by service (``jira``, ``bitbucket``,
    ``confluence``, ``firecrawl``) with ``ok`` / ``failed`` status.

    Tests stub ``st.session_state["_probe_runner"]`` with a callable
    returning a fixed mapping.
    """

    runner = st.session_state.get("_probe_runner")
    if runner is None:
        return {}
    try:
        return runner(dept_id) or {}
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def clear_session_except_user(
    state: Any,
    *,
    keep: Iterable[str] = _KEEP_KEYS,
) -> None:
    """Drop every session_state key except the auth-related ones.

    Exposed as a public helper so the reset test can call it directly
    with synthetic session-state dicts. Production code
    routes through :func:`render_dept_switcher`, which calls this
    function on every dept change.
    """

    keep_set = frozenset(keep)
    snapshot = {k: state[k] for k in list(state.keys()) if k in keep_set}
    for key in list(state.keys()):
        del state[key]
    for key, value in snapshot.items():
        state[key] = value


def render_dept_switcher() -> str:
    """Render the mandatory dept dropdown and return the selected dept_id.

    The function blocks the page (``st.stop()``) when the user has
    been granted no departments — page bodies SHOULD call this
    function as their first sidebar widget so a misconfigured user
    never reaches dept-scoped data.

    Returns:
        The active dept id. The same value is also stored on
        ``st.session_state.active_dept_id`` for downstream pages and
        on a 30-day signed cookie for the next session.
    """

    user = st.session_state.get("user")
    if not user:
        st.error("Oturum bulunamadı; lütfen yeniden giriş yapın.")
        st.stop()

    user_depts: list[str] = list(user.get("dept_ids", []))
    if not user_depts:
        st.error(
            "Hesabınız hiçbir departmana atanmamış. "
            "Yöneticiyle iletişime geçin."
        )
        st.stop()

    # Default: OIDC claim → cookie → first allowed dept.
    default = (
        user.get("default_dept_id")
        or _read_cookie(_DEPT_COOKIE_NAME)
        or user_depts[0]
    )
    if default not in user_depts:
        default = user_depts[0]

    selected = st.sidebar.selectbox(
        "Departman",
        user_depts,
        index=user_depts.index(default),
        key="dept_select",
        help=(
            "Dept seçimi tüm sayfaları o departmanın verisine "
            "kapsar. Departman değiştirmek session içeriğini "
            "(chat, workflow listesi, credential cache) sıfırlar."
        ),
    )

    previous = st.session_state.get("active_dept_id")
    if selected != previous:
        # ---- Full session reset on dept change ------------------
        clear_session_except_user(st.session_state)
        st.session_state["active_dept_id"] = selected
        _write_cookie(
            _DEPT_COOKIE_NAME, selected, ttl_days=_DEPT_COOKIE_TTL_DAYS
        )
        # ---- Auto-probe + tooltip -------------------------------
        st.session_state["dept_probe"] = _run_probe(selected)
        st.rerun()

    # First render — populate state if missing (covers the "no
    # previous selection" path; no rerun needed because the value
    # is already in the dropdown).
    if previous is None:
        st.session_state["active_dept_id"] = selected
        if "dept_probe" not in st.session_state:
            st.session_state["dept_probe"] = _run_probe(selected)

    # Show probe state as a tooltip-equivalent caption under the
    # dropdown so the user sees connectivity at a glance.
    probe = st.session_state.get("dept_probe") or {}
    if probe:
        ok = sum(1 for v in probe.values() if isinstance(v, dict) and v.get("status") == "ok")
        total = sum(1 for v in probe.values() if isinstance(v, dict))
        if total:
            st.sidebar.caption(f"Probe: {ok}/{total} servis hazır")

    return selected
