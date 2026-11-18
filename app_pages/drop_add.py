"""
Drop/add request page.

Flow: auth login -> submit a drop/add request (drop only, add only, or
both) -> request is validated and written to dropadd_requests as pending
-> a commissioner reviews pending requests below, marks any subset
Approve / Deny, and applies them as one all-or-nothing batch. The batch
is re-validated against current rosters (stale drops, duplicate drops or
adds, roster size) before anything is written — see
lib.db.decide_dropadd_requests.

Not yet decided (see TODO.md): weekly request limits, a lock window near
kickoff, and whether a player can cancel their own pending request.
"""

import pandas as pd
import streamlit as st

from lib.auth import get_logged_in_player
from lib.db import (
    get_active_roster_teams,
    get_available_teams,
    get_owned_team_ids,
    create_dropadd_request,
    get_dropadd_requests,
    decide_dropadd_requests,
    get_current_week,
    DropAddBatchError,
    DropAddRequestError,
)
from lib.dropadd_rules import validate_dropadd_request, find_shared_drops
from lib.config import CURRENT_SEASON_ID

NONE_LABEL = "— None —"
LEAVE, APPROVE, DENY = "—", "Approve", "Deny"

st.title("Drop / Add")

# The request is always recorded against the current week — there's no
# reason for the player to pick one; a request takes effect (on approval)
# immediately regardless of what week is stored, and week is kept purely as
# the audit-trail label on dropadd_requests / drop_add_log (useful after the
# fact for "which week did this team get dropped"). Removed the old
# free-entry "Week" number input (a DEV SEAM) that let a player record any
# week 1-20 regardless of when they actually asked.
_current_week = get_current_week(CURRENT_SEASON_ID)
if _current_week is not None:
    st.caption(f"Current week: {_current_week}")

if st.session_state.pop("dropadd_just_submitted", False):
    st.success("Your drop/add request was submitted — waiting on commissioner approval.")


# --- Auth ------------------------------------------------------------------
player = get_logged_in_player()
player_id = player["id"]

season_id = CURRENT_SEASON_ID


# --- Submit a request -----------------------------------------------------
# A roster-less login (e.g. a commissioner checking approvals without
# owning a roster in this season/pod) must still reach the sections below,
# so this only skips the submission form — it must never st.stop() the page.
st.subheader("Request a drop/add")

roster = get_active_roster_teams(player_id, season_id)

if _current_week is None:
    st.info("No current week could be determined for this season, so requests can't be recorded — contact the commissioner.")
elif not roster:
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
                week=_current_week,
                dropped_team_id=drop_choice,
                added_team_id=add_choice,
                notes=notes.strip() if notes else None,
            )
            # st.rerun() below restarts the script immediately, so a
            # st.success() called here would only flash for an instant.
            # Stash it in session_state and render it near the top of the
            # next run instead, where it actually stays visible.
            st.session_state["dropadd_just_submitted"] = True
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

    batch_msg = st.session_state.pop("dropadd_batch_done", None)
    if batch_msg:
        st.success(batch_msg)

    pending = get_dropadd_requests(season_id, status="pending")
    if not pending:
        st.write("No pending requests.")
    else:
        pending_ids = {r["id"] for r in pending}
        shared = find_shared_drops(pending)

        sort_mode = st.radio(
            "Sort by", ["Date/time", "Player"], horizontal=True, key="dropadd_sort"
        )
        if sort_mode == "Player":
            rows = sorted(pending, key=lambda r: (r["player_name"], r["requested_at"], r["id"]))
        else:
            rows = sorted(pending, key=lambda r: (r["requested_at"], r["id"]))

        # Chosen actions live in session_state keyed by request id, so they
        # survive a sort change. The editor's own edits are positional, so
        # each (sort, pending-set) combination gets its own editor key, and
        # its base data is snapshotted once when that key first appears —
        # the data passed to a keyed editor must not change under it.
        actions: dict[int, str] = st.session_state.setdefault("dropadd_actions", {})
        for rid in list(actions):
            if rid not in pending_ids:
                del actions[rid]
        editor_key = f"dropadd_editor_{sort_mode}_{hash(tuple(sorted(pending_ids)))}"
        if st.session_state.get("dropadd_editor_key") != editor_key:
            st.session_state["dropadd_editor_key"] = editor_key
            st.session_state["dropadd_editor_base"] = dict(actions)
        base = st.session_state["dropadd_editor_base"]

        df = pd.DataFrame([
            {
                "#": r["id"],
                "Action": base.get(r["id"], LEAVE),
                "Player": r["player_name"],
                "Requested": r["requested_at"],
                "Wk": r["week"],
                "Drop": r["dropped_team_name"] or NONE_LABEL,
                "Add": r["added_team_name"] or NONE_LABEL,
                "Flag": "⚠ same drop" if r["id"] in shared else "",
                "Note": r["notes"] or "",
            }
            for r in rows
        ]).set_index("#")

        st.caption(
            "Set Action on the rows you want to decide; rows left at — stay "
            "pending. ⚠ same drop = this player has several pending requests "
            "dropping the same team (ranked fallbacks) — approve at most one."
        )
        edited = st.data_editor(
            df,
            key=editor_key,
            width="stretch",
            disabled=[c for c in df.columns if c != "Action"],
            column_config={
                "Action": st.column_config.SelectboxColumn(
                    options=[LEAVE, APPROVE, DENY], required=True, width="small"
                ),
                "Requested": st.column_config.DatetimeColumn(format="MMM D, h:mm:ss a"),
                "Note": st.column_config.TextColumn(width="large"),
            },
        )
        for rid, act in edited["Action"].items():
            actions[int(rid)] = act

        to_apply = [(rid, act == APPROVE) for rid, act in actions.items() if act in (APPROVE, DENY)]
        n_approve = sum(1 for _, a in to_apply if a)
        n_deny = len(to_apply) - n_approve
        st.write(f"**{n_approve}** to approve, **{n_deny}** to deny.")

        if st.button("Apply decisions", type="primary", disabled=not to_apply):
            try:
                decide_dropadd_requests(to_apply, decided_by=player_id)
            except DropAddBatchError as e:
                st.error(
                    "Nothing was applied. Fix these and apply again:\n\n"
                    + "\n".join(f"- {err}" for err in e.errors)
                )
            except DropAddRequestError as e:
                st.error(f"Nothing was applied: {e}")
            else:
                st.session_state["dropadd_batch_done"] = (
                    f"Applied {n_approve} approval(s) and {n_deny} denial(s)."
                )
                for k in ("dropadd_actions", "dropadd_editor_key", "dropadd_editor_base"):
                    st.session_state.pop(k, None)
                st.rerun()
