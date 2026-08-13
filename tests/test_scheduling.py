"""
Unit tests for lib.scheduling -- pure Python, no DB, no Streamlit.
"""

from collections import Counter

import pytest

from lib.scheduling import (
    circle_method_round_robin,
    repeat_rounds,
    bipartite_round_robin,
    spaced_positions,
    interleave_pvp_weeks,
    build_pvp_schedule,
)


def _all_pairs_frozensets(rounds):
    return [frozenset(pair) for rnd in rounds for pair in rnd]


class TestCircleMethodRoundRobin:
    def test_even_players_every_pair_once(self):
        rounds = circle_method_round_robin([1, 2, 3, 4])

        assert len(rounds) == 3
        assert all(len(rnd) == 2 for rnd in rounds)

        pairs = _all_pairs_frozensets(rounds)
        expected = {frozenset(p) for p in [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]}
        assert Counter(pairs) == Counter(expected)  # each exactly once

        appearances = Counter(p for rnd in rounds for pair in rnd for p in pair)
        assert appearances == {1: 3, 2: 3, 3: 3, 4: 3}

    def test_odd_players_every_pair_once_one_bye_each(self):
        rounds = circle_method_round_robin([1, 2, 3, 4, 5])

        assert len(rounds) == 5

        pairs = _all_pairs_frozensets(rounds)
        expected = {frozenset(p) for p in
                    [(1, 2), (1, 3), (1, 4), (1, 5), (2, 3), (2, 4), (2, 5), (3, 4), (3, 5), (4, 5)]}
        assert Counter(pairs) == Counter(expected)

        appearances = Counter(p for rnd in rounds for pair in rnd for p in pair)
        assert appearances == {1: 4, 2: 4, 3: 4, 4: 4, 5: 4}  # each sits out exactly 1 of 5 rounds


class TestRepeatRounds:
    def test_triple_round_robin_each_pair_three_times(self):
        base = circle_method_round_robin([1, 2, 3, 4])
        tripled = repeat_rounds(base, 3)

        assert len(tripled) == 9
        pairs = _all_pairs_frozensets(tripled)
        counts = Counter(pairs)
        assert all(c == 3 for c in counts.values())
        assert len(counts) == 6  # 6 unique pairs among 4 players


class TestBipartiteRoundRobin:
    def test_4v4_every_cross_pair_once(self):
        rounds = bipartite_round_robin([1, 2, 3, 4], [11, 12, 13, 14])

        assert len(rounds) == 4
        assert all(len(rnd) == 4 for rnd in rounds)

        pairs = [pair for rnd in rounds for pair in rnd]
        assert len(pairs) == 16
        assert len(set(pairs)) == 16  # all 16 (a, b) combinations, each exactly once

        for i in [1, 2, 3, 4]:
            assert sum(1 for a, b in pairs if a == i) == 4  # meets all 4 of pod_b

    def test_unequal_pods_raises(self):
        with pytest.raises(ValueError):
            bipartite_round_robin([1, 2, 3], [11, 12, 13, 14])


class TestSpacedPositions:
    def test_two_byes_in_eleven_weeks(self):
        assert spaced_positions(2, 11) == [4, 7]

    def test_one_bye_in_eleven_weeks(self):
        assert spaced_positions(1, 11) == [6]

    def test_zero_count_returns_empty(self):
        assert spaced_positions(0, 11) == []


class TestInterleavePvpWeeks:
    def test_single_pod_shape_no_cross_pod(self):
        result = interleave_pvp_weeks(n_in=9, n_cross=0, total_weeks=11)

        assert len(result) == 11
        assert result[3] == 'bye'   # week 4 (0-indexed 3)
        assert result[6] == 'bye'   # week 7 (0-indexed 6)
        assert result.count('in_pod') == 9
        assert result.count('bye') == 2
        assert result.count('cross_pod') == 0

    def test_two_pod_even_alternation_matches_confirmed_pattern(self):
        result = interleave_pvp_weeks(n_in=6, n_cross=4, total_weeks=11)

        expected = ['in_pod', 'cross_pod', 'in_pod', 'cross_pod', 'in_pod', 'bye',
                    'in_pod', 'cross_pod', 'in_pod', 'cross_pod', 'in_pod']
        assert result == expected

    def test_starts_and_ends_on_in_pod(self):
        result = interleave_pvp_weeks(n_in=6, n_cross=4, total_weeks=11)
        assert result[0] == 'in_pod'
        assert result[-1] == 'in_pod'

    def test_n_cross_greater_than_n_in_raises(self):
        with pytest.raises(ValueError):
            interleave_pvp_weeks(n_in=2, n_cross=5, total_weeks=11)

    def test_schedule_does_not_fit_raises(self):
        with pytest.raises(ValueError):
            interleave_pvp_weeks(n_in=9, n_cross=0, total_weeks=5)


