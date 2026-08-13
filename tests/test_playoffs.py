"""
Unit tests for lib.playoffs -- pure Python, no DB, no Streamlit.
"""

from collections import defaultdict
from fractions import Fraction

import pytest

from lib.bracket_engine import GameResult
from lib.seeding import MatchupRecord
from lib.playoffs import (
    FOUR_TEAM_BRACKET,
    SIX_TEAM_BRACKET,
    EIGHT_TEAM_BRACKET,
    assign_seeds_4_team,
    assign_seeds_6_team,
    assign_seeds_8_team,
    compute_4_team_placements,
    pick_record_score,
    resolve_playoff_game,
    find_exhibition_pairing,
    bracket_format_for_league_shape,
    compute_final_placements,
    UnresolvedPlayoffTieError,
)


def build_records(meetings):
    """
    meetings: {(a, b): [(week, winner), ...]}, winner is 'a', 'b', or
    None (tie). Builds symmetric {player_id: [MatchupRecord, ...]}.
    """
    records = defaultdict(list)
    for (a, b), games in meetings.items():
        for week, winner in games:
            if winner == 'a':
                ra, rb = 'W', 'L'
            elif winner == 'b':
                ra, rb = 'L', 'W'
            else:
                ra, rb = 'T', 'T'
            records[a].append(MatchupRecord(week=week, opponent_id=b, result=ra, margin=0))
            records[b].append(MatchupRecord(week=week, opponent_id=a, result=rb, margin=0))
    return dict(records)


def seeding_row(seed, player_id, pod=None):
    """Minimal lib.seeding.compute_seeding()-shaped row for these tests."""
    return {'seed': seed, 'player_id': player_id, 'wins': 0, 'losses': 0,
            'ties': 0, 'win_pct': 0.0, 'net_margin': 0}


def pick(is_pass=False, margin=None):
    return {'is_pass': is_pass, 'margin': margin}


class TestAssignSeeds4Team:
    def test_identity_pass_through(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 5)]
        assert assign_seeds_4_team(seeding) == {1: 101, 2: 102, 3: 103, 4: 104}

    def test_wrong_length_raises(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 4)]
        with pytest.raises(ValueError):
            assign_seeds_4_team(seeding)


class TestAssignSeeds8Team:
    def test_alternating_pods_matches_overall_order(self):
        # overall order alternates pods -- seed 2 already happens to be
        # in the other pod, so no reshuffling is visible.
        seeding = [
            seeding_row(1, 1), seeding_row(2, 2), seeding_row(3, 3), seeding_row(4, 4),
            seeding_row(5, 5), seeding_row(6, 6), seeding_row(7, 7), seeding_row(8, 8),
        ]
        pod_of_player = {1: 'A', 3: 'A', 5: 'A', 7: 'A', 2: 'B', 4: 'B', 6: 'B', 8: 'B'}
        result = assign_seeds_8_team(seeding, pod_of_player)
        assert result == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8}

    def test_overall_number_2_bumped_to_seed_3_when_same_pod_as_1(self):
        # Zach's own scenario: P1 and P2 share a pod; P3 (other pod) jumps
        # to seed 2, P2 gets bumped down to seed 3.
        seeding = [
            seeding_row(1, 1), seeding_row(2, 2), seeding_row(3, 3), seeding_row(4, 4),
            seeding_row(5, 5), seeding_row(6, 6), seeding_row(7, 7), seeding_row(8, 8),
        ]
        pod_of_player = {1: 'A', 2: 'A', 4: 'A', 7: 'A', 3: 'B', 5: 'B', 6: 'B', 8: 'B'}
        result = assign_seeds_8_team(seeding, pod_of_player)
        assert result[1] == 1
        assert result[2] == 3  # other pod's best, not overall #2
        assert result[3] == 2  # overall #2, bumped down for sharing seed 1's pod
        assert result[4] == 4
        assert result[5] == 5
        assert result[6] == 6
        assert result[7] == 7
        assert result[8] == 8

    def test_wrong_length_raises(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 7)]
        pod_of_player = {100 + i: 'A' if i % 2 else 'B' for i in range(1, 7)}
        with pytest.raises(ValueError):
            assign_seeds_8_team(seeding, pod_of_player)

    def test_all_one_pod_raises(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 9)]
        pod_of_player = {100 + i: 'A' for i in range(1, 9)}
        with pytest.raises(ValueError):
            assign_seeds_8_team(seeding, pod_of_player)


