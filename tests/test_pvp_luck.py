"""Unit tests for lib/pvp_luck.py -- pure logic, no DB."""

import random
from itertools import permutations

import pytest

from lib.pvp_luck import (
    actual_halves,
    score_round,
    summarize_luck,
    win_distributions,
)

# 4 players, 3 rounds of a single round robin (every pair meets once).
ROUNDS = [
    [(1, 2), (3, 4)],
    [(1, 3), (2, 4)],
    [(1, 4), (2, 3)],
]


def brute_force(rounds, totals_by_week):
    weeks = sorted(totals_by_week)
    players = sorted({p for t in totals_by_week.values() for p in t})
    counts = {p: [0] * (2 * len(weeks) + 1) for p in players}
    n = 0
    for order in permutations(range(len(rounds)), len(weeks)):
        n += 1
        halves = dict.fromkeys(players, 0)
        for week, r in zip(weeks, order):
            for p, h in score_round(rounds[r], totals_by_week[week]).items():
                halves[p] += h
        for p in players:
            counts[p][halves[p]] += 1
    return {p: [c / n for c in row] for p, row in counts.items()}


def test_score_round_win_tie_loss_and_bye():
    out = score_round([(1, 2), (3, 4)], {1: 10, 2: 5, 3: 7, 4: 7, 5: 99})
    assert out == {1: 2, 2: 0, 3: 1, 4: 1}  # player 5 (bye) absent


def test_actual_halves():
    pairings = {1: ROUNDS[0], 2: ROUNDS[1]}
    totals = {
        1: {1: 10, 2: 5, 3: 7, 4: 7},   # 1 beats 2; 3 ties 4
        2: {1: 1, 2: 9, 3: 3, 4: 2},    # 3 beats 1; 2 beats 4
    }
    assert actual_halves(pairings, totals) == {1: 2, 2: 2, 3: 3, 4: 1}


def test_matches_brute_force_with_ties():
    rng = random.Random(7)
    totals = {
        w: {p: rng.choice([-3, 0, 4, 4, 9]) for p in (1, 2, 3, 4)}  # ties likely
        for w in (1, 2, 3)
    }
    got = win_distributions(ROUNDS, totals)
    want = brute_force(ROUNDS, totals)
    for p in want:
        assert got[p] == pytest.approx(want[p])


def test_partial_season_uses_whole_round_pool():
    # 2 weeks evaluated out of a 3-round pool -> 3*2 = 6 orderings.
    totals = {1: {1: 5, 2: 1, 3: 3, 4: 2}, 2: {1: 0, 2: 8, 3: 4, 4: 6}}
    got = win_distributions(ROUNDS, totals)
    want = brute_force(ROUNDS, totals)
    for p in want:
        assert got[p] == pytest.approx(want[p])
        assert sum(got[p]) == pytest.approx(1.0)


def test_bye_player_gets_zero_for_rounds_they_sit_out():
    rounds = [[(1, 2)], [(2, 3)], [(1, 3)]]  # odd league: someone sits each round
    totals = {1: {1: 9, 2: 5, 3: 1}, 2: {1: 2, 2: 6, 3: 4}}
    got = win_distributions(rounds, totals)
    want = brute_force(rounds, totals)
    for p in want:
        assert got[p] == pytest.approx(want[p])


def test_more_weeks_than_rounds_raises():
    totals = {w: {1: 1, 2: 2, 3: 3, 4: 4} for w in (1, 2, 3, 4)}
    with pytest.raises(ValueError):
        win_distributions(ROUNDS, totals)


def test_summarize_luck_jacob_style_zero_and_two():
    # Player 1 has the lowest total both weeks, player 4 the highest: no
    # matter the order, 1 always loses and 4 always wins.
    totals = {w: {1: 0, 2: 1, 3: 2, 4: 3} for w in (1, 2)}
    dist = win_distributions(ROUNDS, totals)
    s = summarize_luck(dist, actual_halves({1: ROUNDS[0], 2: ROUNDS[1]}, totals))
    assert s[1]["actual_wins"] == 0 and s[1]["expected_wins"] == 0 and s[1]["luck"] == 0
    assert s[4]["actual_wins"] == 2 and s[4]["p_exact"] == pytest.approx(1.0)
    assert s[1]["z"] is None


def test_summarize_luck_tail_probabilities():
    totals = {1: {1: 5, 2: 1, 3: 3, 4: 2}, 2: {1: 0, 2: 8, 3: 4, 4: 6}}
    dist = win_distributions(ROUNDS, totals)
    actual = actual_halves({1: ROUNDS[0], 2: ROUNDS[1]}, totals)
    s = summarize_luck(dist, actual)
    for p, row in s.items():
        a = actual[p]
        assert row["p_at_most"] == pytest.approx(sum(dist[p][: a + 1]))
        assert row["p_at_least"] == pytest.approx(sum(dist[p][a:]))
        assert row["p_at_most"] + row["p_at_least"] - row["p_exact"] == pytest.approx(1.0)
        assert row["luck"] == pytest.approx(row["actual_wins"] - row["expected_wins"])
