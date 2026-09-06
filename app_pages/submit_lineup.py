"""
Weekly lineup submission page.

Flow: auth login -> week picker (dev seam) ->
fill 16 slots (each a roster team eligible for that slot, or an explicit Pass)
-> Review (runs validate_lineup) -> Confirm & submit (overwrite warning) ->
save_lineup writes 16 rows + header in one transaction.

Eligibility per dropdown is computed by running each roster team through
game_rules.validate_pick for that slot, so the rules live in exactly one
place. Duplicate-team across slots is intentionally NOT filtered live (it
makes dropdowns jump on rerun); it's caught by validate_lineup at review.
"""

from datetime import timezone
from zoneinfo import ZoneInfo

import streamlit as st

from lib.auth import get_logged_in_player
from lib.db import (
    get_roster_with_context,
    get_conference_slot_tiers,
    get_week0_elections,
    get_week0_cutoff_week,
    get_week_date_ranges,
    format_week_label,
    get_current_week,
    get_lineup,
    save_lineup,
    LineupLockedError,
)
from lib.game_rules import Pick, SlotType, build_slot_manifest, validate_pick, validate_lineup
from lib.config import CURRENT_SEASON_ID

# --- Dev seams ------------------------------------------------------------
# Replaced later: season by a current-season source, week by a
# current-week source. Marked so they're easy to find.

# Sentinels for the "not yet chosen" and "deliberately passed" dropdown states.
SELECT = "__SELECT__"
PASS = "__PASS__"
SELECT_LABEL = "— Select —"
PASS_LABEL = "— Pass (scores 0) —"

_EASTERN = ZoneInfo("America/New_York")


def _format_submitted_at(dt):
    """Render a weekly_lineups.submitted_at value in US Eastern time.

    submitted_at is written by MySQL NOW(); the Aiven server runs on UTC, so
    the value comes back as a naive-UTC datetime. Present it in Eastern with
    an explicit EST/EDT label so players aren't left guessing the zone.
    """
    if not dt:
        return "earlier"
    eastern = dt.replace(tzinfo=timezone.utc).astimezone(_EASTERN)
    return eastern.strftime("%b %d, %Y %I:%M %p %Z")

st.title("Submit Weekly Lineup")

# T25: informational only — separate from the week *selector* below, which
# is what the player is actually editing/viewing and may deliberately
# differ from "today's" week.
_current_week = get_current_week(CURRENT_SEASON_ID)
if _current_week is not None:
    st.caption(f"Current week: {_current_week}")


# --- Auth + week picker ---------------------------------------------------
player = get_logged_in_player()
player_id = player["id"]

season_id = CURRENT_SEASON_ID

# T24: label each week with its date range ("1 - 9/3-9/7") when known, from
# v_week_date_ranges (MIN/MAX game_date per week). Falls back to the bare
# week number if no games are loaded for that week yet. The selectable
# range itself is unchanged (still 1-20 — T24 only enhances the label, it
# doesn't narrow which weeks can be picked based on which happen to have
# game data loaded so far).
_date_ranges = get_week_date_ranges(season_id)  # DEV SEAM (season_id)

week = st.selectbox(
    "Week", options=list(range(1, 21)),
    format_func=lambda w: format_week_label(w, _date_ranges),
)  # DEV SEAM
week = int(week)


# --- Load data for this player/week ---------------------------------------
roster = get_roster_with_context(player_id, season_id, week)
if not roster:
    st.warning("This player has no active roster for the current season.")
    st.stop()

slot_tiers = get_conference_slot_tiers(season_id)
roster_by_id = {t.team_id: t for t in roster}
roster_ids = set(roster_by_id)

elections = get_week0_elections(player_id, season_id)
cutoff_week = get_week0_cutoff_week(season_id)
week0_game_ids = {e["game_id"] for e in elections}
week0_by_team: dict[int, list[int]] = {}
for e in elections:
    week0_by_team.setdefault(e["team_id"], []).append(e["game_id"])

if elections and cutoff_week:
    if week >= cutoff_week:
        st.error(
            f"Week 0 deadline: you must play your banked game(s) this week "
            f"or they will be automatically assigned by the commissioner."
        )
    elif week == cutoff_week - 1:
        st.warning(
            f"Reminder: banked week 0 game(s) must be played by week {cutoff_week}."
        )

stored = get_lineup(player_id, season_id, week)

if stored is not None and stored.is_locked:
    st.error(f"Your week {week} lineup is locked and can no longer be edited.")
    st.stop()

if stored is not None:
    when = _format_submitted_at(stored.submitted_at)
    st.info(
        f"You already have a lineup for week {week} (submitted {when}). "
        "Editing here and submitting will overwrite it."
    )


manifest, p4_confs, g6_confs = build_slot_manifest(slot_tiers)


