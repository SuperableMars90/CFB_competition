"""
Welcome page — sign-in landing content. Rapid-fire navigation only;
detailed instructions live on the Help page. Meant to be scannable in a
few seconds, not read.

Auth is already gated by streamlit_app.py before this page can be
reached; get_logged_in_player() here just reads the player dict back out
of session state (no rendering side effect — see lib/auth.py).
"""

import streamlit as st

from lib.auth import get_logged_in_player
from lib.config import CURRENT_SEASON_ID
from lib.db import get_current_week, is_draft_complete
from lib.links import WORDPRESS_HOME_URL, WORDPRESS_ROSTERS_RECORDS_URL
from lib.nav import SUBMIT_LINEUP, DROP_ADD, ROSTERS, HISTORY, HELP, DRAFT

st.title("CFB Game — Player App")

current_week = get_current_week(CURRENT_SEASON_ID)
if current_week is not None:
    st.caption(f"Current week: {current_week}")

player = get_logged_in_player()

st.write(f"Welcome, **{player['name']}**.")

st.subheader("Pages")
if not is_draft_complete(CURRENT_SEASON_ID, player["pod_id"]):
    st.page_link(DRAFT, help="Live draft — make your pick when it's your turn")
st.page_link(SUBMIT_LINEUP, help="Pick your 16 teams for a given week")
st.page_link(DROP_ADD, help="Request to swap a team on your roster")
st.page_link(ROSTERS, help="See everyone's teams and which teams are still free agents")
st.page_link(HISTORY, help="Review a past week's submitted lineup")
st.page_link(HELP, help="Detailed instructions for using this app")

st.divider()
st.markdown(
    f"Full game rules/strategy, live standings, and historical records live on the "
    f"WordPress site: [Rosters & Records]({WORDPRESS_ROSTERS_RECORDS_URL}) · "
    f"[Home]({WORDPRESS_HOME_URL})"
)
