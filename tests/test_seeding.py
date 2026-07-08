"""
Unit tests for lib.seeding's pure computation (win_pct, record_and_margin,
resolve_tiebreak_group, compute_seeding). No DB access needed -- these
build MatchupRecord fixtures directly, mirroring how
get_pvp_regular_season_records() would shape real data.
"""

from fractions import Fraction

import pytest

from lib.seeding import (
    MatchupRecord,
    UnresolvedTieError,
    win_pct,
    record_and_margin,
    resolve_tiebreak_group,
    compute_seeding,
)


def rec(week, opponent_id, result, margin):
    return MatchupRecord(week=week, opponent_id=opponent_id, result=result, margin=margin)


class TestWinPct:
    def test_spec_worked_example(self):
        # 5-3-2 -> (5 + 1) / 10 = 0.600
        assert win_pct(5, 3, 2) == Fraction(6, 10) == Fraction(3, 5)

    def test_all_wins(self):
        assert win_pct(4, 0, 0) == Fraction(1)

    def test_no_games(self):
        assert win_pct(0, 0, 0) == Fraction(0)


class TestRecordAndMargin:
    def test_unscoped_counts_everything(self):
        records = [rec(1, 20, 'W', 10), rec(2, 30, 'L', -5), rec(3, 20, 'T', 0)]
        assert record_and_margin(records) == (1, 1, 1, 5)

    def test_scoped_to_opponent_filter(self):
        records = [rec(1, 20, 'W', 10), rec(2, 30, 'L', -5), rec(3, 20, 'T', 0)]
        # only opponent 20's games
        assert record_and_margin(records, {20}) == (1, 0, 1, 10)


class TestResolveTiebreakGroupTwoPlayer:
    def test_resolved_by_criterion_1_in_group_record(self):
        # Players 1 and 2 tied overall, but 1 beat 2 head-to-head twice, 2 beat 1 once
        # -> in-group record for player 1: 2-1-0, for player 2: 1-2-0 -> 1 separates them
        all_records = {
            1: [rec(1, 2, 'W', 5), rec(2, 2, 'W', 3), rec(3, 2, 'L', -2)],
            2: [rec(1, 1, 'L', -5), rec(2, 1, 'L', -3), rec(3, 1, 'W', 2)],
        }
        result = resolve_tiebreak_group({1, 2}, all_records, {})
        assert result == [1, 2]

    def test_resolved_by_criterion_2_in_group_margin(self):
        # Criterion 1 ties (1-1 head-to-head each), criterion 2 (in-group margin) separates:
        # player 1's margin vs 2 is +10 net, player 2's margin vs 1 is -10 net
        all_records = {
            1: [rec(1, 2, 'W', 8), rec(2, 2, 'L', -2)],
            2: [rec(1, 1, 'L', -8), rec(2, 1, 'W', 2)],
        }
        result = resolve_tiebreak_group({1, 2}, all_records, {})
        assert result == [1, 2]

    def test_resolved_by_criterion_3_full_season_margin(self):
        # Criteria 1 and 2 tie (never played each other at all -> 0 games,
        # 0 margin in-group), criterion 3 (unscoped season margin) separates.
        all_records = {
            1: [rec(1, 3, 'W', 50)],
            2: [rec(1, 3, 'L', -5)],
        }
        result = resolve_tiebreak_group({1, 2}, all_records, {})
        assert result == [1, 2]

    def test_all_criteria_tied_no_manual_override_raises(self):
        all_records = {
            1: [],
            2: [],
        }
        with pytest.raises(UnresolvedTieError) as exc_info:
            resolve_tiebreak_group({1, 2}, all_records, {})
        assert exc_info.value.group == {1, 2}

    def test_all_criteria_tied_with_manual_override_resolves(self):
        all_records = {1: [], 2: []}
        manual = {frozenset({1, 2}): [2, 1]}
        result = resolve_tiebreak_group({1, 2}, all_records, manual)
        assert result == [2, 1]


class TestResolveTiebreakGroupThreePlayer:
    def test_outright_leader_plus_tied_pair(self):
        # Player 1 beats both 2 and 3 in-group; 2 and 3 split their own
        # meeting (criterion 1 ties for the {2,3} sub-group) but 2 has a
        # bigger in-group margin against 3 (criterion 2 separates them).
        all_records = {
            1: [rec(1, 2, 'W', 10), rec(2, 3, 'W', 10)],
            2: [rec(1, 1, 'L', -10), rec(2, 3, 'W', 20), rec(3, 3, 'L', -5)],
            3: [rec(1, 1, 'L', -10), rec(2, 2, 'L', -20), rec(3, 2, 'W', 5)],
        }
        result = resolve_tiebreak_group({1, 2, 3}, all_records, {})
        assert result == [1, 2, 3]

    def test_tied_pair_at_top_plus_clear_bottom(self):
        # Players 1 and 2 both beat player 3 in-group (2-0 in-group each for 1,2;
        # 0-2 for 3) -> criterion 1 splits into class {1,2} (win_pct 1.0) and
        # class {3} (win_pct 0.0). Player 3 is seeded last immediately;
        # {1,2} recurses from criterion 1 among just themselves, where their
        # head-to-head record separates them.
        all_records = {
            1: [rec(1, 3, 'W', 10), rec(2, 2, 'W', 5)],
            2: [rec(1, 3, 'W', 8), rec(2, 1, 'L', -5)],
            3: [rec(1, 1, 'L', -10), rec(2, 2, 'L', -8)],
        }
        result = resolve_tiebreak_group({1, 2, 3}, all_records, {})
        assert result == [1, 2, 3]


def test_compute_seeding_orders_by_win_pct_then_resolves_ties():
    # Player 10: 3-0 (best, no tiebreak needed)
    # Players 20/30: both 1-1, tied overall; 20 beat 30 head-to-head -> 20 seeded above 30
    all_records = {
        10: [rec(1, 20, 'W', 5), rec(2, 30, 'W', 5), rec(3, 20, 'W', 5)],
        20: [rec(1, 10, 'L', -5), rec(2, 30, 'W', 3), rec(3, 10, 'L', -5)],
        30: [rec(1, 10, 'L', -5), rec(2, 20, 'L', -3)],
    }
    seeding = compute_seeding(all_records, {})

    assert [row['player_id'] for row in seeding] == [10, 20, 30]
    assert [row['seed'] for row in seeding] == [1, 2, 3]
    assert seeding[0]['wins'] == 3 and seeding[0]['losses'] == 0
