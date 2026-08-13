"""
Player authentication for the CFB Game Streamlit app.

Passwords are stored in st.secrets["auth"][username] and matched against
the username column in the players table. No hash library needed — secrets
are server-side only and never exposed to the client.

Usage:
    - streamlit_app.py (the router) is the ONLY caller of require_login().
      It gates the whole app (nav sidebar included) and renders the
      logged-in sidebar block (name + logout + Help/WordPress links)
      exactly once per page load.
    - Individual pages that need the player dict call get_logged_in_player()
      instead — a plain session-state read with no rendering side effect.
      Calling require_login() again from a page would re-render the
      sidebar block a second time (this happened — see PROGRESS.md).
"""

import streamlit as st

from lib.config import CURRENT_SEASON_ID
from lib.db import get_player_by_username
from lib.links import WORDPRESS_HOME_URL
from lib.nav import HELP

_SESSION_KEY = "auth_player"


def require_login() -> dict:
    """
    Gate the app: if not logged in, render the login form and call
    st.stop(). If already logged in, render the sidebar block (name +
    logout + Help/WordPress links) and return the player dict.

    Call this exactly once per page load — from streamlit_app.py, before
    st.navigation(...).run(). Pages that need the player dict should call
    get_logged_in_player() instead, not this.
    """
    player = st.session_state.get(_SESSION_KEY)
    if player:
        _render_logout_button()
        return player

    st.title("Log in")
    username = st.text_input("Username", autocomplete="username")
    password = st.text_input("Password", type="password", autocomplete="current-password")

    if st.button("Log in"):
        username = username.lower()
        expected = st.secrets.get("auth", {}).get(username)
        if expected is not None and expected == password:
            record = get_player_by_username(username, CURRENT_SEASON_ID)
            if record:
                st.session_state[_SESSION_KEY] = record
                st.rerun()
        st.error("Invalid username or password.")

    st.stop()


def get_logged_in_player() -> dict:
    """
    Return the already-authenticated player dict — a plain session-state
    read, no rendering. Assumes require_login() already ran upstream
    (streamlit_app.py) to gate access; safe to call from any page.
    """
    return st.session_state[_SESSION_KEY]


def logout() -> None:
    """Clear the authenticated session and return to the login form."""
    st.session_state.pop(_SESSION_KEY, None)
    st.rerun()


def _render_logout_button() -> None:
    with st.sidebar:
        name = st.session_state[_SESSION_KEY]["name"]
        st.write(f"Logged in as **{name}**")
        if st.button("Log out"):
            logout()

        st.divider()
        st.page_link(HELP)
        st.link_button("WordPress site", WORDPRESS_HOME_URL, icon="🌐")
