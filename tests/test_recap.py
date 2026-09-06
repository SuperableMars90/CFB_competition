"""
Unit tests for lib.recap's pure logic: _matchup_lookup,
build_overall_results, build_pod_vs_pod_summary, the "Unplayed Games"
classification (greedy_arrange_started_picks / classify_bench_team), and
the "Games by Pod & Ownership" classification (_played_by_pod /
classify_pod_play / find_played_matchups) -- all take pre-fetched data,
no DB access. The remaining functions in lib.recap (build_player_recap,
build_unplayed_games, build_pod_play_breakdown, build_scrappy_by_pod)
call the DB directly and are verified live instead, matching this repo's
existing convention for thin DB-glue functions.
"""

from lib.recap import (
    _matchup_lookup,
    _played_by_pod,
    _slot_capacities,
    _slot_margins_by_key,
    build_overall_results,
    build_pod_vs_pod_summary,
    classify_bench_team,
    classify_pod_play,
    compare_players_by_slot_points,
    compare_players_by_teams,
    find_played_matchups,
    greedy_arrange_started_picks,
)
from lib.optimal_lineup import OptimalLineupResult, TeamWeekResult


def test_matchup_lookup_both_directions_win_loss():
    matchups = [{'player_a_id': 1, 'player_a_name': 'Bob', 'player_b_id': 2, 'player_b_name': 'Dan',
                 'winner_id': 1, 'is_tie': False, 'matchup_type': 'cross_pod'}]
    lookup = _matchup_lookup(matchups)
    assert lookup[1] == {'opponent_id': 2, 'opponent_name': 'Dan', 'result': 'W', 'matchup_type': 'cross_pod'}
    assert lookup[2] == {'opponent_id': 1, 'opponent_name': 'Bob', 'result': 'L', 'matchup_type': 'cross_pod'}


def test_matchup_lookup_tie():
    matchups = [{'player_a_id': 1, 'player_a_name': 'Bob', 'player_b_id': 2, 'player_b_name': 'Dan',
                 'winner_id': None, 'is_tie': True, 'matchup_type': 'in_pod'}]
    lookup = _matchup_lookup(matchups)
    assert lookup[1]['result'] == 'T'
    assert lookup[2]['result'] == 'T'


def test_matchup_lookup_unresolved_is_none_not_a_guess():
    """If winner_id is somehow still NULL and is_tie is False (shouldn't
    happen post-finalize, but not assumed), result is None -- not
    silently guessed as a win or loss for either side."""
    matchups = [{'player_a_id': 1, 'player_a_name': 'Bob', 'player_b_id': 2, 'player_b_name': 'Dan',
                 'winner_id': None, 'is_tie': False, 'matchup_type': 'in_pod'}]
    lookup = _matchup_lookup(matchups)
    assert lookup[1]['result'] is None
    assert lookup[2]['result'] is None


def _standing(pid, name, total, match_pts=5):
    return {
        'player_id': pid, 'player_name': name, 'total_points': total,
        'pod_rank': 1, 'overall_rank': 1, 'match_play_points': match_pts,
        'base_points': match_pts, 'pod_bonus': 0, 'pod_vs_pod_bonus': 0, 'overall_bonus': 0,
    }


def test_overall_results_interleaves_scrappy_by_total_points():
    standings = [_standing(1, 'Bob', 100), _standing(2, 'Dan', 50)]
    scrappy = {
        'Orange Pod': OptimalLineupResult(total=75, picks=[]),  # between Bob and Dan
        'White Pod': OptimalLineupResult(total=10, picks=[]),   # below both
    }
    pod_of_player = {1: 11, 2: 22}
    pod_names = {11: 'Orange Pod', 22: 'White Pod'}

    rows = build_overall_results(standings, [], scrappy, pod_of_player, pod_names)

    names_in_order = [r['name'] for r in rows]
    assert names_in_order == ['Bob', 'Scrappy (Orange Pod)', 'Dan', 'Scrappy (White Pod)']


def test_overall_results_scrappy_rows_have_no_match_play_or_pvp_fields():
    scrappy = {'Orange Pod': OptimalLineupResult(total=42, picks=[])}
    rows = build_overall_results([], [], scrappy, {}, {})
    row = rows[0]
    assert row['kind'] == 'scrappy'
    assert row['total_points'] == 42
    for field in ('match_pts', 'base_pts', 'pod_bonus', 'vs_pod_bonus', 'overall_bonus', 'pvp_result', 'pvp_opponent', 'pod_rank', 'overall_rank'):
        assert row[field] is None


