"""
auth.py — Per-person login for the app.

This exists because the app itself has no access control by default: on
Streamlit Community Cloud, anyone with the URL can open it, upload data,
and see everything already uploaded — completely independent of whether
the GitHub repo is public or private. This module is what actually closes
that gap.

Credentials live ONLY in Streamlit secrets (.streamlit/secrets.toml
locally — already gitignored — or the "Secrets" panel in the Streamlit
Cloud app settings when deployed), never in source code, never committed
to the repo, and never compared in plaintext: only a salted SHA-256 hash
of each password is stored, and verification uses a constant-time
comparison (hmac.compare_digest) to avoid leaking timing information
about how much of the password matched.

Secrets format expected (see .streamlit/secrets.toml.example):

    [auth]
    [auth.users]
    alice = "<salt>$<hash>"
    bob   = "<salt>$<hash>"

Use modules/generate_password_hash.py to create the "<salt>$<hash>" value
for a new user's password — never type a real password directly into
secrets.toml or anywhere else in plain form.

Every page should call require_login() as the very first thing, before
rendering any content — this is what makes the page show a login form
instead of the real content when nobody (or the wrong somebody) is
logged in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets_module
from typing import Optional

import streamlit as st

_SESSION_KEY = "_authenticated_user"
_LOGIN_ATTEMPTED_KEY = "_login_attempted"


def _hash_password(password: str, salt: str) -> str:
    """SHA-256 of salt+password, hex-encoded. Matches generate_password_hash.py."""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _verify_password(password: str, stored_salt_and_hash: str) -> bool:
    """
    Verify a password against a stored "salt$hash" string using a
    constant-time comparison, so a wrong-password response can't be used
    to infer how many characters were correct via response-time timing.
    """
    try:
        salt, expected_hash = stored_salt_and_hash.split("$", 1)
    except ValueError:
        return False
    actual_hash = _hash_password(password, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


def _get_configured_users() -> dict:
    """
    Read the configured users from Streamlit secrets. Returns {} (and lets
    the caller show a clear setup message) if secrets aren't configured
    yet, rather than crashing the whole app with a raw KeyError — this
    matters especially right after first deploying, before secrets have
    been added in the Streamlit Cloud settings panel.
    """
    try:
        return dict(st.secrets["auth"]["users"])
    except Exception:
        return {}


def is_logged_in() -> bool:
    return bool(st.session_state.get(_SESSION_KEY))


def current_user() -> Optional[str]:
    """The username of whoever is currently logged in, or None."""
    return st.session_state.get(_SESSION_KEY)


def logout() -> None:
    st.session_state.pop(_SESSION_KEY, None)
    st.session_state.pop(_LOGIN_ATTEMPTED_KEY, None)


def require_login() -> None:
    """
    Call this as the very first thing on every page (after st.set_page_config,
    before anything else is rendered). If nobody is logged in yet, this
    renders a login form and stops the rest of the page from running at
    all (st.stop()) — so no data, charts, or upload controls ever render
    behind the login wall.
    """
    if is_logged_in():
        return

    users = _get_configured_users()

    st.title("🔒 Sign in")
    st.caption("Encalm Group — Revenue Analytics System")

    if not users:
        st.error(
            "No users are configured yet. An administrator needs to add "
            "credentials under `[auth.users]` in this app's Secrets "
            "(Streamlit Cloud → App settings → Secrets, or "
            "`.streamlit/secrets.toml` when running locally) before anyone "
            "can sign in. See `modules/generate_password_hash.py` for how "
            "to generate a password entry safely (hashed, never plaintext)."
        )
        st.stop()

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        st.session_state[_LOGIN_ATTEMPTED_KEY] = True
        stored = users.get(username.strip())
        if stored and _verify_password(password, stored):
            st.session_state[_SESSION_KEY] = username.strip()
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    st.stop()


def render_user_badge() -> None:
    """
    Small sidebar widget showing who's logged in plus a logout button —
    call this from every page after require_login() passes, so it's
    always visible alongside the page content.
    """
    user = current_user()
    if not user:
        return
    with st.sidebar:
        st.caption(f"👤 Signed in as **{user}**")
        if st.button("Log out", key="_logout_button", use_container_width=True):
            logout()
            st.rerun()
