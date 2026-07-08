"""
lib/nav.py
-----------
Single source of truth for this app's pages, using Streamlit's
st.Page/st.navigation API (replaces the old pages/ directory
auto-discovery convention, where a page's sidebar title was tied to its
filename with no way to override it).

Page files live in app_pages/, not pages/ — Streamlit's script runner
auto-detects a *directory literally named* pages/ next to the entrypoint
and silently switches to its legacy v1 multi-page routing, which
dispatches straight to the requested page's script without ever running
streamlit_app.py's own top-level code (the require_login() gate
included). That caused a real crash: a session with no auth_player in
session state landing directly on a sub-page. Keep this directory named
anything other than pages/ or the gate gets bypassed again.

streamlit_app.py imports this to build the navigation. Any page that
needs to link to another page (e.g. a "Help" link in a sidebar, or the
Welcome page's own nav cards) should import the StreamlitPage objects
here rather than hardcoding path strings, so a renamed/moved page file
only needs to be updated in one place.
"""

import streamlit as st

WELCOME = st.Page("app_pages/welcome.py", title="Welcome", icon="🏠", default=True)
SUBMIT_LINEUP = st.Page("app_pages/submit_lineup.py", title="Submit Lineup", icon="📝")
DROP_ADD = st.Page("app_pages/drop_add.py", title="Drop / Add", icon="🔄")
ROSTERS = st.Page("app_pages/rosters.py", title="Rosters", icon="📋")
HISTORY = st.Page("app_pages/history.py", title="History", icon="🕘")
HELP = st.Page("app_pages/help.py", title="Help", icon="❓")
DRAFT = st.Page("app_pages/draft.py", title="Draft", icon="🏈")

ALL_PAGES = [WELCOME, SUBMIT_LINEUP, DROP_ADD, ROSTERS, HISTORY, HELP, DRAFT]


def visible_pages(player: dict) -> list:
    """
    Per-rerun page list -- same as ALL_PAGES but drops DRAFT once this
    player's own pod has finished its draft. Call this fresh every rerun
    from streamlit_app.py; this module's own top-level code (including
    ALL_PAGES above) only runs once per worker process, so it can't react
    to draft-completion state changing mid-season on its own.
    """
    from lib.config import CURRENT_SEASON_ID
    from lib.db import is_draft_complete

    if is_draft_complete(CURRENT_SEASON_ID, player["pod_id"]):
        return [p for p in ALL_PAGES if p is not DRAFT]
    return ALL_PAGES