def test_overall_results_attaches_pvp_result_to_players():
    standings = [_standing(1, 'Bob', 100), _standing(2, 'Dan', 50)]
    matchups = [{'player_a_id': 1, 'player_a_name': 'Bob', 'player_b_id': 2, 'player_b_name': 'Dan',
                 'winner_id': 1, 'is_tie': False, 'matchup_type': 'in_pod'}]
    rows = build_overall_results(standings, matchups, {}, {1: 11, 2: 11}, {11: 'Orange Pod'})
    by_name = {r['name']: r for r in rows}
    assert by_name['Bob']['pvp_result'] == 'W'
    assert by_name['Bob']['pvp_opponent'] == 'Dan'
    assert by_name['Dan']['pvp_result'] == 'L'


# ------------------------------------------------------------------
# build_pod_vs_pod_summary
# ------------------------------------------------------------------

POD_A, POD_B = 11, 22
POD_OF_PLAYER = {1: POD_A, 4: POD_A, 2: POD_B, 3: POD_B}
POD_NAMES = {POD_A: 'Orange Pod', POD_B: 'White Pod'}


def _standing_total(pid, name, total):
    return {'player_id': pid, 'player_name': name, 'total_points': total}


def test_pod_vs_pod_none_for_single_pod():
    result = build_pod_vs_pod_summary([], {1: POD_A}, POD_NAMES, [])
    assert result is None


def test_pod_vs_pod_this_week_points_summed_per_pod():
    standings = [
        _standing_total(1, 'Dan', 100), _standing_total(4, 'Bob', 50),   # Pod A: 150
        _standing_total(2, 'Jacob', 30), _standing_total(3, 'Zach', 20),  # Pod B: 50
    ]
    result = build_pod_vs_pod_summary(standings, POD_OF_PLAYER, POD_NAMES, [])
    by_pod = {r['podName']: r['points'] for r in result['thisWeek']}
    assert by_pod == {'Orange Pod': 150, 'White Pod': 50}


def test_pod_vs_pod_series_record_from_stored_bonus_not_recomputed():
    # Weeks 1-3: Pod A(11) won weeks 1,2; Pod B(22) won week 3.
    season_results = [
        {'week': 1, 'pod_id': 11, 'won': 1}, {'week': 1, 'pod_id': 22, 'won': 0},
        {'week': 2, 'pod_id': 11, 'won': 1}, {'week': 2, 'pod_id': 22, 'won': 0},
        {'week': 3, 'pod_id': 11, 'won': 0}, {'week': 3, 'pod_id': 22, 'won': 1},
    ]
    result = build_pod_vs_pod_summary([], POD_OF_PLAYER, POD_NAMES, season_results)
    by_pod = {r['podName']: r['wins'] for r in result['series']}
    assert by_pod == {'Orange Pod': 2, 'White Pod': 1}
    assert result['ties'] == 0


def test_pod_vs_pod_series_counts_ties_separately():
    season_results = [
        {'week': 1, 'pod_id': 11, 'won': 0}, {'week': 1, 'pod_id': 22, 'won': 0},  # tie
    ]
    result = build_pod_vs_pod_summary([], POD_OF_PLAYER, POD_NAMES, season_results)
    assert result['ties'] == 1
    assert all(r['wins'] == 0 for r in result['series'])


def test_pod_vs_pod_skips_a_week_missing_one_pods_row():
    """Defensive: a week where only one pod has a row (shouldn't happen
    once a season's underway) isn't silently counted as a loss."""
    season_results = [{'week': 1, 'pod_id': 11, 'won': 1}]  # pod 22 missing entirely
    result = build_pod_vs_pod_summary([], POD_OF_PLAYER, POD_NAMES, season_results)
    assert result['ties'] == 0
    assert all(r['wins'] == 0 for r in result['series'])


# ------------------------------------------------------------------
# build_pvp_matchups_table
# ------------------------------------------------------------------

from lib.recap import build_pvp_matchups_table


