"""
Unit tests for lib.scenarios' pure computation (split_pairings,
apply_picks, rank_standings, projected_playoff_seeds), plus
lib.seeding.resolve_tiebreak_tiers, which rank_standings is built on.
No DB access -- rows are shaped like get_regular_season_state()'s query.
"""

import pytest

from lib.scenarios import (
    RemainingMatchup,
    apply_picks,
    projected_playoff_seeds,
    rank_standings,
    split_pairings,
)
from lib.seeding import MatchupRecord, UnresolvedTieError, resolve_tiebreak_group, resolve_tiebreak_tiers


def row(pairing_id, week, a, b, winner=None, is_tie=False, a_pts=None, b_pts=None):
    return {
        'id': pairing_id, 'week': week, 'player_a_id': a, 'player_b_id': b,
        'winner_id': winner, 'is_tie': is_tie, 'a_points': a_pts, 'b_points': b_pts,
    }


class TestSplitPairings:
    def test_played_and_remaining(self):
        records, remaining = split_pairings([
            row(1, 1, 10, 20, winner=10, a_pts=30, b_pts=25),
            row(2, 1, 30, 40, is_tie=True, a_pts=12, b_pts=12),
            row(3, 2, 10, 30),
        ])
        assert records[10] == [MatchupRecord(1, 20, 'W', 5)]
        assert records[20] == [MatchupRecord(1, 10, 'L', -5)]
        assert records[30] == [MatchupRecord(1, 40, 'T', 0)]
        assert remaining == [RemainingMatchup(3, 2, 10, 30)]

    def test_player_with_only_remaining_games_still_listed(self):
        records, remaining = split_pairings([row(1, 1, 10, 20)])
        assert records == {10: [], 20: []}
        assert len(remaining) == 1

    def test_missing_points_counts_as_zero_margin(self):
        records, _ = split_pairings([row(1, 1, 10, 20, winner=20)])
        assert records[20] == [MatchupRecord(1, 10, 'W', 0)]


class TestApplyPicks:
    def test_adds_margin_zero_results_and_skips_unpicked(self):
        records = {10: [], 20: [], 30: []}
        remaining = [RemainingMatchup(1, 5, 10, 20), RemainingMatchup(2, 5, 20, 30)]
        out = apply_picks(records, remaining, {1: 20})
        assert out[20] == [MatchupRecord(5, 10, 'W', 0)]
        assert out[10] == [MatchupRecord(5, 20, 'L', 0)]
        assert out[30] == []
        assert records == {10: [], 20: [], 30: []}  # input untouched

    def test_rejects_pick_outside_pairing(self):
        with pytest.raises(ValueError):
            apply_picks({}, [RemainingMatchup(1, 5, 10, 20)], {1: 99})


class TestResolveTiebreakTiers:
    def test_unbreakable_group_is_one_tier(self):
        # 10 and 20 split their two games with equal margins
        recs = {
            10: [MatchupRecord(1, 20, 'W', 3), MatchupRecord(2, 20, 'L', -3)],
            20: [MatchupRecord(1, 10, 'L', -3), MatchupRecord(2, 10, 'W', 3)],
        }
        assert resolve_tiebreak_tiers({10, 20}, recs, {}) == [{10, 20}]
        with pytest.raises(UnresolvedTieError):
            resolve_tiebreak_group({10, 20}, recs, {})

    def test_manual_override_splits_tier(self):
        recs = {
            10: [MatchupRecord(1, 20, 'W', 3), MatchupRecord(2, 20, 'L', -3)],
            20: [MatchupRecord(1, 10, 'L', -3), MatchupRecord(2, 10, 'W', 3)],
        }
        manual = {frozenset({10, 20}): [20, 10]}
        assert resolve_tiebreak_tiers({10, 20}, recs, manual) == [{20}, {10}]
        assert resolve_tiebreak_group({10, 20}, recs, manual) == [20, 10]


class TestRankStandings:
    def test_distinct_records_rank_in_order(self):
        recs = {
            1: [MatchupRecord(1, 2, 'W', 5), MatchupRecord(2, 3, 'W', 5)],
            2: [MatchupRecord(1, 1, 'L', -5), MatchupRecord(3, 3, 'W', 0)],
            3: [MatchupRecord(2, 1, 'L', -5), MatchupRecord(3, 2, 'L', 0)],
        }
        rows = rank_standings(recs, {})
        assert [(r['player_id'], r['rank'], r['tied']) for r in rows] == [
            (1, 1, False), (2, 2, False), (3, 3, False),
        ]

    def test_no_games_player_ties_on_pct_then_breaks_on_margin(self):
        # 3 is 0-1 at -5, 4 hasn't played: both .000, 4 wins on overall margin
        recs = {
            3: [MatchupRecord(1, 9, 'L', -5)],
            4: [],
            9: [MatchupRecord(1, 3, 'W', 5)],
        }
        rows = rank_standings(recs, {})
        assert [r['player_id'] for r in rows] == [9, 4, 3]

    def test_unbreakable_tie_shown_as_tied(self):
        recs = {
            10: [MatchupRecord(1, 20, 'W', 3), MatchupRecord(2, 20, 'L', -3)],
            20: [MatchupRecord(1, 10, 'L', -3), MatchupRecord(2, 10, 'W', 3)],
            30: [],
        }
        rows = rank_standings(recs, {})
        assert [(r['player_id'], r['rank'], r['tied']) for r in rows] == [
            (10, 1, True), (20, 1, True), (30, 3, False),
        ]

    def test_picks_change_standings(self):
        records, remaining = split_pairings([
            row(1, 1, 10, 20, winner=10, a_pts=20, b_pts=10),
            row(2, 2, 10, 20),
        ])
        rows = rank_standings(apply_picks(records, remaining, {2: 20}), {})
        # 1-1 each, h2h split, margin within group: 10 is +10 -> 10 ahead
        assert [r['player_id'] for r in rows] == [10, 20]
        assert not any(r['tied'] for r in rows)
        assert rows[1]['wins'] == 1 and rows[1]['net_margin'] == -10


class TestProjectedPlayoffSeeds:
    def test_six_team_pod_winner_gets_seed_two(self):
        # Overall order 1..6; pod A = {1,2,3}, pod B = {4,5,6}.
        standings = [
            {'rank': i, 'tied': False, 'player_id': i} for i in range(1, 7)
        ]
        pods = {1: 'A', 2: 'A', 3: 'A', 4: 'B', 5: 'B', 6: 'B'}
        seeds = projected_playoff_seeds(standings, pods)
        assert seeds[1] == 1
        assert seeds[4] == 2  # pod B's best gets seed 2 over pod A's #2
        assert seeds[2] == 3

    def test_refuses_with_ties(self):
        standings = [
            {'rank': 1, 'tied': True, 'player_id': 1},
            {'rank': 1, 'tied': True, 'player_id': 2},
        ]
        with pytest.raises(ValueError):
            projected_playoff_seeds(standings, {1: 'A', 2: 'A'})