class TestAssignSeeds6Team:
    def test_alternating_pods_matches_overall_order(self):
        seeding = [seeding_row(i, i) for i in range(1, 7)]
        pod_of_player = {1: 'A', 3: 'A', 5: 'A', 2: 'B', 4: 'B', 6: 'B'}
        result = assign_seeds_6_team(seeding, pod_of_player)
        assert result == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}

    def test_overall_number_2_bumped_to_seed_3_when_same_pod_as_1(self):
        # Same shape as the 8-team scenario, scaled to 3-player pods:
        # P1/P2 share a pod; P3 (other pod) jumps to seed 2.
        seeding = [seeding_row(i, i) for i in range(1, 7)]
        pod_of_player = {1: 'A', 2: 'A', 4: 'A', 3: 'B', 5: 'B', 6: 'B'}
        result = assign_seeds_6_team(seeding, pod_of_player)
        assert result[1] == 1
        assert result[2] == 3  # other pod's best, not overall #2
        assert result[3] == 2  # overall #2, bumped down for sharing seed 1's pod
        assert result[4] == 4
        assert result[5] == 5
        assert result[6] == 6

    def test_wrong_length_raises(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 6)]
        pod_of_player = {100 + i: 'A' if i % 2 else 'B' for i in range(1, 6)}
        with pytest.raises(ValueError):
            assign_seeds_6_team(seeding, pod_of_player)

    def test_all_one_pod_raises(self):
        seeding = [seeding_row(i, 100 + i) for i in range(1, 7)]
        pod_of_player = {100 + i: 'A' for i in range(1, 7)}
        with pytest.raises(ValueError):
            assign_seeds_6_team(seeding, pod_of_player)


class TestCompute4TeamPlacements:
    def test_full_walk_including_non_qualifier(self):
        seed_to_player = {1: 11, 2: 12, 3: 13, 4: 14}
        results = {
            1: GameResult(winner_id=12, loser_id=13),  # seed2 beats seed3
            2: GameResult(winner_id=11, loser_id=12),  # seed1 beats seed2
        }
        placements = compute_4_team_placements(seed_to_player, results)
        assert placements == {1: 11, 2: 12, 3: 13, 4: 14}


class TestPickRecordScore:
    def test_all_wins(self):
        picks = [pick(margin=10), pick(margin=1), pick(margin=7)]
        assert pick_record_score(picks) == Fraction(3)

    def test_mixed_win_pass_loss(self):
        picks = [pick(margin=10), pick(margin=1), pick(is_pass=True), pick(margin=-5)]
        # 2 wins + 1 pass (0.5) + 1 loss (0) = 2.5
        assert pick_record_score(picks) == Fraction(5, 2)

    def test_tied_underlying_game_counts_as_half_point(self):
        picks = [pick(margin=0)]
        assert pick_record_score(picks) == Fraction(1, 2)

    def test_in_flight_pick_raises(self):
        picks = [pick(margin=10), pick(margin=None)]
        with pytest.raises(ValueError):
            pick_record_score(picks)


class TestResolvePlayoffGame:
    def test_unequal_totals_decides_by_total_points(self):
        player_totals = {
            1: {'total': 100, 'slots': []},
            2: {'total': 80, 'slots': []},
        }
        result = resolve_playoff_game(player_totals, 1, 2)
        assert result.winner_id == 1
        assert result.loser_id == 2
        assert result.decided_by == 'total_points'

    def test_equal_totals_decides_by_pick_record(self):
        player_totals = {
            1: {'total': 100, 'slots': [pick(margin=10), pick(margin=5)]},        # 2 wins
            2: {'total': 100, 'slots': [pick(margin=10), pick(is_pass=True)]},    # 1 win + 1 pass = 1.5
        }
        result = resolve_playoff_game(player_totals, 1, 2)
        assert result.winner_id == 1
        assert result.loser_id == 2
        assert result.decided_by == 'pick_record'

    def test_equal_totals_and_equal_pick_record_raises(self):
        player_totals = {
            1: {'total': 100, 'slots': [pick(margin=10)]},
            2: {'total': 100, 'slots': [pick(margin=5)]},
        }
        with pytest.raises(UnresolvedPlayoffTieError) as exc_info:
            resolve_playoff_game(player_totals, 1, 2)
        assert exc_info.value.player_a_id == 1
        assert exc_info.value.player_b_id == 2


