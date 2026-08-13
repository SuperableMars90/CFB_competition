"""
Unit tests for lib.metagame's pure PVP-resolution and pod-standings
computation.

compute_pvp_resolutions() and compute_pod_standings() are pure Python (no
DB) -- their DB-writing counterparts (write_pvp_resolutions(),
write_weekly_results()) are thin wrappers and aren't given dedicated unit
tests here, consistent with lib.records's write_records() having no
dedicated unit test either. compute_weekly_standings() itself isn't unit
tested since it opens its own DB connection (get_scoring_context) --
verified live instead, per this repo's established pattern.
"""

import pytest

from lib.metagame import compute_pvp_resolutions, compute_pod_standings


def test_win_sets_winner_id_and_is_tie_false():
    pairings = [{'id': 1, 'player_a_id': 10, 'player_b_id': 20}]
    player_totals = {10: {'total': 50}, 20: {'total': 30}}

    result = compute_pvp_resolutions(pairings, player_totals)

    assert result == [{'pairing_id': 1, 'winner_id': 10, 'is_tie': False}]


def test_loss_side_sets_correct_winner():
    pairings = [{'id': 1, 'player_a_id': 10, 'player_b_id': 20}]
    player_totals = {10: {'total': 15}, 20: {'total': 40}}

    result = compute_pvp_resolutions(pairings, player_totals)

    assert result == [{'pairing_id': 1, 'winner_id': 20, 'is_tie': False}]


def test_tie_sets_winner_id_none_and_is_tie_true():
    pairings = [{'id': 1, 'player_a_id': 10, 'player_b_id': 20}]
    player_totals = {10: {'total': 25}, 20: {'total': 25}}

    result = compute_pvp_resolutions(pairings, player_totals)

    assert result == [{'pairing_id': 1, 'winner_id': None, 'is_tie': True}]


def test_pairing_missing_a_players_total_is_skipped():
    pairings = [{'id': 1, 'player_a_id': 10, 'player_b_id': 20}]
    player_totals = {10: {'total': 25}}  # player 20 not present

    result = compute_pvp_resolutions(pairings, player_totals)

    assert result == []


def test_multiple_pairings_mixed_results():
    pairings = [
        {'id': 1, 'player_a_id': 10, 'player_b_id': 20},
        {'id': 2, 'player_a_id': 30, 'player_b_id': 40},
        {'id': 3, 'player_a_id': 50, 'player_b_id': 60},
    ]
    player_totals = {
        10: {'total': 100}, 20: {'total': 50},   # 10 wins
        30: {'total': 10}, 40: {'total': 10},    # tie
        50: {'total': 5},                        # 60 missing -> skipped
    }

    result = compute_pvp_resolutions(pairings, player_totals)

    assert result == [
        {'pairing_id': 1, 'winner_id': 10, 'is_tie': False},
        {'pairing_id': 2, 'winner_id': None, 'is_tie': True},
    ]


# ------------------------------------------------------------------
# compute_pod_standings -- cross-pod match-play formula
# ------------------------------------------------------------------

CTX = {'id': 15, 'points_map': [7, 6, 5, 4, 3, 2, 1, 0]}

POD_A, POD_B = 100, 200


def _totals(**by_player):
    """{player_id: {'player_name': ..., 'total': ...}} shorthand."""
    return {pid: {'player_name': f'P{pid}', 'total': total} for pid, total in by_player.items()}


def test_pod_standings_base_points_by_overall_rank():
    # a1..a4 in POD_A, b1..b4 in POD_B; overall order: a1,b1,a2,b2,a3,b3,a4,b4
    player_totals = _totals(a1=50, a2=30, a3=20, a4=10, b1=45, b2=25, b3=15, b4=5)
    pod_of_player = {'a1': POD_A, 'a2': POD_A, 'a3': POD_A, 'a4': POD_A,
                      'b1': POD_B, 'b2': POD_B, 'b3': POD_B, 'b4': POD_B}

    results = compute_pod_standings(week=1, season_id=8, ctx=CTX,
                                     player_totals=player_totals, pod_of_player=pod_of_player)
    by_id = {r['player_id']: r for r in results}

    assert by_id['a1']['overall_rank'] == 1 and by_id['a1']['base_points'] == 7
    assert by_id['b1']['overall_rank'] == 2 and by_id['b1']['base_points'] == 6
    assert by_id['a2']['overall_rank'] == 3 and by_id['a2']['base_points'] == 5
    assert by_id['b4']['overall_rank'] == 8 and by_id['b4']['base_points'] == 0