class TestBuildPvpSchedule:
    def test_single_pod_four_players(self):
        schedule = build_pvp_schedule(single_pod_players=[1, 2, 3, 4])

        assert set(schedule.keys()) == set(range(1, 12))
        assert schedule[4] == []
        assert schedule[7] == []

        non_bye_weeks = [wk for wk, games in schedule.items() if games]
        assert len(non_bye_weeks) == 9
        assert all(len(schedule[wk]) == 2 for wk in non_bye_weeks)
        assert all(g[2] == 'in_pod' for wk in non_bye_weeks for g in schedule[wk])

        pairs = [frozenset((a, b)) for wk in non_bye_weeks for a, b, _ in schedule[wk]]
        counts = Counter(pairs)
        assert all(c == 3 for c in counts.values())
        assert len(counts) == 6

    def test_single_pod_with_pod_of_player_labels_matchup_type(self):
        # The 3-player-pod PVP design: 2 pods of 3 (6 players total),
        # flat double round robin, matchup_type derived from pod
        # membership rather than the tiered two-pod path.
        pod_of_player = {1: 'A', 2: 'A', 3: 'A', 11: 'B', 12: 'B', 13: 'B'}
        schedule = build_pvp_schedule(
            single_pod_players=[1, 2, 3, 11, 12, 13],
            pod_of_player=pod_of_player,
            regular_season_weeks=10,
            single_pod_repeats=2,
        )

        non_bye_weeks = [wk for wk, games in schedule.items() if games]
        all_games = [g for wk in non_bye_weeks for g in schedule[wk]]

        # Every pair meets exactly twice (double round robin over all 6).
        pairs = Counter(frozenset((a, b)) for a, b, _ in all_games)
        assert all(c == 2 for c in pairs.values())
        assert len(pairs) == 15  # C(6,2)

        # matchup_type matches pod membership, not a hardcoded label.
        for a, b, matchup_type in all_games:
            same_pod = pod_of_player[a] == pod_of_player[b]
            assert matchup_type == ('in_pod' if same_pod else 'cross_pod')

        in_pod_games = [g for g in all_games if g[2] == 'in_pod']
        cross_games = [g for g in all_games if g[2] == 'cross_pod']
        assert len(in_pod_games) == 12   # 2 pods x C(3,2)=3 pairs x 2 meetings
        assert len(cross_games) == 18    # 3x3=9 cross pairs x 2 meetings

    def test_single_pod_without_pod_of_player_still_labels_in_pod(self):
        # Backward-compat: omitting pod_of_player keeps the original
        # single-pod behavior (every pair 'in_pod') unchanged.
        schedule = build_pvp_schedule(single_pod_players=[1, 2, 3, 4])
        non_bye_weeks = [wk for wk, games in schedule.items() if games]
        assert all(g[2] == 'in_pod' for wk in non_bye_weeks for g in schedule[wk])

    def test_two_pod_four_and_four(self):
        schedule = build_pvp_schedule(pod_a=[1, 2, 3, 4], pod_b=[11, 12, 13, 14])

        assert set(schedule.keys()) == set(range(1, 12))
        assert schedule[6] == []  # the single bye week

        non_bye_weeks = [wk for wk, games in schedule.items() if games]
        assert len(non_bye_weeks) == 10

        # no player appears twice in the same week
        for wk in non_bye_weeks:
            players_this_week = [p for a, b, _ in schedule[wk] for p in (a, b)]
            assert len(players_this_week) == len(set(players_this_week))

        # every player has exactly 10 games: 6 in-pod (3 podmates x2) + 4 cross-pod
        all_games = [g for wk in non_bye_weeks for g in schedule[wk]]
        game_count = Counter(p for a, b, _ in all_games for p in (a, b))
        assert all(c == 10 for c in game_count.values())

        in_pod_games = [g for g in all_games if g[2] == 'in_pod']
        cross_games = [g for g in all_games if g[2] == 'cross_pod']
        assert len(in_pod_games) == 24  # 6 weeks x (2 games from pod A + 2 from pod B)
        assert len(cross_games) == 16   # 4 weeks x 4 concurrent cross-pod games

        # in-pod pairs never cross pods; cross-pod pairs always do
        assert all(
            {a, b} <= {1, 2, 3, 4} or {a, b} <= {11, 12, 13, 14}
            for a, b, _ in in_pod_games
        )
        assert all(
            (a in [1, 2, 3, 4]) != (b in [1, 2, 3, 4])
            for a, b, _ in cross_games
        )

    def test_both_args_given_raises(self):
        with pytest.raises(ValueError):
            build_pvp_schedule(single_pod_players=[1, 2, 3, 4], pod_a=[5, 6], pod_b=[7, 8])

    def test_neither_arg_given_raises(self):
        with pytest.raises(ValueError):
            build_pvp_schedule()

    def test_unequal_pod_sizes_raises(self):
        with pytest.raises(ValueError):
            build_pvp_schedule(pod_a=[1, 2, 3], pod_b=[11, 12, 13, 14])

    def test_does_not_fit_week_budget_raises(self):
        with pytest.raises(ValueError):
            build_pvp_schedule(single_pod_players=[1, 2, 3, 4], regular_season_weeks=5)
