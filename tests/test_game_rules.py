"""
Unit tests for lib.game_rules.

lib.game_rules is pure Python (no DB, no Streamlit — see its module
docstring), so every test here is a plain unit test; none need the
`integration` marker.

Tests are organized by what they verify:
    - TestValidatePick: per-pick rules in isolation
    - TestValidateLineup: full-lineup composition rules
    - Fixtures/helpers at the top build reusable TeamGameContext data
"""

import pytest

from lib.game_context import TeamGameContext
from lib.game_rules import Pick, SlotType, validate_lineup, validate_pick

# -----------------------------------------------------------------
# Season shape used throughout: 4 P4 + 6 G6 conference slots, matching
# the live is_lineup_slot=1 conferences (see lib/db.py get_conference_slot_tiers).
# -----------------------------------------------------------------

P4_CONFERENCES = ["ACC", "B12", "B1G", "SEC"]
G6_CONFERENCES = ["AAC", "CUSA", "MAC", "MWC", "PAC", "SBC"]
CONFERENCE_SLOT_TIERS = {
    **{c: "P4" for c in P4_CONFERENCES},
    **{c: "G6" for c in G6_CONFERENCES},
}


def make_team(team_id, name, conference_abbreviation, tier, game_id=None):
    """Build a minimal TeamGameContext for a given conference/tier."""
    return TeamGameContext(
        team_id=team_id,
        name=name,
        conference_id=team_id,
        conference_abbreviation=conference_abbreviation,
        tier=tier,
        game_id=game_id,
    )


@pytest.fixture
def roster():
    """
    A roster with one team per required conference slot, plus extra teams
    for flex/wildcard slots, an Independent-bucket team of each tier, and
    an FCS team — enough to exercise every branch in game_rules.
    """
    teams = {}
    tid = 1
    for conf in P4_CONFERENCES:
        teams[conf] = make_team(tid, f"{conf} Team", conf, "P4", game_id=100 + tid)
        tid += 1
    for conf in G6_CONFERENCES:
        teams[conf] = make_team(tid, f"{conf} Team", conf, "G6", game_id=100 + tid)
        tid += 1

    teams["p4_flex_1"] = make_team(tid, "P4 Flex A", "SEC", "P4", game_id=200); tid += 1
    teams["p4_flex_2"] = make_team(tid, "P4 Flex B", "ACC", "P4", game_id=201); tid += 1
    teams["p4_flex_3"] = make_team(tid, "P4 Flex C", "B1G", "P4", game_id=202); tid += 1
    teams["g6_flex_1"] = make_team(tid, "G6 Flex A", "MAC", "G6", game_id=203); tid += 1
    teams["g6_flex_2"] = make_team(tid, "G6 Flex B", "SBC", "G6", game_id=204); tid += 1
    teams["wildcard"] = make_team(tid, "Wildcard Team", "MWC", "G6", game_id=205); tid += 1

    # Independents: real conference abbreviation never matches a lineup
    # slot, but the team still carries a P4/G6 tier (see module docstring).
    teams["independent_p4"] = make_team(tid, "Ind P4 Team", "P4_IND", "P4", game_id=206); tid += 1
    teams["independent_g6"] = make_team(tid, "Ind G6 Team", "G6_IND", "G6", game_id=207); tid += 1

    teams["fcs"] = make_team(tid, "FCS Team", "SWAC", "FCS", game_id=208); tid += 1

    return teams


@pytest.fixture
def roster_team_ids(roster):
    return {t.team_id for t in roster.values()}


def build_full_valid_picks(roster):
    """A complete, structurally valid 16-slot lineup for `roster`."""
    picks = []
    for conf in P4_CONFERENCES + G6_CONFERENCES:
        team = roster[conf]
        picks.append(Pick(SlotType.CONFERENCE, conf, team, game_id=team.game_id))

    for i, key in enumerate(("p4_flex_1", "p4_flex_2", "p4_flex_3"), start=1):
        team = roster[key]
        picks.append(Pick(SlotType.P4_FLEX, str(i), team, game_id=team.game_id))

    for i, key in enumerate(("g6_flex_1", "g6_flex_2"), start=1):
        team = roster[key]
        picks.append(Pick(SlotType.G6_FLEX, str(i), team, game_id=team.game_id))

    team = roster["wildcard"]
    picks.append(Pick(SlotType.WILDCARD, "1", team, game_id=team.game_id))

    return picks