def test_pvp_matchups_table_basic_win_loss():
    matchups = [{'matchup_type': 'cross_pod', 'player_a_id': 1, 'player_a_name': 'Bob',
                 'player_b_id': 2, 'player_b_name': 'Dan', 'winner_id': 1, 'is_tie': False}]
    standings = [_standing_total(1, 'Bob', 100), _standing_total(2, 'Dan', 80)]
    pod_of_player = {1: POD_A, 2: POD_B}
    rows = build_pvp_matchups_table(matchups, standings, pod_of_player, POD_NAMES)

    assert len(rows) == 1
    row = rows[0]
    assert row['aName'] == 'Bob' and row['aPoints'] == 100 and row['aResult'] == 'W'
    assert row['bName'] == 'Dan' and row['bPoints'] == 80 and row['bResult'] == 'L'
    assert row['aPodName'] == 'Orange Pod' and row['bPodName'] == 'White Pod'


def test_pvp_matchups_table_tie():
    matchups = [{'matchup_type': 'in_pod', 'player_a_id': 1, 'player_a_name': 'Bob',
                 'player_b_id': 4, 'player_b_name': 'Zach', 'winner_id': None, 'is_tie': True}]
    standings = [_standing_total(1, 'Bob', 50), _standing_total(4, 'Zach', 50)]
    rows = build_pvp_matchups_table(matchups, standings, POD_OF_PLAYER, POD_NAMES)
    assert rows[0]['aResult'] == 'T' and rows[0]['bResult'] == 'T'


def test_pvp_matchups_table_agrees_with_overall_results_pvp_fields():
    """The dedicated table and build_overall_results()'s per-player pvp
    fields must never disagree -- they share _resolve_matchup()."""
    matchups = [{'matchup_type': 'cross_pod', 'player_a_id': 1, 'player_a_name': 'Bob',
                 'player_b_id': 2, 'player_b_name': 'Dan', 'winner_id': 1, 'is_tie': False}]
    standings = [_standing(1, 'Bob', 100), _standing(2, 'Dan', 80)]
    overall = build_overall_results(standings, matchups, {}, {1: POD_A, 2: POD_B}, POD_NAMES)
    table = build_pvp_matchups_table(matchups, standings, {1: POD_A, 2: POD_B}, POD_NAMES)

    bob_overall = next(r for r in overall if r['name'] == 'Bob')
    assert bob_overall['pvp_result'] == table[0]['aResult']


# ------------------------------------------------------------------
# tally_pick_record
# ------------------------------------------------------------------

from lib.recap import tally_pick_record


def test_tally_pick_record_counts_wins_losses_ties_and_passes():
    slots = [
        {'is_pass': False, 'margin': 7},
        {'is_pass': False, 'margin': 3},
        {'is_pass': False, 'margin': -4},
        {'is_pass': False, 'margin': 0},
        {'is_pass': True, 'margin': 0},
    ]
    rec = tally_pick_record(slots)
    assert rec == {'w': 2, 'l': 1, 't': 1, 'pass': 1}


def test_tally_pick_record_skips_unresolved_margins():
    slots = [{'is_pass': False, 'margin': None}, {'is_pass': False, 'margin': 5}]
    rec = tally_pick_record(slots)
    assert rec == {'w': 1, 'l': 0, 't': 0, 'pass': 0}


def test_tally_pick_record_empty():
    assert tally_pick_record([]) == {'w': 0, 'l': 0, 't': 0, 'pass': 0}


# ------------------------------------------------------------------
# Unplayed Games -- greedy arrangement + redundant/wasted classification
# ------------------------------------------------------------------

_TIERS = {
    'SEC': 'P4', 'B1G': 'P4', 'ACC': 'P4', 'B12': 'P4',
    'AAC': 'G6', 'MWC': 'G6', 'SBC': 'G6', 'MAC': 'G6', 'CUSA': 'G6', 'PAC': 'G6',
}


def _t(team_id, conf, tier, margin):
    return TeamWeekResult(
        team_id=team_id, name=f'T{team_id}',
        conference_abbreviation=conf, tier=tier, margin=margin,
    )


def _ids(teams):
    return [t.team_id for t in teams]


def test_greedy_puts_best_team_in_its_conference_slot():
    arr = greedy_arrange_started_picks(
        [_t(1, 'SEC', 'P4', 10), _t(2, 'SEC', 'P4', 30), _t(3, 'B1G', 'P4', 5)], _TIERS,
    )
    assert _ids(arr['SEC']) == [2]          # highest SEC team, not the first seen
    assert _ids(arr['B1G']) == [3]
    assert _ids(arr['P4_FLEX']) == [1]      # the spare SEC team overflows to flex


