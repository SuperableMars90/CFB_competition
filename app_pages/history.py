"""
Lineup history page — read-only "selections" viewer for any week the
player has submitted a lineup, not just the one currently being edited
on the Submit Lineup page. Reuses the same slot manifest (lib.game_rules)
so grouping/order matches the picks page exactly.

Deliberately read-only: no widgets that could be mistaken for editing a
past lineup (that's what Submit Lineup is for, including its own
overwrite-warning flow). This page only ever calls get_lineup, never
save_lineup.
"""

import streamlit as st

from lib.auth import get_logged_in_player
from lib.db import (
    get_conference_slot_tiers,
    get_week_date_ranges,
    format_week_label,
    get_current_week,
    get_lineup,
    get_teams_by_ids,
    get_games_by_ids,
)
from lib.game_rules import SlotType, build_slot_manifest
from lib.config import CURRENT_SEASON_ID

st.title("Lineup History")

player = get_logged_in_player()
player_id = player["id"]
season_id = CURRENT_SEASON_ID

_current_week = get_current_week(season_id)
if _current_week is not None:
    st.caption(f"Current week: {_current_week}")

date_ranges = get_week_date_ranges(season_id)
week_options = list(range(1, 21))
default_index = week_options.index(_current_week) if _current_week in week_options else 0

week = st.selectbox(
    "Week", options=week_options,
    format_func=lambda w: format_week_label(w, date_ranges),
    index=default_index,
)
week = int(week)

stored = get_lineup(player_id, season_id, week)

if stored is None:
    st.info(f"No lineup submitted for week {week}.")
    st.stop()

when = stored.submitted_at.strftime("%b %d, %Y %I:%M %p") if stored.submitted_at else "unknown time"
status = "locked" if stored.is_locked else "not yet locked"
st.caption(f"Submitted {when} — {status}.")

slot_tiers = get_conference_slot_tiers(season_id)
manifest, p4_confs, g6_confs = build_slot_manifest(slot_tiers)

team_ids = {v[0] for v in stored.slot_assignments.values() if v is not None}
game_ids = {v[1] for v in stored.slot_assignments.values() if v is not None and v[1] is not None}
teams_by_id = get_teams_by_ids(team_ids)
games_by_id = get_games_by_ids(game_ids)


def render_pick(slot_type: SlotType, identifier: str, label: str) -> None:
    assignment = stored.slot_assignments.get((slot_type.value, identifier))
    if assignment is None:
        st.write(f"**{label}:** — Passed —")
        return

    team_id, game_id = assignment
    team = teams_by_id.get(team_id)
    team_name = team["name"] if team else f"Team #{team_id} (no longer on record)"

    game = games_by_id.get(game_id) if game_id is not None else None
    if game is None:
        st.write(f"**{label}:** {team_name}")
        return

    is_home = game["home_team_id"] == team_id
    opponent_id = game["away_team_id"] if is_home else game["home_team_id"]
    opponent = teams_by_id.get(opponent_id)
    # Opponent may not be in teams_by_id (only picked teams were fetched) —
    # fall back to a second lookup rather than widen the batch query for
    # what's normally a single extra id per slot.
    if opponent is None:
        opponent = get_teams_by_ids({opponent_id}).get(opponent_id)
    opponent_name = opponent["name"] if opponent else "TBD"

    if game["is_neutral"]:
        matchup = f"{team_name} vs {opponent_name} (N)"
    elif is_home:
        matchup = f"{team_name} vs {opponent_name}"
    else:
        matchup = f"{team_name} @ {opponent_name}"

    banked = game["is_week0"] or game["week"] != week
    if banked:
        matchup += " (week 0)"

    home_score, away_score = game["home_score"], game["away_score"]
    if home_score is not None and away_score is not None:
        team_score = home_score if is_home else away_score
        opp_score = away_score if is_home else home_score
        matchup += f" — final {team_score}-{opp_score}"

    st.write(f"**{label}:** {matchup}")


st.subheader("P4")
st.markdown("**Conferences**")
for abbr in p4_confs:
    render_pick(SlotType.CONFERENCE, abbr, abbr)

st.markdown("**Flex**")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.P4_FLEX:
        render_pick(slot_type, identifier, label)

st.subheader("G6")
st.markdown("**Conferences**")
for abbr in g6_confs:
    render_pick(SlotType.CONFERENCE, abbr, abbr)

st.markdown("**Flex**")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.G6_FLEX:
        render_pick(slot_type, identifier, label)

st.subheader("Wildcard")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.WILDCARD:
        render_pick(slot_type, identifier, label)
