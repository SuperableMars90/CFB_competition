"""
Unit tests for lib.recap's pure logic: _matchup_lookup,
build_overall_results, and build_pod_vs_pod_summary (all take
pre-fetched data as arguments, no DB access). The other functions in
lib.recap (build_player_recap, build_top_unplayed_winners,
build_top_free_agents, build_scrappy_by_pod) call the DB directly and
are verified live instead, matching this repo's existing convention for
thin DB-glue functions.
"""

from lib.recap import _matchup_lookup, build_overall_results, build_pod_vs_pod_summary
from lib.optimal_lineup import OptimalLineupResult


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