def test_greedy_keeps_a_losing_native_pick_in_its_slot():
    # optimize_lineup() would drop a <=0 team; the arrangement must not --
    # the player really did use that slot.
    arr = greedy_arrange_started_picks([_t(1, 'SEC', 'P4', -5)], _TIERS)
    assert _ids(arr['SEC']) == [1]


def test_greedy_independent_falls_back_into_a_tier_conference_slot():
    arr = greedy_arrange_started_picks([_t(1, 'IND', 'P4', 9)], _TIERS)
    assert _ids(arr['SEC']) == [1]          # first P4 conference slot, no native available


def _full_p4_arrangement():
    started = [
        _t(1, 'SEC', 'P4', 30), _t(2, 'B1G', 'P4', 25), _t(3, 'ACC', 'P4', 20),
        _t(4, 'B12', 'P4', 8), _t(5, 'SEC', 'P4', 15), _t(6, 'B1G', 'P4', 12),
        _t(7, 'ACC', 'P4', 10), _t(8, 'SEC', 'P4', 18),
    ]
    return greedy_arrange_started_picks(started, _TIERS)


def test_classify_wasted_against_a_weaker_conference_incumbent():
    arr = _full_p4_arrangement()          # B12 slot holds T4 (+8); flex weakest +12; wc +10
    caps = _slot_capacities(_TIERS)
    verdict = classify_bench_team(_t(99, 'B12', 'P4', 14), arr, caps, _TIERS)
    assert verdict == {
        'kind': 'wasted', 'gain': 6,
        'replaced_category': 'B12', 'replaced_team': 'T4',
    }


def test_classify_wasted_against_an_open_slot_has_no_replaced_team():
    arr = _full_p4_arrangement()          # every G6 slot is empty
    caps = _slot_capacities(_TIERS)
    verdict = classify_bench_team(_t(99, 'AAC', 'G6', 21), arr, caps, _TIERS)
    assert verdict['kind'] == 'wasted'
    assert verdict['gain'] == 21
    assert verdict['replaced_team'] is None
    assert verdict['replaced_category'] == 'AAC'


def test_classify_redundant_when_every_eligible_slot_scored_more():
    arr = _full_p4_arrangement()
    caps = _slot_capacities(_TIERS)
    verdict = classify_bench_team(_t(98, 'B12', 'P4', 5), arr, caps, _TIERS)
    assert verdict == {'kind': 'redundant', 'score': 5}


def test_classify_exact_wash_is_shown_nowhere():
    arr = _full_p4_arrangement()          # B12 incumbent is exactly +8
    caps = _slot_capacities(_TIERS)
    assert classify_bench_team(_t(97, 'B12', 'P4', 8), arr, caps, _TIERS) is None


def test_classify_non_scoring_redundant_team_is_dropped():
    arr = _full_p4_arrangement()
    caps = _slot_capacities(_TIERS)
    assert classify_bench_team(_t(96, 'B12', 'P4', -2), arr, caps, _TIERS) is None


# ------------------------------------------------------------------
# Games by Pod & Ownership -- _played_by_pod / classify_pod_play /
# find_played_matchups
# ------------------------------------------------------------------

# pods 1 (Orange) / 2 (White); players 10,11 in Orange, 20,21 in White
_POD_OF = {10: 1, 11: 1, 20: 2, 21: 2}


def test_played_by_pod_groups_players_and_skips_the_podless():
    played = {10: {100, 200}, 11: {200}, 20: {100}, 99: {300}}  # 99 has no pod
    out = _played_by_pod(played, _POD_OF)
    assert out[1][200] == [10, 11]
    assert out[1][100] == [10]
    assert out[2][100] == [20]
    assert 300 not in {tid for teams in out.values() for tid in teams}