class TestFindExhibitionPairing:
    def test_trivial_two_player_case(self):
        # 4-team format: only one possible pair, no records needed.
        result = find_exhibition_pairing([10, 20], {}, {}, current_week=13)
        assert result == [(10, 20)]

    def test_wrong_count_raises(self):
        with pytest.raises(ValueError):
            find_exhibition_pairing([1, 2, 3], {}, {}, current_week=13)
        with pytest.raises(ValueError):
            find_exhibition_pairing([1, 2, 3, 4, 5], {}, {}, current_week=13)

    def test_tier1_total_meetings_decides_alone(self):
        records = build_records({
            (1, 2): [(5, 'a')],
            (3, 4): [(6, 'a')],
            (1, 3): [(1, 'a'), (2, 'b'), (3, 'a')],
            (2, 4): [(1, 'a'), (2, 'b'), (3, 'a')],
            (1, 4): [(4, 'a'), (5, 'b')],
            (2, 3): [(4, 'a'), (5, 'b')],
        })
        pods = {1: 'A', 2: 'A', 3: 'A', 4: 'A'}  # irrelevant to this test
        result = find_exhibition_pairing([1, 2, 3, 4], records, pods, current_week=13)
        assert result == [(1, 2), (3, 4)]

    def test_tier2_weeks_since_breaks_tier1_tie(self):
        # (7,4)+(8,6) and (7,6)+(8,4) both total 3 meetings -- tier 2
        # (combined weeks-since-last-meeting) must break the tie.
        records = build_records({
            (7, 8): [(2, 'a'), (6, 'b'), (11, 'a')],
            (4, 6): [(4, 'a'), (12, 'b')],
            (7, 4): [(5, 'a')],
            (8, 6): [(4, 'a'), (10, 'b')],
            (7, 6): [(3, 'a'), (9, 'b')],
            (8, 4): [(2, 'a')],
        })
        pods = {7: 'B', 8: 'B', 4: 'A', 6: 'B'}
        result = find_exhibition_pairing([7, 8, 4, 6], records, pods, current_week=13)
        # option (7,4)+(8,6): total=1+2=3, weeks_since=8+3=11
        # option (7,6)+(8,4): total=2+1=3, weeks_since=4+11=15 -- wins (staler)
        assert result == [(7, 6), (8, 4)]

    def test_tier3_cross_pod_breaks_tier1_and_2_tie(self):
        records = build_records({
            (1, 2): [(1, 'a'), (2, 'b'), (10, 'a')],   # total 6 combined w/ (3,4) -- eliminated tier1
            (3, 4): [(1, 'a'), (2, 'b'), (10, 'a')],
            (1, 3): [(3, 'a'), (8, 'b')],                # same-pod pair
            (2, 4): [(3, 'a'), (8, 'b')],                # same-pod pair
            (1, 4): [(3, 'a'), (8, 'b')],                # cross-pod pair
            (2, 3): [(3, 'a'), (8, 'b')],                # cross-pod pair
        })
        pods = {1: 'A', 2: 'B', 3: 'A', 4: 'B'}
        result = find_exhibition_pairing([1, 2, 3, 4], records, pods, current_week=13)
        # (1,3)+(2,4): total=4, weeks_since=10, cross_pod_count=0
        # (1,4)+(2,3): total=4, weeks_since=10, cross_pod_count=2 -- wins
        assert result == [(1, 4), (2, 3)]

    def test_tier4_variance_breaks_tier1_2_3_tie(self):
        records = build_records({
            (1, 2): [(1, 'a'), (2, 'b'), (10, 'a')],   # eliminated at tier1 (total 6)
            (3, 4): [(1, 'a'), (2, 'b'), (10, 'a')],
            (1, 3): [(3, 'a'), (9, 'b')],   # even split -> variance 0
            (2, 4): [(3, 'a'), (9, 'b')],   # even split -> variance 0
            (1, 4): [(3, 'a'), (9, 'a')],   # 1 sweeps 4 -> variance 2
            (2, 3): [(3, 'a'), (9, 'a')],   # 2 sweeps 3 -> variance 2
        })
        pods = {1: 'A', 2: 'A', 3: 'B', 4: 'B'}  # both remaining candidates are cross-pod (tied)
        result = find_exhibition_pairing([1, 2, 3, 4], records, pods, current_week=13)
        # (1,3)+(2,4): total=4, weeks_since=8, cross_pod=2, variance=0+0=0 -- wins
        # (1,4)+(2,3): total=4, weeks_since=8, cross_pod=2, variance=2+2=4
        assert result == [(1, 3), (2, 4)]

    def test_tier5_deterministic_fallback_when_everything_ties(self):
        records = build_records({
            (1, 2): [(1, 'a'), (2, 'b'), (10, 'a')],   # eliminated at tier1
            (3, 4): [(1, 'a'), (2, 'b'), (10, 'a')],
            (1, 3): [(3, 'a'), (9, 'b')],   # even split -> variance 0
            (2, 4): [(3, 'a'), (9, 'b')],   # even split -> variance 0
            (1, 4): [(3, 'a'), (9, 'b')],   # even split -> variance 0
            (2, 3): [(3, 'a'), (9, 'b')],   # even split -> variance 0
        })
        pods = {1: 'A', 2: 'A', 3: 'B', 4: 'B'}
        result = find_exhibition_pairing([1, 2, 3, 4], records, pods, current_week=13)
        # Every tier ties between (1,3)+(2,4) and (1,4)+(2,3) -- falls to the
        # deterministic canonical-sort fallback: ((1,3),(2,4)) < ((1,4),(2,3))
        assert result == [(1, 3), (2, 4)]

    def test_deterministic_across_repeated_calls(self):
        records = build_records({
            (1, 2): [(1, 'a'), (2, 'b'), (10, 'a')],
            (3, 4): [(1, 'a'), (2, 'b'), (10, 'a')],
            (1, 3): [(3, 'a'), (9, 'b')],
            (2, 4): [(3, 'a'), (9, 'b')],
            (1, 4): [(3, 'a'), (9, 'b')],
            (2, 3): [(3, 'a'), (9, 'b')],
        })
        pods = {1: 'A', 2: 'A', 3: 'B', 4: 'B'}
        results = {
            tuple(find_exhibition_pairing([1, 2, 3, 4], records, pods, current_week=13))
            for _ in range(5)
        }
        assert len(results) == 1


