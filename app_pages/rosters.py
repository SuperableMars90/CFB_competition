"""
Rosters page — read-only reference showing every player's current teams
plus free agents, grouped by conference. Non-interactive: no forms, no
buttons, nothing that writes to the DB. To request a roster change, use
the Drop / Add page instead.

Grouped by pod (Zach, 2026-07-05): pods are completely severed rosters,
each with its own independent claim on the full team universe, so
"Unclaimed" is rendered once per pod, not once for the whole season --
a team on another pod's roster is still a legitimate free agent here.
"""

import pandas as pd
import streamlit as st

from lib.db import get_active_roster_teams, get_available_teams, get_pods, get_pod_players
from lib.links import WORDPRESS_ROSTERS_RECORDS_URL
from lib.config import CURRENT_SEASON_ID

# Auth is already gated by streamlit_app.py — this page doesn't need the
# player dict, so no lib.auth call at all (see lib/auth.py's docstring).

st.title("Rosters")
st.caption(
    f"Current teams only — for season history and awards, see the "
    f"[full Rosters & Records page]({WORDPRESS_ROSTERS_RECORDS_URL}) on WordPress."
)

season_id = CURRENT_SEASON_ID


def _render_teams(teams: list[dict]) -> None:
    if not teams:
        st.write("None.")
        return
    df = pd.DataFrame(teams)[["conference_abbreviation", "name"]]
    df.columns = ["Conference", "Team"]
    df = df.sort_values(["Conference", "Team"]).reset_index(drop=True)
    st.dataframe(df, hide_index=True, width="stretch")


for pod in get_pods(season_id):
    st.header(pod["name"])

    st.subheader("Unclaimed")
    _render_teams(get_available_teams(season_id, pod["id"]))

    for pod_player in get_pod_players(pod["id"]):
        st.subheader(pod_player["name"])
        _render_teams(get_active_roster_teams(pod_player["id"], season_id))
