"""
Unit tests for lib.draft -- pure Python, no DB, no Streamlit.
"""

from collections import Counter

import pytest

from lib.draft import (
    DraftSlot,
    round_order,
    slot_for_pick,
    total_picks,
    generate_full_draft_order,
    is_double_pick,
    ordinal,
    SortBy,
    TierFilter,
    filter_and_sort_teams,
)

BASE_ORDER = [10, 20, 30, 40]  # 4 players, arbitrary player_ids -- the 4-player-pod format

BASE_ORDER_3 = [100, 200, 300]  # 3 players, arbitrary player_ids -- the 3-player-pod format


class TestRoundOrder:
    def test_odd_round_unchanged(self):
        assert round_order(BASE_ORDER, 1) == [10, 20, 30, 40]
        assert round_order(BASE_ORDER, 3) == [10, 20, 30, 40]

    def test_even_round_reversed(self):
        assert round_order(BASE_ORDER, 2) == [40, 30, 20, 10]
        assert round_order(BASE_ORDER, 4) == [40, 30, 20, 10]

    def test_3_players_odd_round_unchanged(self):
        assert round_order(BASE_ORDER_3, 1) == [100, 200, 300]
        assert round_order(BASE_ORDER_3, 3) == [100, 200, 300]

    def test_3_players_even_round_reversed(self):
        assert round_order(BASE_ORDER_3, 2) == [300, 200, 100]


class TestSlotForPick:
    def test_pick_1_is_round1_pick_in_round1(self):
        slot = slot_for_pick(BASE_ORDER, 1)
        assert slot == DraftSlot(pick_number=1, round_number=1, pick_in_round=1, player_id=10)

    def test_pick_4_is_round1_pick_in_round4(self):
        slot = slot_for_pick(BASE_ORDER, 4)
        assert slot == DraftSlot(pick_number=4, round_number=1, pick_in_round=4, player_id=40)

    def test_pick_5_is_round2_pick_in_round1_same_player_as_pick_4(self):
        slot = slot_for_pick(BASE_ORDER, 5)
        assert slot.round_number == 2
        assert slot.pick_in_round == 1
        assert slot.player_id == 40  # boundary carry-over from pick 4

    def test_pick_8_is_round2_pick_in_round4(self):
        slot = slot_for_pick(BASE_ORDER, 8)
        assert slot.round_number == 2
        assert slot.pick_in_round == 4
        assert slot.player_id == 10

    def test_3_players_pick_1_is_round1_pick_in_round1(self):
        slot = slot_for_pick(BASE_ORDER_3, 1)
        assert slot == DraftSlot(pick_number=1, round_number=1, pick_in_round=1, player_id=100)

    def test_3_players_pick_3_is_round1_pick_in_round3(self):
        slot = slot_for_pick(BASE_ORDER_3, 3)
        assert slot == DraftSlot(pick_number=3, round_number=1, pick_in_round=3, player_id=300)

    def test_3_players_pick_4_is_round2_pick_in_round1_same_player_as_pick_3(self):
        slot = slot_for_pick(BASE_ORDER_3, 4)
        assert slot.round_number == 2
        assert slot.pick_in_round == 1
        assert slot.player_id == 300  # boundary carry-over from pick 3

    def test_3_players_pick_6_is_round2_pick_in_round3(self):
        slot = slot_for_pick(BASE_ORDER_3, 6)
        assert slot.round_number == 2
        assert slot.pick_in_round == 3
        assert slot.player_id == 100


