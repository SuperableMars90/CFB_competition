"""
Unit tests for lib.optimal_lineup's change-budget variant
(optimize_lineup_with_change_budget / min_changes_to_beat) -- pure, no DB.

The load-bearing test is test_matches_brute_force: random small instances
checked against exhaustive enumeration of every lineup, so the flow
model's change accounting (max(#in, #out), reshuffles free) is verified
independently of how it's encoded.
"""

import itertools
import random

import pytest

from lib.optimal_lineup import (
    TeamWeekResult,
    _eligible_categories,
    min_changes_to_beat,
    optimize_lineup,
    optimize_lineup_with_change_budget,
)

CONFS = {'ACC': 'P4', 'B12': 'P4', 'B1G': 'P4', 'SEC': 'P4',
         'AAC': 'G6', 'CUSA': 'G6', 'MAC': 'G6', 'MWC': 'G6', 'PAC': 'G6', 'SBC': 'G6'}

# Small layout for brute force: 2 conference slots + 1 P4 flex + 1 G6 flex + 1 wildcard.
SMALL_CONFS = {'ACC': 'P4', 'AAC': 'G6'}
SMALL_SLOTS = dict(p4_flex_slots=1, g6_flex_slots=1, wildcard_slots=1)


def team(tid, conf, tier, margin, name=None):
    return TeamWeekResult(team_id=tid, name=name or f't{tid}', conference_abbreviation=conf,
                          tier=tier, margin=margin)


def ids(teams):
    return sorted(t.team_id for t in teams)


# --- change accounting -------------------------------------------------------

def test_zero_budget_returns_submitted_lineup_including_losers():
    submitted = [team(1, 'ACC', 'P4', 20), team(2, 'SEC', 'P4', -15)]
    pool = [team(3, 'ACC', 'P4', 50)]
    r = optimize_lineup_with_change_budget(submitted, pool, 0, CONFS)
    assert r.total == 5
    assert r.changes == 0
    assert ids(r.removed) == [] and ids(r.brought_in) == []


def test_benching_a_loser_to_a_pass_costs_one_change():
    submitted = [team(1, 'ACC', 'P4', 20), team(2, 'SEC', 'P4', -15)]
    r = optimize_lineup_with_change_budget(submitted, [], 1, CONFS)
    assert r.total == 20
    assert r.changes == 1
    assert ids(r.removed) == [2]


def test_straight_swap_is_one_change():
    submitted = [team(1, 'ACC', 'P4', -5)]
    pool = [team(2, 'ACC', 'P4', 30)]
    r = optimize_lineup_with_change_budget(submitted, pool, 1, CONFS, p4_flex_slots=0, wildcard_slots=0)
    assert r.total == 30
    assert ids(r.removed) == [1] and ids(r.brought_in) == [2]
    assert r.changes == 1


def test_reshuffling_kept_teams_is_free():
    # Kept independent sits in ACC; the incoming ACC-only team needs that
    # slot, so the independent must move to B12. Only one team comes in and
    # nothing is removed: one change, not two.
    submitted = [team(1, 'IndP4', 'P4', 10), team(2, 'SEC', 'P4', 5)]
    pool = [team(3, 'ACC', 'P4', 40)]
    confs = {'ACC': 'P4', 'B12': 'P4', 'SEC': 'P4'}
    r = optimize_lineup_with_change_budget(submitted, pool, 1, confs,
                                           p4_flex_slots=0, g6_flex_slots=0, wildcard_slots=0)
    assert r.total == 55
    assert r.changes == 1
    slot_of = {p.team_id: p.category for p in r.picks}
    assert slot_of[3] == 'ACC' and slot_of[1] == 'B12'


def test_filling_a_passed_slot_is_one_change():
    submitted = [team(1, 'ACC', 'P4', 10)]
    pool = [team(2, 'SEC', 'P4', 7)]
    r = optimize_lineup_with_change_budget(submitted, pool, 1, CONFS)
    assert r.total == 17
    assert ids(r.brought_in) == [2] and r.removed == []


def test_equal_margin_swap_is_not_spent():
    submitted = [team(1, 'ACC', 'P4', 7)]
    pool = [team(2, 'ACC', 'P4', 7)]
    r = optimize_lineup_with_change_budget(submitted, pool, 1, CONFS, p4_flex_slots=0, wildcard_slots=0)
    assert r.total == 7
    assert r.changes == 0


def test_large_budget_matches_unconstrained_optimum():
    submitted = [team(1, 'ACC', 'P4', -3), team(2, 'AAC', 'G6', 4), team(3, 'IndP4', 'P4', 12)]
    pool = [team(4, 'ACC', 'P4', 9), team(5, 'SEC', 'P4', 11), team(6, 'MAC', 'G6', -2), team(7, 'IndG6', 'G6', 6)]
    r = optimize_lineup_with_change_budget(submitted, pool, 16, CONFS)
    assert r.total == optimize_lineup(submitted + pool, CONFS).total