class TestBracketFormatForLeagueShape:
    def test_one_pod_four_players_is_four_team(self):
        assert bracket_format_for_league_shape(1, 4) == 'four_team'

    def test_two_pods_six_players_is_six_team(self):
        assert bracket_format_for_league_shape(2, 6) == 'six_team'

    def test_two_pods_eight_players_is_eight_team(self):
        assert bracket_format_for_league_shape(2, 8) == 'eight_team'

    def test_unconfirmed_combination_raises(self):
        with pytest.raises(ValueError):
            bracket_format_for_league_shape(0, 0)
        with pytest.raises(ValueError):
            bracket_format_for_league_shape(3, 9)
        with pytest.raises(ValueError):
            bracket_format_for_league_shape(1, 6)  # right player count, wrong pod count
        with pytest.raises(ValueError):
            bracket_format_for_league_shape(2, 4)  # right pod count, wrong player count


class TestComputeFinalPlacements:
    def test_four_team_none_until_fully_decided(self):
        seed_to_player = {1: 11, 2: 12, 3: 13, 4: 14}
        assert compute_final_placements('four_team', seed_to_player, {}) is None

        partial = {1: GameResult(winner_id=12, loser_id=13)}  # only G1 decided
        assert compute_final_placements('four_team', seed_to_player, partial) is None

        full = {
            1: GameResult(winner_id=12, loser_id=13),
            2: GameResult(winner_id=11, loser_id=12),
        }
        assert compute_final_placements('four_team', seed_to_player, full) == {
            1: 11, 2: 12, 3: 13, 4: 14,
        }

    def test_six_team_none_until_g6_and_g7_decided_even_though_5_and_6_known_early(self):
        seed_to_player = {i: i for i in range(1, 7)}
        early = {
            1: GameResult(3, 6),  # G1: seed3 beats seed6
            2: GameResult(4, 5),  # G2: seed4 beats seed5
            3: GameResult(5, 6),  # G3: loser(G2) beats loser(G1) -- 5th/6th decided
        }
        # Place 5/6 (from G3) already known, but the bracket as a whole
        # isn't finished -- must still return None.
        assert compute_final_placements('six_team', seed_to_player, early) is None

        full = {
            1: GameResult(3, 6),
            2: GameResult(4, 5),
            3: GameResult(5, 6),
            4: GameResult(1, 4),  # G4: seed1 beats winner(G2)
            5: GameResult(2, 3),  # G5: seed2 beats winner(G1)
            6: GameResult(3, 4),  # G6: loser(G4) beats loser(G5) -- 3rd/4th
            7: GameResult(1, 2),  # G7: winner(G4) beats winner(G5) -- championship
        }
        assert compute_final_placements('six_team', seed_to_player, full) == {
            1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6,
        }

    def test_eight_team_none_until_g7_and_g8_decided_even_though_5_through_8_known_early(self):
        seed_to_player = {i: i for i in range(1, 9)}
        early = {
            1: GameResult(4, 5), 2: GameResult(3, 6), 3: GameResult(7, 8),
            6: GameResult(5, 6),
        }
        # Places 7/8 (from G3) and 5/6 (from G6) are already knowable, but
        # the bracket as a whole isn't finished -- must still return None.
        assert compute_final_placements('eight_team', seed_to_player, early) is None

        full = {
            1: GameResult(4, 5), 2: GameResult(3, 6), 3: GameResult(7, 8),
            4: GameResult(1, 4), 5: GameResult(2, 3), 6: GameResult(5, 6),
            7: GameResult(1, 2), 8: GameResult(3, 4),
        }
        assert compute_final_placements('eight_team', seed_to_player, full) == {
            1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
        }