def eligible_options(slot_type, identifier):
    """
    Return (team_id, game_id) tuples eligible for this slot.

    For each eligible roster team, the current-week game is included as
    (team_id, team.game_id). If the player also has an undeployed week 0
    election for that team, each banked game_id is appended immediately after.
    """
    out = []
    for team in roster:
        pick = Pick(slot_type, identifier, team)
        if validate_pick(pick, roster_ids, slot_tiers).is_valid:
            out.append((team.team_id, team.game_id))
            for w0_gid in week0_by_team.get(team.team_id, []):
                out.append((team.team_id, w0_gid))
    return out


def widget_key(slot_type, identifier):
    return f"slot__{slot_type.value}__{identifier}"


# --- Seed widget state from the stored lineup (once per player/week) -------
# Seeding must happen before the widgets are created. We only seed a stored
# team if it's still eligible/owned for that slot; otherwise fall back to
# Select so the widget never holds a value that isn't in its options.
load_key = (player_id, week)
if st.session_state.get("_load_key") != load_key:
    for slot_type, identifier, _label in manifest:
        key = widget_key(slot_type, identifier)
        seeded = SELECT
        if stored is not None:
            assignment = stored.slot_assignments.get((slot_type.value, identifier), "absent")
            if assignment is None:
                seeded = PASS
            elif assignment != "absent":
                # assignment is (team_id, game_id); restore only if still eligible
                eligible = eligible_options(slot_type, identifier)
                if assignment in eligible:
                    seeded = assignment
        st.session_state[key] = seeded
    st.session_state["_load_key"] = load_key
    st.session_state.pop("pending_picks", None)


def _invalidate_review():
    """Any slot change invalidates a prior review/confirm step."""
    st.session_state.pop("pending_picks", None)


# --- Render the 16 slot pickers -------------------------------------------
def render_slot(slot_type, identifier, label):
    options = [SELECT, PASS] + eligible_options(slot_type, identifier)

    def fmt(value):
        if value == SELECT:
            return SELECT_LABEL
        if value == PASS:
            return PASS_LABEL
        team_id, game_id = value
        team = roster_by_id[team_id]
        if game_id in week0_game_ids:
            return f"{team.name} (week 0)"
        return team.display_label

    return st.selectbox(
        label,
        options=options,
        format_func=fmt,
        key=widget_key(slot_type, identifier),
        on_change=_invalidate_review,
    )


selections = {}  # (slot_type, identifier) -> SELECT | PASS | (team_id, game_id)

st.subheader("P4")
st.markdown("**Conferences**")
for abbr in p4_confs:
    selections[(SlotType.CONFERENCE, abbr)] = render_slot(SlotType.CONFERENCE, abbr, abbr)

st.markdown("**Flex**")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.P4_FLEX:
        selections[(slot_type, identifier)] = render_slot(slot_type, identifier, label)

st.subheader("G6")
st.markdown("**Conferences**")
for abbr in g6_confs:
    selections[(SlotType.CONFERENCE, abbr)] = render_slot(SlotType.CONFERENCE, abbr, abbr)

st.markdown("**Flex**")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.G6_FLEX:
        selections[(slot_type, identifier)] = render_slot(slot_type, identifier, label)

st.subheader("Wildcard")
for slot_type, identifier, label in manifest:
    if slot_type == SlotType.WILDCARD:
        selections[(slot_type, identifier)] = render_slot(slot_type, identifier, label)


# --- Review ---------------------------------------------------------------
st.divider()

unresolved = [
    label for (slot_type, identifier, label) in manifest
    if selections[(slot_type, identifier)] == SELECT
]

if st.button("Review lineup", type="primary"):
    st.session_state.pop("pending_picks", None)
    if unresolved:
        st.warning(
            f"{len(unresolved)} slot(s) still unset — choose a team or Pass: "
            + ", ".join(unresolved)
        )
    else:
        picks = []
        for slot_type, identifier, _label in manifest:
            val = selections[(slot_type, identifier)]
            if val == PASS:
                picks.append(Pick(slot_type, identifier, None))
            else:
                team_id, game_id = val
                picks.append(Pick(slot_type, identifier, roster_by_id[team_id], game_id=game_id))

        result = validate_lineup(picks, roster_ids, slot_tiers)
        if not result.is_valid:
            for err in result.errors:
                st.error(err)
        else:
            st.success("Lineup is valid.")
            for warn in result.warnings:
                st.warning(warn)
            st.session_state["pending_picks"] = picks


# --- Confirm & submit -----------------------------------------------------
if st.session_state.get("pending_picks"):
    st.divider()
    if stored is not None:
        st.warning(f"Submitting will OVERWRITE your existing week {week} lineup.")
    if st.button("Confirm & submit", type="primary"):
        try:
            save_lineup(player_id, season_id, week, st.session_state["pending_picks"])
        except LineupLockedError:
            st.error("That lineup is now locked and can't be overwritten.")
        except Exception as e:  # surface unexpected DB errors rather than swallowing
            st.error(f"Save failed ({type(e).__name__}): {e}")
        else:
            st.session_state.pop("pending_picks", None)
            st.session_state["_load_key"] = None  # force reseed from saved lineup
            st.success(f"Week {week} lineup submitted.")
            st.rerun()