"""Operator access control for the production dashboard (one shared secret: TRIPY_ACCESS_TOKEN).

Intentionally small: no user accounts, OAuth or roles. In production the dashboard FAILS CLOSED:

    TRIPY_ACCESS_TOKEN missing      → a configuration error only; nothing operational is rendered
    token set, session not unlocked → a minimal access screen only (password field + Unlock)
    correct token                   → st.session_state[SESSION_FLAG] = True (the secret itself is never stored)
    wrong token                     → "Invalid access token." (no length, partial match or environment detail)

Outside production the gate is open (local development stays convenient); access control is production-only.

The token is opaque: it is compared exactly as configured (no trimming or case folding), in constant time
(hmac.compare_digest over UTF-8 bytes). It is only ever read from the server environment / Streamlit secrets and only
ever entered through a password field, never from the URL (query string, path, fragment) or a cookie, so it cannot
leak through browser history, referrers or request logs. Its value is registered with the secret redaction in
src/app_config.py.

A per-session backoff slows guessing: after MAX_FAILURES wrong tokens the session must wait LOCKOUT_S seconds before
the next attempt (no sleep; the attempt is simply refused). A browser refresh starts a new Streamlit session (and so
requires the token again); the backoff is per session, so the real protection is a long random token.

Authentication concerns stay here: the RunManager, the research engine, diagnostics and storage know nothing of it.
"""

from __future__ import annotations

import hmac
import time
from typing import Callable, MutableMapping

from .app_config import Lookup, is_production

ACCESS_TOKEN_VAR = "TRIPY_ACCESS_TOKEN"
SESSION_FLAG = "tripy_authenticated"
FAILURES_KEY = "tripy_auth_failures"
LOCKED_UNTIL_KEY = "tripy_auth_locked_until"
MESSAGE_KEY = "tripy_auth_message"
INPUT_KEY = "tripy_access_token_input"
MAX_FAILURES = 5
LOCKOUT_S = 30.0

INVALID_MESSAGE = "Invalid access token."
MISSING_TITLE = "Production access control is not configured."
MISSING_DETAIL = "Set TRIPY_ACCESS_TOKEN in the deployment environment."


def _get(lookup: Lookup, name: str) -> str:
    try:
        return str(lookup(name) or "")
    except Exception:  # noqa: BLE001
        return ""


def access_required(lookup: Lookup) -> bool:
    """Access control applies in production only (TRIPY_ENV=production, or a Railway deployment)."""
    return is_production(lookup)


def expected_token(lookup: Lookup) -> str:
    """The configured token, opaque (whitespace-only counts as not configured)."""
    value = _get(lookup, ACCESS_TOKEN_VAR)
    return value if value.strip() else ""


def token_configured(lookup: Lookup) -> bool:
    return bool(expected_token(lookup))


def verify_token(submitted: object, expected: str) -> bool:
    """Constant-time comparison; False when nothing is configured or nothing was submitted."""
    if not expected or not isinstance(submitted, str) or not submitted:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def is_authenticated(state: MutableMapping) -> bool:
    return state.get(SESSION_FLAG) is True


def attempt(submitted: object, lookup: Lookup, state: MutableMapping,
            now: Callable[[], float] = time.monotonic) -> bool:
    """One unlock attempt. Updates only the session flag, the failure counter, the lockout deadline and a generic
    message; never stores the submitted or the expected value."""
    current = now()
    locked_until = float(state.get(LOCKED_UNTIL_KEY) or 0.0)
    if locked_until > current:
        state[MESSAGE_KEY] = f"Too many attempts. Try again in {int(locked_until - current) + 1} s."
        return False
    if verify_token(submitted, expected_token(lookup)):
        state[SESSION_FLAG] = True
        for key in (FAILURES_KEY, LOCKED_UNTIL_KEY, MESSAGE_KEY):
            state.pop(key, None)
        return True
    failures = int(state.get(FAILURES_KEY) or 0) + 1
    state[FAILURES_KEY] = failures
    state[MESSAGE_KEY] = INVALID_MESSAGE
    if failures >= MAX_FAILURES:
        state[FAILURES_KEY] = 0
        state[LOCKED_UNTIL_KEY] = current + LOCKOUT_S
    return False


def logout(state: MutableMapping) -> None:
    """Lock this browser session only: runs, history and configuration are untouched."""
    for key in (SESSION_FLAG, MESSAGE_KEY):
        state.pop(key, None)


# --- Streamlit -------------------------------------------------------------------------------------------------------

def _unlock_callback(lookup: Lookup) -> None:
    """Runs before the rerun: read the password field, verify, then clear the field so the secret is not kept in
    session state."""
    import streamlit as st

    submitted = st.session_state.get(INPUT_KEY)
    st.session_state[INPUT_KEY] = ""
    attempt(submitted, lookup, st.session_state)


def gate(lookup: Lookup) -> bool:
    """Render the access screen when needed. Returns True when the dashboard may render; on False the caller must stop
    the script (st.stop()) before touching any operational state."""
    import streamlit as st

    if not access_required(lookup):
        return True
    if not token_configured(lookup):
        st.markdown('<div class="tripy-brand">TRIPY</div><div class="tripy-sub">Private research dashboard</div>',
                    unsafe_allow_html=True)
        st.error(f"**{MISSING_TITLE}**\n\n{MISSING_DETAIL}")
        return False
    if is_authenticated(st.session_state):
        return True
    st.markdown('<div class="tripy-brand">TRIPY</div><div class="tripy-sub">Private research dashboard</div>',
                unsafe_allow_html=True)
    with st.form("tripy_access", clear_on_submit=True):
        st.text_input("Access token", type="password", key=INPUT_KEY, autocomplete="current-password")
        st.form_submit_button("Unlock", type="primary", on_click=_unlock_callback, args=(lookup,))
    message = st.session_state.get(MESSAGE_KEY)
    if message:
        st.error(message)
    return False


def render_logout(lookup: Lookup) -> None:
    """A small Logout control (only when access control is enforced)."""
    import streamlit as st

    if access_required(lookup) and is_authenticated(st.session_state):
        st.button("Logout", key="tripy_logout", on_click=logout, args=(st.session_state,),
                  help="Locks this browser session. Running research continues.")