class TestGenerateFullDraftOrder:
    def test_n4_picks_per_player_25(self):
        slots = generate_full_draft_order(BASE_ORDER, 25)

        assert len(slots) == 100
        assert [s.pick_number for s in slots] == list(range(1, 101))
        assert Counter(s.player_id for s in slots) == {10: 25, 20: 25, 30: 25, 40: 25}

        # Matches slot_for_pick pick-for-pick.
        for pick_number in (1, 4, 5, 8, 100):
            assert slots[pick_number - 1] == slot_for_pick(BASE_ORDER, pick_number)

    def test_n3_picks_per_player_30(self):
        # The confirmed 3-player-pod format's real draft length
        # (seasons.draft_picks_per_player=30) -- not just an arbitrary
        # smaller number, the actual shape this transition produces.
        slots = generate_full_draft_order(BASE_ORDER_3, 30)

        assert len(slots) == 90
        assert [s.pick_number for s in slots] == list(range(1, 91))
        assert Counter(s.player_id for s in slots) == {100: 30, 200: 30, 300: 30}

        # Matches slot_for_pick pick-for-pick.
        for pick_number in (1, 3, 4, 6, 90):
            assert slots[pick_number - 1] == slot_for_pick(BASE_ORDER_3, pick_number)


class TestIsDoublePick:
    def test_round_boundary_pair_is_double(self):
        pick4 = slot_for_pick(BASE_ORDER, 4)
        pick5 = slot_for_pick(BASE_ORDER, 5)
        assert is_double_pick(pick4, pick5) is True

    def test_non_boundary_pair_is_not_double(self):
        pick1 = slot_for_pick(BASE_ORDER, 1)
        pick2 = slot_for_pick(BASE_ORDER, 2)
        assert is_double_pick(pick1, pick2) is False

    def test_last_pick_of_draft_has_no_second_no_crash(self):
        pick100 = slot_for_pick(BASE_ORDER, 100)
        assert is_double_pick(pick100, None) is False

    def test_3_players_round_boundary_pair_is_double(self):
        pick3 = slot_for_pick(BASE_ORDER_3, 3)
        pick4 = slot_for_pick(BASE_ORDER_3, 4)
        assert is_double_pick(pick3, pick4) is True


class TestOrdinal:
    @pytest.mark.parametrize("n,expected", [
        (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
        (11, "11th"), (12, "12th"), (13, "13th"),
        (21, "21st"), (22, "22nd"), (23, "23rd"),
        (100, "100th"), (101, "101st"), (111, "111th"),
    ])
    def test_ordinal(self, n, expected):
        assert ordinal(n) == expected


def team(team_id, name, conference_abbreviation, tier):
    return {'team_id': team_id, 'name': name, 'conference_abbreviation': conference_abbreviation, 'tier': tier}


TEAMS = [
    team(1, 'Zeta State', 'SEC', 'P4'),
    team(2, 'Alpha U', 'SEC', 'P4'),
    team(3, 'Beta College', 'MAC', 'G6'),
    team(4, 'Gamma Tech', 'MWC', 'G6'),
]


class TestFilterAndSortTeams:
    def test_default_sorts_by_name(self):
        result = filter_and_sort_teams(TEAMS)
        assert [t['name'] for t in result] == ['Alpha U', 'Beta College', 'Gamma Tech', 'Zeta State']

    def test_tier_p4_filters_to_p4_only(self):
        result = filter_and_sort_teams(TEAMS, tier=TierFilter.P4)
        assert {t['team_id'] for t in result} == {1, 2}

    def test_tier_g6_filters_to_g6_only(self):
        result = filter_and_sort_teams(TEAMS, tier=TierFilter.G6)
        assert {t['team_id'] for t in result} == {3, 4}

    def test_specific_conference_filter(self):
        result = filter_and_sort_teams(TEAMS, tier=TierFilter.CONFERENCE, conference='MAC')
        assert [t['team_id'] for t in result] == [3]

    def test_conference_filter_without_conference_raises(self):
        with pytest.raises(ValueError):
            filter_and_sort_teams(TEAMS, tier=TierFilter.CONFERENCE)

    def test_sort_by_conference(self):
        result = filter_and_sort_teams(TEAMS, sort_by=SortBy.CONFERENCE)
        assert [t['conference_abbreviation'] for t in result] == ['MAC', 'MWC', 'SEC', 'SEC']

    def test_sort_by_tier(self):
        result = filter_and_sort_teams(TEAMS, sort_by=SortBy.TIER)
        assert [t['tier'] for t in result] == ['G6', 'G6', 'P4', 'P4']