def test_classify_pod_play_five_buckets():
    played_by_pod = {
        1: {100: [10], 200: [10], 300: [11], 400: [10]},   # Orange started 100,200,300,400
        2: {100: [20], 500: [20]},                          # White started 100,500
    }
    owned_by_pod = {
        1: {100, 200, 300, 400, 500},   # Orange owns everything in play
        2: {100, 200, 500},             # White owns 100, 200, 500
    }
    buckets = classify_pod_play(played_by_pod, owned_by_pod, 1, 2)

    assert [e['team_id'] for e in buckets['both']] == [100]
    assert buckets['both'][0]['players_by_pod'] == {1: [10], 2: [20]}
    # 200: Orange-only, White owns it
    assert [e['team_id'] for e in buckets['owned_both_only_a']] == [200]
    # 300 & 400: Orange-only, White doesn't own
    assert [e['team_id'] for e in buckets['only_a_unowned_b']] == [300, 400]
    # 500: White-only, Orange owns it
    assert [e['team_id'] for e in buckets['owned_both_only_b']] == [500]
    assert buckets['only_b_unowned_a'] == []


def test_find_played_matchups_only_when_both_sides_started_and_deduped():
    played_by_pod = {
        1: {100: [10], 300: [11]},   # Orange started 100 and 300
        2: {200: [20]},              # White started 200
    }
    team_games = {
        100: {'game_id': 7, 'opponent_id': 200, 'opponent_name': 'T200', 'margin': 14},
        200: {'game_id': 7, 'opponent_id': 100, 'opponent_name': 'T100', 'margin': -14},
        300: {'game_id': 9, 'opponent_id': 999, 'opponent_name': 'T999', 'margin': 3},  # opp not started
    }
    rows = find_played_matchups(played_by_pod, team_games)
    assert len(rows) == 1
    row = rows[0]
    assert row['game_id'] == 7
    assert {row['team_id'], row['opponent_id']} == {100, 200}
    assert row['players_by_pod'] == {1: [10]}
    assert row['opp_players_by_pod'] == {2: [20]}


# ------------------------------------------------------------------
# Pick Similarity -- compare_players_by_teams
# ------------------------------------------------------------------

_SIM_NAMES = {1: 'Bob', 2: 'Dan', 3: 'Zach', 4: 'Jacob'}
_SIM_POD_OF = {1: 11, 3: 11, 2: 22, 4: 22}   # 1,3 Orange; 2,4 White
_SIM_POD_NAMES = {11: 'Orange Pod', 22: 'White Pod'}


