"""
Live draft page.

Flow: commissioner sets the round-1 pick order once, before any picks are
made -> draft_order is pre-populated with the entire draft's schedule
(all rounds), team_selected left NULL -> players pick in turn, reading
directly off draft_order's lowest-pick_number still-open rows -> select
team(s) -> Review -> Confirm writes the pick(s) atomically. At a snake
round boundary, the same player is on the clock twice in a row, so both
picks are reviewed/confirmed together in one submission.

Section 1 (top): the interactive pick area.
Section 2: everyone's rosters so far (sortable/filterable).
Section 3: the full pick log, in order.
"""

import pandas as pd
import streamlit as st

from lib.auth import get_logged_in_player
from lib.config import CURRENT_SEASON_ID
from lib.db import (
    get_pod_players,
    get_draft_page_state,
    set_draft_order,
    submit_draft_picks,
    DraftSetupError,
    DraftOutOfSyncError,
)
from lib.draft import SortBy, TierFilter, filter_and_sort_teams, is_double_pick, ordinal

TIER_LABELS = {
    TierFilter.ALL: "All",
    TierFilter.P4: "P4",
    TierFilter.G6: "G6",
    TierFilter.CONFERENCE: "Specific conference",
}

st.title("Draft")

player = get_logged_in_player()
player_id = player["id"]
pod_id = player["pod_id"]
season_id = CURRENT_SEASON_ID

state = get_draft_page_state(season_id, pod_id)
pod_players = get_pod_players(pod_id)
player_names = {p["id"]: p["name"] for p in pod_players}


def _team_filter_controls(key_prefix: str, teams_for_conference_options: list):
    """Shared sort/filter row -- returns (sort_by, tier, conference)."""
    col1, col2, col3 = st.columns(3)
    with col1:
        sort_by = st.selectbox(
            "Sort by", list(SortBy), format_func=lambda s: s.value.title(),
            key=f"{key_prefix}_sort_by",
        )
    with col2:
        tier = st.selectbox(
            "Filter", list(TierFilter), format_func=lambda t: TIER_LABELS[t],
            key=f"{key_prefix}_tier_filter",
        )
    with col3:
        conference = None
        if tier == TierFilter.CONFERENCE:
            confs = sorted({t["conference_abbreviation"] for t in teams_for_conference_options if t.get("conference_abbreviation")})
            conference = st.selectbox("Conference", confs, key=f"{key_prefix}_conference_filter") if confs else None
    return sort_by, tier, conference


# --- Gate 0: commissioner setup, only while the draft hasn't been initialized ---
if not state.all_slots:
    if not player["is_commissioner"]:
        st.info("Waiting on the commissioner to set the round 1 pick order.")
        st.stop()

    st.subheader("Set the round 1 pick order")
    st.caption("Round 2 reverses this order, round 3 repeats it, and so on (standard snake draft).")

    order = []
    player_ids = list(player_names)
    for i in range(len(pod_players)):
        order.append(
            st.selectbox(
                f"Pick {i + 1}", options=player_ids,
                format_func=lambda pid: player_names[pid],
                key=f"draft_setup_pick_{i}",
            )
        )

    if st.button("Confirm order", type="primary"):
        if len(set(order)) != len(order):
            st.error("Each player must be selected exactly once.")
        else:
            try:
                set_draft_order(season_id, pod_id, order, set_by=player_id)
            except DraftSetupError as e:
                st.error(str(e))
            else:
                st.rerun()
    st.stop()


# --- Section 1: pick area ---------------------------------------------------
open_slots = state.open_slots[:2]

if not open_slots:
    st.success("The draft is complete!")
elif open_slots[0].player_id != player_id:
    st.session_state.pop("draft_pending_picks", None)
    slot = open_slots[0]
    on_clock_name = player_names.get(slot.player_id, f"Player {slot.player_id}")
    st.info(
        f"Round {slot.round_number}, pick {slot.pick_number}: **{on_clock_name}** is on the clock."
    )
    st.button("Check whose turn it is")
    st.caption("Use this button, not your browser's refresh — a browser refresh logs you out.")