def test_submitted_without_margin_is_rejected():
    with pytest.raises(ValueError):
        optimize_lineup_with_change_budget([team(1, 'ACC', 'P4', None)], [], 0, CONFS)


# --- brute force cross-check -------------------------------------------------

def _small_categories():
    cats = {abbr: 1 for abbr in SMALL_CONFS}
    cats.update({'P4_FLEX': 1, 'G6_FLEX': 1, 'WILDCARD': 1})
    return cats


def _feasible(items):
    cats = _small_categories()

    def place(i, used):
        if i == len(items):
            return True
        for c in _eligible_categories(items[i], SMALL_CONFS):
            if c in cats and used.get(c, 0) < cats[c]:
                used[c] = used.get(c, 0) + 1
                if place(i + 1, used):
                    return True
                used[c] -= 1
        return False

    return place(0, {})


def _brute_best(submitted, pool, k):
    best = None
    sub_idx = range(len(submitted))
    pool_idx = range(len(pool))
    for n_keep in range(len(submitted) + 1):
        for keep in itertools.combinations(sub_idx, n_keep):
            out = len(submitted) - n_keep
            for n_in in range(len(pool) + 1):
                if max(out, n_in) > k:
                    continue
                for bring in itertools.combinations(pool_idx, n_in):
                    lineup = [submitted[i] for i in keep] + [pool[j] for j in bring]
                    if len(lineup) > 5 or not _feasible(lineup):
                        continue
                    total = sum(t.margin for t in lineup)
                    if best is None or total > best:
                        best = total
    return best


def _random_instance(rng):
    confs = [('ACC', 'P4'), ('AAC', 'G6'), ('SEC', 'P4'), ('MAC', 'G6'), ('IndP4', 'P4'), ('IndG6', 'G6')]
    items = [team(i, *rng.choice(confs), rng.randint(-12, 15)) for i in range(rng.randint(2, 8))]
    rng.shuffle(items)
    submitted = []
    for t in items:
        if len(submitted) < 5 and rng.random() < 0.6 and _feasible(submitted + [t]):
            submitted.append(t)
    pool = [t for t in items if t not in submitted]
    return submitted, pool


def test_matches_brute_force():
    rng = random.Random(20261004)
    for _ in range(300):
        submitted, pool = _random_instance(rng)
        for k in range(0, 6):
            r = optimize_lineup_with_change_budget(submitted, pool, k, SMALL_CONFS, **SMALL_SLOTS)
            assert r.total == _brute_best(submitted, pool, k), (submitted, pool, k)
            assert r.changes <= k
            assert _feasible([p for p in submitted + pool if p.team_id in {x.team_id for x in r.picks}])


# --- min_changes_to_beat -----------------------------------------------------

def test_min_changes_win():
    submitted = [team(1, 'ACC', 'P4', -10), team(2, 'SEC', 'P4', -8), team(3, 'B12', 'P4', 30)]
    pool = [team(4, 'ACC', 'P4', 5)]
    # 12 now; target 21 needs the -10 out. Swapping it for the +5 (27)
    # beats benching it to a pass (22) at the same one change.
    r = min_changes_to_beat(submitted, pool, 21, CONFS)
    assert r.changes == 1
    assert r.lineup.total == 27
    assert ids(r.lineup.removed) == [1] and ids(r.lineup.brought_in) == [4]
    assert r.tie_changes is None


def test_min_changes_records_tie_before_win():
    submitted = [team(1, 'ACC', 'P4', -5), team(2, 'SEC', 'P4', 10)]
    pool = [team(3, 'B12', 'P4', 5), team(4, 'B1G', 'P4', 3)]
    # 5 now. 1 change -> best 15 (swap -5 for +5): ties target 15;
    # 2 changes -> 18 (also bring in the +3)
    r = min_changes_to_beat(submitted, pool, 15, CONFS)
    assert r.tie_changes == 1
    assert r.changes == 2
    assert r.lineup.total == 18


def test_min_changes_unflippable_returns_optimum():
    submitted = [team(1, 'ACC', 'P4', 3)]
    pool = [team(2, 'SEC', 'P4', 4)]
    r = min_changes_to_beat(submitted, pool, 100, CONFS)
    assert r.changes is None
    assert r.lineup.total == 7


def test_min_changes_zero_when_already_winning():
    r = min_changes_to_beat([team(1, 'ACC', 'P4', 3)], [], 2, CONFS)
    assert r.changes == 0
