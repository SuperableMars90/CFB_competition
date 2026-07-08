"""
Drop/add request page.

Flow: auth login -> submit a drop/add request (drop only, add only, or
both) -> request is validated and written to dropadd_requests as pending
-> a commissioner reviews pending requests below and approves or denies
them. Approval applies the roster change (rosters + drop_add_log) in the
same transaction as the status update — see lib.db.decide_dropadd_request.

Not yet decided (see TODO.md): weekly request limits, a lock window near
kickoff, and whether a player can cancel their own pending request.
"""

import streamlit as st

from lib.auth import get_logged_in_player
from lib.db import (
    get_active_roster_teams,
    get_available_teams,
    get_owned_team_ids,
    create_dropadd_request,
    get_dropadd_requests,
    decide_dropadd_request,
    get_current_week,
    DropAddRequestError,
)
from lib.dropadd_rules import validate_dropadd_request
from lib.config import CURRENT_SEASON_ID

NONE_LABEL = "— None —"

st.title("Drop / Add")

# T25: informational only — separate from the week input below.
_current_week = get_current_week(CURRENT_SEASON_ID)
if _current_week is not None:
    st.caption(f"Current week: {_current_week}")


# --- Auth + week picker -------------------------------------------------
player = get_logged_in_player()
player_id = player["id"]

week = st.number_input("Week", min_value=1, max_value=20, value=1, step=1)  # DEV SEAM
week = int(week)

season_id = CURRENT_SEASON_ID


# --- Submit a request -----------------------------------------------------
# A roster-less login (e.g. a commissioner checking approvals without
# owning a roster in this season/pod) must still reach the sections below,
# so this only skips the submission form — it must never st.stop() the page.
st.subheader("Request a drop/add")

roster = get_active_roster_teams(player_id, season_id)

if not roster:
    st.info("You have no active roster for the current season — nothing to drop/add.")
else:
    available = get_available_teams(season_id, player["pod_id"])
    roster_by_id = {t["team_id"]: t for t in roster}
    available_by_id = {t["team_id"]: t for t in available}

    def _fmt_roster(team_id):
        if team_id is None:
            return NONE_LABEL
        t = roster_by_id[team_id]
        return f"{t['name']} ({t['conference_abbreviation']})"

    def _fmt_available(team_id):
        if team_id is None:
            return NONE_LABEL
        t = available_by_id[team_id]
        return f"{t['name']} ({t['conference_abbreviation']})"

    drop_choice = st.selectbox(
        "Drop (optional)",
        options=[None] + list(roster_by_id.keys()),
        format_func=_fmt_roster,
    )
    add_choice = st.selectbox(
        "Add (optional)",
        options=[None] + list(available_by_id.keys()),
        format_func=_fmt_available,
    )
    notes = st.text_area("Notes for the commissioner (optional)")

    if st.button("Submit request", type="primary"):
        roster_team_ids = set(roster_by_id.keys())
        owned_team_ids = get_owned_team_ids(season_id, player["pod_id"])
        result = validate_dropadd_request(
            dropped_team_id=drop_choice,
            added_team_id=add_choice,
            roster_team_ids=roster_team_ids,
            owned_team_ids=owned_team_ids,
        )
        if not result.is_valid:
            for err in result.errors:
                st.error(err)
        else:
            create_dropadd_request(
                player_id=player_id,
                season_id=season_id,
                week=week,
                dropped_team_id=drop_choice,
                added_team_id=add_choice,
            )
            st.success("Request submitted — waiting on commissioner approval.")
            st.rerun()


# --- Your requests ----------------------------------------------------
st.divider()
st.subheader("Your requests")

my_requests = [
    r for r in get_dropadd_requests(season_id) if r["player_id"] == player_id
]
if not my_requests:
    st.write("No drop/add requests yet.")
else:
    for r in my_requests:
        drop_label = r["dropped_team_name"] or NONE_LABEL
        add_label = r["added_team_name"] or NONE_LABEL
        st.write(
            f"**Week {r['week']}** — drop {drop_label}, add {add_label} "
            f"— *{r['status']}* (requested {r['requested_at'].strftime('%b %d, %I:%M %p')})"
        )


# --- Commissioner review -----------------------------------------------
if player["is_commissioner"]:
    st.divider()
    st.subheader("Commissioner: pending requests")

    pending = get_dropadd_requests(season_id, status="pending")
    if not pending:
        st.write("No pending requests.")
    else:
        for r in pending:
            drop_label = r["dropped_team_name"] or NONE_LABEL
            add_label = r["added_team_name"] or NONE_LABEL
            st.write(
                f"**{r['player_name']}**, week {r['week']}: "
                f"drop {drop_label}, add {add_label}"
                + (f" — _{r['notes']}_" if r["notes"] else "")
            )
            col1, col2 = st.columns(2)
            if col1.button("Approve", key=f"approve_{r['id']}"):
                try:
                    decide_dropadd_request(r["id"], decided_by=player_id, approve=True)
                    st.success(f"Approved request {r['id']}.")
                    st.rerun()
                except DropAddRequestError as e:
                    st.error(str(e))
            if col2.button("Deny", key=f"deny_{r['id']}"):
                try:
                    decide_dropadd_request(r["id"], decided_by=player_id, approve=False)
                    st.info(f"Denied request {r['id']}.")
                    st.rerun()
                except DropAddRequestError as e:
                    st.error(str(e))