else:
    pending = st.session_state.get("draft_pending_picks")

    if not pending:
        is_double = is_double_pick(open_slots[0], open_slots[1] if len(open_slots) > 1 else None)
        slots_to_fill = open_slots[:2] if is_double else open_slots[:1]

        st.success("Your pick — twice in a row (round boundary)!" if is_double else "Your pick!")

        sort_by, tier, conference = _team_filter_controls("draft_pick", state.available_teams)
        pool = filter_and_sort_teams(state.available_teams, tier=tier, conference=conference, sort_by=sort_by)
        pool_by_id = {t["team_id"]: t for t in pool}

        if not pool:
            st.warning("No teams match the current filter.")
        else:
            with st.form("draft_pick_form"):
                chosen = [
                    st.selectbox(
                        f"Pick {slot.pick_number} (Round {slot.round_number})",
                        options=list(pool_by_id),
                        format_func=lambda tid: f"{pool_by_id[tid]['name']} ({pool_by_id[tid]['conference_abbreviation']})",
                        key=f"draft_pick_choice_{slot.pick_number}",
                    )
                    for slot in slots_to_fill
                ]
                submitted = st.form_submit_button(
                    "Review pick" if len(chosen) == 1 else "Review both picks", type="primary",
                )

            if submitted:
                if len(set(chosen)) != len(chosen):
                    st.error("You selected the same team twice — pick two different teams.")
                else:
                    st.session_state["draft_pending_picks"] = {
                        "team_ids": chosen,
                        "team_names": [pool_by_id[tid]["name"] for tid in chosen],
                        "slots": slots_to_fill,
                    }
                    st.rerun()
    else:
        team_ids = pending["team_ids"]
        team_names_chosen = pending["team_names"]
        slots = pending["slots"]

        if len(team_ids) == 1:
            st.write(f"You have selected **{team_names_chosen[0]}** as your **{ordinal(slots[0].pick_number)}** pick.")
        else:
            st.write(
                f"You have selected **{team_names_chosen[0]}** and **{team_names_chosen[1]}** as your "
                f"**{ordinal(slots[0].pick_number)}** and **{ordinal(slots[1].pick_number)}** picks."
            )

        col_confirm, col_change = st.columns(2)
        with col_confirm:
            if st.button("Confirm", type="primary"):
                try:
                    submit_draft_picks(season_id, pod_id, player_id, team_ids)
                except DraftOutOfSyncError as e:
                    st.error(str(e))
                st.session_state.pop("draft_pending_picks", None)
                st.rerun()
        with col_change:
            if st.button("Change selection"):
                st.session_state.pop("draft_pending_picks", None)
                st.rerun()


# --- Section 2: everyone's rosters so far -----------------------------------
st.divider()
st.subheader("Rosters so far")

roster_teams_for_filter = [
    {"conference_abbreviation": s.conference_abbreviation} for s in state.pick_log
]
roster_sort_by, roster_tier, roster_conference = _team_filter_controls("draft_roster", roster_teams_for_filter)

picks_by_player: dict = {}
for s in state.pick_log:
    picks_by_player.setdefault(s.player_id, []).append({
        "team_id": s.team_id, "name": s.team_name,
        "conference_abbreviation": s.conference_abbreviation, "tier": s.tier,
    })

for pod_player in pod_players:
    st.markdown(f"**{pod_player['name']}**")
    teams = picks_by_player.get(pod_player["id"], [])
    if not teams:
        st.write("No picks yet.")
        continue
    filtered = filter_and_sort_teams(teams, tier=roster_tier, conference=roster_conference, sort_by=roster_sort_by)
    if not filtered:
        st.write("None match the current filter.")
        continue
    df = pd.DataFrame(filtered)[["conference_abbreviation", "name"]]
    df.columns = ["Conference", "Team"]
    st.dataframe(df, hide_index=True, width="stretch")


# --- Section 3: full pick log ------------------------------------------------
st.divider()
st.subheader("Pick log")

if not state.pick_log:
    st.write("No picks yet.")
else:
    df = pd.DataFrame([
        {
            "Pick": s.pick_number, "Round": s.round_number,
            "Player": player_names.get(s.player_id, s.player_id), "Team": s.team_name,
        }
        for s in state.pick_log
    ])
    st.dataframe(df, hide_index=True, width="stretch")