# ===================================================================
# TestValidatePick
# ===================================================================

class TestValidatePick:
    def test_pass_is_always_valid(self, roster_team_ids):
        pick = Pick(SlotType.CONFERENCE, "ACC", None)
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert result.is_valid

    def test_team_not_on_roster_rejected(self, roster):
        outside_team = make_team(9999, "Outside Team", "ACC", "P4")
        pick = Pick(SlotType.CONFERENCE, "ACC", outside_team)
        result = validate_pick(pick, {t.team_id for t in roster.values()}, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("not on the player's roster" in e for e in result.errors)

    def test_conference_slot_correct_conference_valid(self, roster, roster_team_ids):
        team = roster["ACC"]
        pick = Pick(SlotType.CONFERENCE, "ACC", team)
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert result.is_valid

    def test_conference_slot_wrong_conference_rejected(self, roster, roster_team_ids):
        team = roster["ACC"]
        pick = Pick(SlotType.CONFERENCE, "SEC", team)
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("cannot fill the SEC conference slot" in e for e in result.errors)

    def test_p4_flex_accepts_p4_team(self, roster, roster_team_ids):
        pick = Pick(SlotType.P4_FLEX, "1", roster["p4_flex_1"])
        assert validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_p4_flex_rejects_g6_team(self, roster, roster_team_ids):
        pick = Pick(SlotType.P4_FLEX, "1", roster["g6_flex_1"])
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("cannot fill a p4 flex slot" in e for e in result.errors)

    def test_g6_flex_accepts_g6_team(self, roster, roster_team_ids):
        pick = Pick(SlotType.G6_FLEX, "1", roster["g6_flex_1"])
        assert validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_g6_flex_rejects_p4_team(self, roster, roster_team_ids):
        pick = Pick(SlotType.G6_FLEX, "1", roster["p4_flex_1"])
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("cannot fill a g6 flex slot" in e for e in result.errors)

    def test_wildcard_accepts_p4_and_g6(self, roster, roster_team_ids):
        for key in ("p4_flex_1", "g6_flex_1"):
            pick = Pick(SlotType.WILDCARD, "1", roster[key])
            assert validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_wildcard_rejects_fcs(self, roster, roster_team_ids):
        pick = Pick(SlotType.WILDCARD, "1", roster["fcs"])
        result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("cannot fill a wildcard slot" in e for e in result.errors)

    def test_p4_independent_eligible_for_every_p4_conference_slot(self, roster, roster_team_ids):
        # Settled 2026-07-04: a P4 independent is eligible for EVERY P4
        # conference slot, not just flex/wildcard — same as if it belonged
        # to all 4 P4 conferences simultaneously.
        team = roster["independent_p4"]
        for conf in P4_CONFERENCES:
            pick = Pick(SlotType.CONFERENCE, conf, team)
            result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
            assert result.is_valid, result.errors

    def test_p4_independent_ineligible_for_g6_conference_slots(self, roster, roster_team_ids):
        team = roster["independent_p4"]
        for conf in G6_CONFERENCES:
            pick = Pick(SlotType.CONFERENCE, conf, team)
            assert not validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_g6_independent_eligible_for_every_g6_conference_slot(self, roster, roster_team_ids):
        team = roster["independent_g6"]
        for conf in G6_CONFERENCES:
            pick = Pick(SlotType.CONFERENCE, conf, team)
            result = validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS)
            assert result.is_valid, result.errors

    def test_g6_independent_ineligible_for_p4_conference_slots(self, roster, roster_team_ids):
        team = roster["independent_g6"]
        for conf in P4_CONFERENCES:
            pick = Pick(SlotType.CONFERENCE, conf, team)
            assert not validate_pick(pick, roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_independent_p4_team_eligible_for_p4_flex_and_wildcard(self, roster, roster_team_ids):
        team = roster["independent_p4"]
        assert validate_pick(Pick(SlotType.P4_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert validate_pick(Pick(SlotType.WILDCARD, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert not validate_pick(Pick(SlotType.G6_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_independent_g6_team_eligible_for_g6_flex_and_wildcard(self, roster, roster_team_ids):
        team = roster["independent_g6"]
        assert validate_pick(Pick(SlotType.G6_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert validate_pick(Pick(SlotType.WILDCARD, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert not validate_pick(Pick(SlotType.P4_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid

    def test_fcs_team_rejected_for_every_slot_type(self, roster, roster_team_ids):
        team = roster["fcs"]
        assert not validate_pick(Pick(SlotType.CONFERENCE, "SBC", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert not validate_pick(Pick(SlotType.P4_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert not validate_pick(Pick(SlotType.G6_FLEX, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid
        assert not validate_pick(Pick(SlotType.WILDCARD, "1", team), roster_team_ids, CONFERENCE_SLOT_TIERS).is_valid


# ===================================================================
# TestValidateLineup
# ===================================================================

class TestValidateLineup:
    def test_happy_path_valid_lineup(self, roster, roster_team_ids):
        picks = build_full_valid_picks(roster)
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert result.is_valid, result.errors
        assert result.warnings == []  # nothing passed, so no "passed" warning

    def test_missing_conference_slot(self, roster, roster_team_ids):
        picks = [p for p in build_full_valid_picks(roster) if p.slot_identifier != "SEC"]
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("Missing conference slot(s): SEC" in e for e in result.errors)

    def test_unexpected_conference_slot(self, roster, roster_team_ids):
        extra_team = make_team(500, "Rogue Team", "ROGUE", "P4", game_id=500)
        picks = build_full_valid_picks(roster) + [
            Pick(SlotType.CONFERENCE, "ROGUE", extra_team, game_id=500)
        ]
        result = validate_lineup(
            picks, roster_team_ids | {500}, CONFERENCE_SLOT_TIERS
        )
        assert not result.is_valid
        assert any("Unexpected conference slot(s): ROGUE" in e for e in result.errors)

    def test_wrong_flex_slot_count(self, roster, roster_team_ids):
        picks = [p for p in build_full_valid_picks(roster) if p.slot_identifier != "3"
                  or p.slot_type != SlotType.P4_FLEX]
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("Expected 3 p4 flex slot(s), got 2" in e for e in result.errors)

    def test_wrong_total_lineup_size(self, roster, roster_team_ids):
        picks = build_full_valid_picks(roster)[:-1]  # drop the wildcard pick entirely
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("Lineup must have exactly 16 slots, got 15" in e for e in result.errors)

    def test_duplicate_team_same_game_id_rejected(self, roster):
        team = roster["p4_flex_1"]
        picks = [
            Pick(SlotType.P4_FLEX, "1", team, game_id=team.game_id),
            Pick(SlotType.P4_FLEX, "2", team, game_id=team.game_id),
        ]
        result = validate_lineup(picks, {team.team_id}, CONFERENCE_SLOT_TIERS)
        assert any(
            "used in 2 slots for the same game" in e for e in result.errors
        )

    def test_same_team_different_game_id_allowed(self, roster):
        # A team may legitimately fill two slots if one targets the
        # current-week game and the other a banked week 0 game.
        team = roster["p4_flex_1"]
        picks = [
            Pick(SlotType.P4_FLEX, "1", team, game_id=team.game_id),
            Pick(SlotType.P4_FLEX, "2", team, game_id=9999),
        ]
        result = validate_lineup(picks, {team.team_id}, CONFERENCE_SLOT_TIERS)
        assert not any(
            "slots for the same game" in e for e in result.errors
        )

    def test_roster_ownership_enforced_across_full_lineup(self, roster, roster_team_ids):
        picks = build_full_valid_picks(roster)
        outside_team = make_team(9999, "Outside Team", "SEC", "P4", game_id=999)
        picks[0] = Pick(SlotType.CONFERENCE, picks[0].slot_identifier, outside_team, game_id=999)
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert not result.is_valid
        assert any("not on the player's roster" in e for e in result.errors)

    def test_passed_slot_is_structurally_valid_and_warns(self, roster, roster_team_ids):
        picks = build_full_valid_picks(roster)
        wildcard_idx = next(
            i for i, p in enumerate(picks) if p.slot_type == SlotType.WILDCARD
        )
        picks[wildcard_idx] = Pick(SlotType.WILDCARD, "1", None)
        result = validate_lineup(picks, roster_team_ids, CONFERENCE_SLOT_TIERS)
        assert result.is_valid, result.errors
        assert any("1 slot(s) passed" in w for w in result.warnings)