def test_compare_by_teams_counts_shared_and_sums_abs_margin():
    lineups = {1: {100, 200, 300}, 2: {200, 300, 400}}
    team_margin = {100: 5, 200: 10, 300: -20, 400: 7}
    team_meta = {tid: {'name': f'T{tid}', 'conference': 'SEC'} for tid in team_margin}
    rows = compare_players_by_teams(
        lineups, team_margin, team_meta, _SIM_NAMES, {1: 11, 2: 22}, _SIM_POD_NAMES,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r['matchingTeams'] == 2
    # |margin| of the two shared teams: |10| + |-20| = 30 -- the loss
    # counts exactly as hard as a win of the same size.
    assert r['sharedImpact'] == 30
    # shared teams ordered by |margin| desc: T300 (20) before T200 (10)
    assert [t['teamId'] for t in r['sharedTeams']] == [300, 200]
    assert r['sharedTeams'][0]['margin'] == -20


def test_compare_by_teams_excludes_same_pod_pairs():
    lineups = {1: {100, 200}, 3: {100, 200}, 2: {100, 200}}
    team_margin = {100: 3, 200: 4}
    team_meta = {100: {'name': 'A', 'conference': 'SEC'}, 200: {'name': 'B', 'conference': 'ACC'}}
    rows = compare_players_by_teams(
        lineups, team_margin, team_meta, _SIM_NAMES, _SIM_POD_OF, _SIM_POD_NAMES,
    )
    pairs = {frozenset((r['aPlayerId'], r['bPlayerId'])) for r in rows}
    # 1&3 are both Orange -> not compared; only the two cross-pod pairs remain
    assert pairs == {frozenset((1, 2)), frozenset((3, 2))}


def test_compare_by_teams_empty_for_single_pod():
    lineups = {1: {100}, 3: {100}}
    rows = compare_players_by_teams(
        lineups, {100: 5}, {100: {'name': 'A', 'conference': 'SEC'}},
        _SIM_NAMES, {1: 11, 3: 11}, _SIM_POD_NAMES,
    )
    assert rows == []


def test_compare_by_teams_unscored_shared_team_counts_but_adds_no_impact():
    lineups = {1: {100, 200}, 2: {100, 200}}
    team_margin = {100: 12, 200: None}   # 200's game has no score yet
    team_meta = {100: {'name': 'A', 'conference': 'SEC'}, 200: {'name': 'B', 'conference': 'ACC'}}
    rows = compare_players_by_teams(
        lineups, team_margin, team_meta, _SIM_NAMES, {1: 11, 2: 22}, _SIM_POD_NAMES,
    )
    assert rows[0]['matchingTeams'] == 2
    assert rows[0]['sharedImpact'] == 12


# ------------------------------------------------------------------
# Pick Similarity -- _slot_margins_by_key / compare_players_by_slot_points
# ------------------------------------------------------------------

def _sl(slot_type, label, margin):
    return {'slot_type': slot_type, 'label': label, 'margin': margin}


def test_slot_margins_by_key_buckets_conference_and_flex():
    slots = [
        _sl('conference', 'SEC', 10),
        _sl('conference', 'MAC', -3),
        _sl('p4_flex', 'P4 Flex', 5),
        _sl('p4_flex', 'P4 Flex', 8),
        _sl('g6_flex', 'G6 Flex', 1),
        _sl('wildcard', 'WC', -4),
    ]
    out = _slot_margins_by_key(slots)
    assert out['SEC'] == [10]
    assert out['MAC'] == [-3]
    assert sorted(out['P4 Flex']) == [5, 8]
    assert out['G6 Flex'] == [1]
    assert out['Wild Card'] == [-4]


def test_slot_margins_by_key_treats_pass_and_bye_as_zero():
    out = _slot_margins_by_key([_sl('conference', 'SEC', None), _sl('wildcard', 'WC', 0)])
    assert out['SEC'] == [0]
    assert out['Wild Card'] == [0]


def test_compare_by_slot_points_pairs_conference_by_conference():
    a = [_sl('conference', 'SEC', 10), _sl('conference', 'ACC', 3)]
    b = [_sl('conference', 'SEC', -4), _sl('conference', 'ACC', 3)]
    rows = compare_players_by_slot_points({1: a, 2: b}, _SIM_NAMES, {1: 11, 2: 22}, _SIM_POD_NAMES)
    r = rows[0]
    # SEC: |10 - (-4)| = 14 ; ACC: |3 - 3| = 0
    assert r['slotPointDistance'] == 14
    # ACC both +3 -> same direction; SEC +10 vs -4 -> not
    assert r['sameDirectionSlots'] == 1
    assert r['slotsCompared'] == 2


def test_compare_by_slot_points_flex_ranked_best_to_best():
    a = [_sl('p4_flex', 'P4 Flex', 30), _sl('p4_flex', 'P4 Flex', 5), _sl('p4_flex', 'P4 Flex', 1)]
    b = [_sl('p4_flex', 'P4 Flex', 28), _sl('p4_flex', 'P4 Flex', 20), _sl('p4_flex', 'P4 Flex', 0)]
    rows = compare_players_by_slot_points({1: a, 2: b}, _SIM_NAMES, {1: 11, 2: 22}, _SIM_POD_NAMES)
    # pairs after sorting each side desc: (30,28), (5,20), (1,0) -> 2 + 15 + 1
    assert rows[0]['slotPointDistance'] == 18


def test_compare_by_slot_points_same_direction_counts_both_negative():
    a = [_sl('conference', 'SEC', -10), _sl('conference', 'ACC', 6)]
    b = [_sl('conference', 'SEC', -3), _sl('conference', 'ACC', -6)]
    rows = compare_players_by_slot_points({1: a, 2: b}, _SIM_NAMES, {1: 11, 2: 22}, _SIM_POD_NAMES)
    # SEC both negative -> counts; ACC +6 vs -6 -> does not
    assert rows[0]['sameDirectionSlots'] == 1


def test_compare_by_slot_points_includes_same_pod_pairs_and_sorts_by_distance():
    identical = [_sl('conference', 'SEC', 7)]
    far = [_sl('conference', 'SEC', -20)]
    rows = compare_players_by_slot_points(
        {1: identical, 3: identical, 2: far},
        _SIM_NAMES, _SIM_POD_OF, _SIM_POD_NAMES,
    )
    # 1 & 3 are same-pod but still compared (subsection 2 is all pairs)
    assert frozenset((rows[0]['aPlayerId'], rows[0]['bPlayerId'])) == frozenset((1, 3))
    assert rows[0]['slotPointDistance'] == 0
