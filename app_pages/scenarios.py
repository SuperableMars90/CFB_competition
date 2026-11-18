"""
Scenarios page -- "what if" tool for the rest of the PVP regular season.
Pick a winner for any still-unresolved matchup (including the current
week's, until it's finalized) and see the resulting standings, each
player's movement vs. the actual standings, and -- once every game is
picked and no ties remain -- projected playoff seeds.

Read-only: picks live only in this session's widget state; nothing is
written to the DB. Ranking logic lives in lib/scenarios.py.
"""

import pandas as pd
import streamlit as st

from lib.config import CURRENT_SEASON_ID
from lib.db import get_current_week, get_players, get_pod_membership_map, get_pods
from lib.metagame import get_scoring_context
from lib.scenarios import (
    apply_picks,
    get_pvp_regular_season_end_week,
    get_regular_season_state,
    projected_playoff_seeds,
    rank_standings,
)
from lib.seeding import get_manual_tiebreaks

# Auth is already gated by streamlit_app.py -- this page doesn't need the
# player dict, so no lib.auth call at all (see lib/auth.py's docstring).

st.title("Scenarios")
st.caption(
    "Pick winners for the remaining PVP matchups to see how the regular-season "
    "standings would shake out. Nothing here is saved."
)

season_id = CURRENT_SEASON_ID


@st.cache_data(ttl=300)
def _load_state(season_id: int):
    """(records, remaining, manual_tiebreaks), or None if the season has
    no PVP regular-season end week configured. Cached so each pick's
    rerun doesn't re-query the DB."""
    end_week = get_pvp_regular_season_end_week(season_id)
    if end_week is None:
        return None
    ctx_id = get_scoring_context(season_id)["id"]
    records, remaining = get_regular_season_state(season_id, ctx_id, end_week)
    return records, remaining, get_manual_tiebreaks(season_id, ctx_id)


state = _load_state(season_id)
if state is None:
    st.info("The PVP regular season isn't set up for this season yet.")
    st.stop()
records, remaining, manual_tiebreaks = state

names = {p["id"]: p["name"] for p in get_players()}
pod_of_player = get_pod_membership_map(season_id)
pod_names = {p["id"]: p["name"] for p in get_pods(season_id)}
current_week = get_current_week(season_id)


def _pick_key(pairing_id: int) -> str:
    return f"scenario_pick_{pairing_id}"


def _clear_picks() -> None:
    for m in remaining:
        st.session_state[_pick_key(m.pairing_id)] = None


def _rank_label(row: dict) -> str:
    return f"T-{row['rank']}" if row["tied"] else str(row["rank"])


def _record_label(row: dict) -> str:
    rec = f"{row['wins']}-{row['losses']}"
    return f"{rec}-{row['ties']}" if row["ties"] else rec


def _movement(before: int, after: int) -> str:
    if after < before:
        return f"▲ {before - after}"
    if after > before:
        return f"▼ {after - before}"
    return "—"


current = rank_standings(records, manual_tiebreaks)
current_rank = {r["player_id"]: r["rank"] for r in current}

picks_col, standings_col = st.columns([1, 1], gap="large")

with picks_col:
    st.subheader("Remaining matchups")
    if not remaining:
        st.info("The PVP regular season is complete — no matchups left to pick.")
    else:
        st.button("Clear all picks", on_click=_clear_picks)
        for week in sorted({m.week for m in remaining}):
            label = f"Week {week}"
            if week == current_week:
                label += " (in progress)"
            st.markdown(f"**{label}**")
            for m in (m for m in remaining if m.week == week):
                st.radio(
                    f"{names.get(m.player_a_id, m.player_a_id)} vs "
                    f"{names.get(m.player_b_id, m.player_b_id)}",
                    options=[m.player_a_id, m.player_b_id],
                    format_func=lambda pid: names.get(pid, str(pid)),
                    index=None,
                    horizontal=True,
                    key=_pick_key(m.pairing_id),
                )

picks = {
    m.pairing_id: st.session_state.get(_pick_key(m.pairing_id))
    for m in remaining
}
picks = {pid: winner for pid, winner in picks.items() if winner is not None}
scenario = rank_standings(apply_picks(records, remaining, picks), manual_tiebreaks)

seeds = None
all_picked = len(picks) == len(remaining)
if all_picked and not any(r["tied"] for r in scenario):
    try:
        seeds = projected_playoff_seeds(scenario, pod_of_player)
    except ValueError:
        seeds = None  # league shape with no confirmed bracket -- just omit the column

with standings_col:
    st.subheader("Scenario standings")
    st.caption(f"{len(picks)} of {len(remaining)} remaining matchups picked.")

    table = pd.DataFrame([
        {
            "Rank": _rank_label(r),
            "Player": names.get(r["player_id"], str(r["player_id"])),
            "Pod": pod_names.get(pod_of_player.get(r["player_id"]), ""),
            "Record": _record_label(r),
            "Net margin": r["net_margin"],
            "vs. now": _movement(current_rank[r["player_id"]], r["rank"]),
            **({"Playoff seed": seeds.get(r["player_id"])} if seeds else {}),
        }
        for r in scenario
    ])
    st.dataframe(table, hide_index=True, width="stretch")

    st.caption(
        "Ties are broken the same way as final playoff seeding: record vs. the tied "
        "group, then net margin vs. the tied group, then overall net margin. Picked "
        "games have no score, so they count as a 0-point margin — net margin only "
        "reflects games actually played."
    )
    if remaining and not all_picked:
        st.caption("Pick every remaining matchup to see projected playoff seeds.")
    elif all_picked and seeds is None and any(r["tied"] for r in scenario):
        st.caption(
            "Projected playoff seeds aren't shown while players are still tied — "
            "the commissioner would break that tie manually."
        )

    with st.expander("Current standings (actual results only)"):
        st.dataframe(
            pd.DataFrame([
                {
                    "Rank": _rank_label(r),
                    "Player": names.get(r["player_id"], str(r["player_id"])),
                    "Pod": pod_names.get(pod_of_player.get(r["player_id"]), ""),
                    "Record": _record_label(r),
                    "Net margin": r["net_margin"],
                }
                for r in current
            ]),
            hide_index=True,
            width="stretch",
        )