def test_pod_standings_bonuses_stack_to_max_ten_for_overall_winner():
    # a1 wins overall, wins their pod, and pod A's combined total is higher.
    player_totals = _totals(a1=50, a2=30, a3=20, a4=10, b1=45, b2=25, b3=15, b4=5)
    pod_of_player = {'a1': POD_A, 'a2': POD_A, 'a3': POD_A, 'a4': POD_A,
                      'b1': POD_B, 'b2': POD_B, 'b3': POD_B, 'b4': POD_B}
    player_totals = {k: v for k, v in zip(pod_of_player, player_totals.values())}

    results = compute_pod_standings(week=1, season_id=8, ctx=CTX,
                                     player_totals=player_totals, pod_of_player=pod_of_player)
    by_id = {r['player_id']: r for r in results}

    # pod A total = 110, pod B total = 90 -> A wins pod-vs-pod
    a1 = by_id['a1']
    assert a1['base_points'] == 7
    assert a1['pod_bonus'] == 1        # top of pod A
    assert a1['pod_vs_pod_bonus'] == 1  # pod A's combined total is higher
    assert a1['overall_bonus'] == 1    # overall #1
    assert a1['match_play_points'] == 10

    # b1: 2nd overall, wins pod B, but pod B lost pod-vs-pod and isn't overall #1
    b1 = by_id['b1']
    assert b1['base_points'] == 6
    assert b1['pod_bonus'] == 1
    assert b1['pod_vs_pod_bonus'] == 0
    assert b1['overall_bonus'] == 0
    assert b1['match_play_points'] == 7

    # b4: last place, no bonuses at all
    b4 = by_id['b4']
    assert b4['match_play_points'] == 0


def test_pod_standings_exact_pod_tie_awards_no_pod_vs_pod_bonus_to_either():
    player_totals = _totals(a1=40, a2=30, b1=35, b2=35)
    pod_of_player = {'a1': POD_A, 'a2': POD_A, 'b1': POD_B, 'b2': POD_B}
    player_totals = {k: v for k, v in zip(pod_of_player, player_totals.values())}
    # pod A total = 70, pod B total = 70 -> exact tie

    results = compute_pod_standings(week=1, season_id=8, ctx=CTX,
                                     player_totals=player_totals, pod_of_player=pod_of_player)

    assert all(r['pod_vs_pod_bonus'] == 0 for r in results)


def test_pod_standings_requires_exactly_two_pods():
    player_totals = _totals(a1=10, a2=5)
    pod_of_player = {'a1': POD_A, 'a2': POD_A}  # only 1 pod present

    with pytest.raises(ValueError):
        compute_pod_standings(week=1, season_id=8, ctx=CTX,
                               player_totals=player_totals, pod_of_player=pod_of_player)


# ------------------------------------------------------------------
# compute_pod_standings -- 3-player-pod (6 total players) fixtures.
# Same formula/code path as the 8-player tests above (no code change
# expected -- points_map is just a shorter list); confirmed points_map
# for this format (Zach, 2026-08-09): rank r gets (6-r) points, same
# "1 point per spot" logic as season 8's [7,6,5,4,3,2,1,0], just for 6
# total players instead of 8. Max weekly total drops to 8 (5 base + 3
# bonuses), not 10 -- season 8's max=10 was an artifact of 8 players,
# not an invariant this format needs to preserve.
# ------------------------------------------------------------------

CTX_SIX = {'id': 16, 'points_map': [5, 4, 3, 2, 1, 0]}


def test_pod_standings_six_player_base_points_by_overall_rank():
    # a1..a3 in POD_A, b1..b3 in POD_B; overall order: a1,b1,a2,b2,a3,b3
    player_totals = _totals(a1=50, a2=30, a3=10, b1=45, b2=25, b3=5)
    pod_of_player = {'a1': POD_A, 'a2': POD_A, 'a3': POD_A,
                      'b1': POD_B, 'b2': POD_B, 'b3': POD_B}

    results = compute_pod_standings(week=1, season_id=44, ctx=CTX_SIX,
                                     player_totals=player_totals, pod_of_player=pod_of_player)
    by_id = {r['player_id']: r for r in results}

    assert by_id['a1']['overall_rank'] == 1 and by_id['a1']['base_points'] == 5
    assert by_id['b1']['overall_rank'] == 2 and by_id['b1']['base_points'] == 4
    assert by_id['a2']['overall_rank'] == 3 and by_id['a2']['base_points'] == 3
    assert by_id['b3']['overall_rank'] == 6 and by_id['b3']['base_points'] == 0


def test_pod_standings_six_player_bonuses_stack_to_max_eight_for_overall_winner():
    # a1 wins overall, wins their (3-player) pod, and pod A's combined
    # total is higher -- max weekly total for this format is 8, not 10.
    player_totals = _totals(a1=50, a2=30, a3=10, b1=45, b2=25, b3=5)
    pod_of_player = {'a1': POD_A, 'a2': POD_A, 'a3': POD_A,
                      'b1': POD_B, 'b2': POD_B, 'b3': POD_B}

    results = compute_pod_standings(week=1, season_id=44, ctx=CTX_SIX,
                                     player_totals=player_totals, pod_of_player=pod_of_player)
    by_id = {r['player_id']: r for r in results}

    # pod A total = 90, pod B total = 75 -> A wins pod-vs-pod
    a1 = by_id['a1']
    assert a1['base_points'] == 5
    assert a1['pod_bonus'] == 1        # top of pod A
    assert a1['pod_vs_pod_bonus'] == 1  # pod A's combined total is higher
    assert a1['overall_bonus'] == 1    # overall #1
    assert a1['match_play_points'] == 8

    # b1: 2nd overall, wins pod B, but pod B lost pod-vs-pod and isn't overall #1
    b1 = by_id['b1']
    assert b1['base_points'] == 4
    assert b1['pod_bonus'] == 1
    assert b1['pod_vs_pod_bonus'] == 0
    assert b1['overall_bonus'] == 0
    assert b1['match_play_points'] == 5

    # b3: last place, no bonuses at all
    b3 = by_id['b3']
    assert b3['match_play_points'] == 0
