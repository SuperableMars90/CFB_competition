"""
Integration test for the full PVP season pipeline: initiation -> seed
population -> weekly playoff advancement -> final placements.

This is the piece that actually matters for this feature -- every
individual function involved (lib.scheduling, lib.seeding,
lib.bracket_engine, lib.playoffs) is already unit-tested in isolation;
the risk here is *sequencing* across scripts/generate_pvp_schedule.py,
scripts/seed_playoffs.py, and scripts/advance_playoffs.py.

Uses a fully synthetic, disposable season (year=9999) created and torn
down entirely within this test -- naturally isolated from the real
season 8/2026 since every row hangs off the synthetic season_id.
"""

import pytest

from lib.db import get_connection
from lib.scheduling import build_pvp_schedule
from lib.playoffs import BRACKET_SPECS, UnresolvedPlayoffTieError

from scripts.generate_pvp_schedule import (
    write_schedule,
    set_pvp_cutoff,
    write_bracket_placeholders,
    write_exhibition_placeholders,
)
from scripts.seed_playoffs import (
    get_pvp_cutoff_week,
    resolve_seed_placeholders,
    write_season_standings_seeding,
)
from scripts.advance_playoffs import (
    assign_exhibition_participants,
    resolve_playable_games,
    propagate_bracket,
    maybe_finalize_placements,
    get_seed_to_player,
)
from lib.seeding import get_pvp_regular_season_records, compute_seeding
from lib.playoffs import assign_seeds_4_team, assign_seeds_6_team
from lib.metagame import compute_weekly_standings, write_weekly_results


SYNTHETIC_YEAR = 9999
REGULAR_SEASON_WEEKS = 4  # last_week=6, four_team playoffs need 2 rounds -> 6-2=4


@pytest.fixture
def synthetic_season():
    """
    Creates a disposable season/pod/4 players/scoring context, yields
    identifying info, and tears everything down afterward regardless of
    test outcome.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO seasons (year, first_week, last_week) VALUES (%s, 1, 6)",
            (SYNTHETIC_YEAR,),
        )
        season_id = cur.lastrowid

        cur.execute("INSERT INTO pods (season_id, name) VALUES (%s, 'Synthetic Pod')", (season_id,))
        pod_id = cur.lastrowid

        player_ids = []
        for i, name in enumerate(['SynthP1', 'SynthP2', 'SynthP3', 'SynthP4']):
            cur.execute(
                "INSERT INTO players (name, username, password_hash) VALUES (%s, %s, 'x')",
                (name, f'synth_{SYNTHETIC_YEAR}_{i}'),
            )
            player_id = cur.lastrowid
            cur.execute(
                "INSERT INTO pod_memberships (season_id, pod_id, player_id) VALUES (%s, %s, %s)",
                (season_id, pod_id, player_id),
            )
            player_ids.append(player_id)

        cur.execute(
            "INSERT INTO scoring_contexts (season_id, name, scoring_type, is_active) "
            "VALUES (%s, 'synthetic_ctx', 'matchup', 1)",
            (season_id,),
        )
        scoring_context_id = cur.lastrowid

        conn.commit()
        cur.close()

    p1, p2, p3, p4 = player_ids
    yield {
        'season_id': season_id,
        'pod_id': pod_id,
        'scoring_context_id': scoring_context_id,
        'p1': p1, 'p2': p2, 'p3': p3, 'p4': p4,
    }

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM playoff_games WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM season_standings WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM weekly_results WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM matchup_pairings WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM manual_tiebreaks WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM pod_memberships WHERE pod_id = %s", (pod_id,))
        placeholders = ",".join(["%s"] * len(player_ids))
        cur.execute(f"DELETE FROM players WHERE id IN ({placeholders})", tuple(player_ids))
        cur.execute("DELETE FROM pods WHERE id = %s", (pod_id,))
        cur.execute("DELETE FROM scoring_contexts WHERE id = %s", (scoring_context_id,))
        cur.execute("DELETE FROM seasons WHERE id = %s", (season_id,))
        conn.commit()
        cur.close()


def _write_weekly_result(season_id, scoring_context_id, player_id, week, total_points):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO weekly_results (player_id, season_id, scoring_context_id, week, total_points) "
            "VALUES (%s, %s, %s, %s, %s)",
            (player_id, season_id, scoring_context_id, week, total_points),
        )
        conn.commit()
        cur.close()


def _set_matchup_winner(season_id, scoring_context_id, week, player_a_id, player_b_id, winner_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matchup_pairings SET winner_id = %s "
            "WHERE season_id = %s AND scoring_context_id = %s AND week = %s "
            "AND player_a_id = %s AND player_b_id = %s",
            (winner_id, season_id, scoring_context_id, week, player_a_id, player_b_id),
        )
        assert cur.rowcount == 1, (
            f"expected exactly one matchup_pairings row for week={week} "
            f"a={player_a_id} b={player_b_id}, got {cur.rowcount}"
        )
        conn.commit()
        cur.close()


SIX_TEAM_SYNTHETIC_YEAR = 9997


@pytest.fixture
def synthetic_six_team_season():
    """
    Disposable season with 2 pods of 3 players (the 3-player-pod PVP
    format), yields identifying info, tears everything down afterward.
    Shared by the schedule-only test, the match_play_pod standings test,
    and the full six_team pipeline test below -- each gets its own fresh
    instance (function-scoped fixture), so mutating the shared
    scoring_context row in place (see the standings test) is safe.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO seasons (year, first_week, last_week) VALUES (%s, 1, 13)",
            (SIX_TEAM_SYNTHETIC_YEAR,),
        )
        season_id = cur.lastrowid

        pod_ids = []
        player_ids = []
        pod_of_player = {}
        for pod_name, prefix in [('Synthetic Pod A', 'A'), ('Synthetic Pod B', 'B')]:
            cur.execute("INSERT INTO pods (season_id, name) VALUES (%s, %s)", (season_id, pod_name))
            pod_id = cur.lastrowid
            pod_ids.append(pod_id)
            for i in range(3):
                cur.execute(
                    "INSERT INTO players (name, username, password_hash) VALUES (%s, %s, 'x')",
                    (f'Synth6{prefix}{i}', f'synth6_{SIX_TEAM_SYNTHETIC_YEAR}_{prefix}_{i}'),
                )
                player_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO pod_memberships (season_id, pod_id, player_id) VALUES (%s, %s, %s)",
                    (season_id, pod_id, player_id),
                )
                player_ids.append(player_id)
                pod_of_player[player_id] = pod_id

        cur.execute(
            "INSERT INTO scoring_contexts (season_id, name, scoring_type, is_active) "
            "VALUES (%s, 'synthetic_six_team_ctx', 'matchup', 1)",
            (season_id,),
        )
        scoring_context_id = cur.lastrowid

        conn.commit()
        cur.close()

    yield {
        'season_id': season_id, 'pod_ids': pod_ids,
        'scoring_context_id': scoring_context_id,
        'player_ids': player_ids, 'pod_of_player': pod_of_player,
    }

    with get_connection() as conn:
        cur = conn.cursor()
        # playoff_games/season_standings/weekly_results FK-reference players,
        # so they must go before the players delete below -- only the PVP-
        # schedule-only tests populate matchup_pairings, but the full
        # pipeline test (and the standings test) populate these too.
        cur.execute("DELETE FROM playoff_games WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM season_standings WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM weekly_results WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM matchup_pairings WHERE season_id = %s", (season_id,))
        cur.execute("DELETE FROM pod_memberships WHERE season_id = %s", (season_id,))
        placeholders = ",".join(["%s"] * len(player_ids))
        cur.execute(f"DELETE FROM players WHERE id IN ({placeholders})", tuple(player_ids))
        cur.execute("DELETE FROM pods WHERE id IN (%s, %s)", tuple(pod_ids))
        cur.execute("DELETE FROM scoring_contexts WHERE id = %s", (scoring_context_id,))
        cur.execute("DELETE FROM seasons WHERE id = %s", (season_id,))
        conn.commit()
        cur.close()


@pytest.mark.integration
def test_six_team_pvp_schedule_writes_correct_matchup_types(synthetic_six_team_season):
    """
    Mirrors what scripts/generate_pvp_schedule.py's 'six_team' branch
    does: flat double round robin across all 6 players (both pods
    combined), matchup_type derived from pod_of_player -- confirms the
    real DB write path (matchup_pairings' ENUM + unique key) handles it
    correctly, not just the pure lib.scheduling logic (already unit
    tested in tests/test_scheduling.py).
    """
    season_id = synthetic_six_team_season['season_id']
    ctx_id = synthetic_six_team_season['scoring_context_id']
    player_ids = synthetic_six_team_season['player_ids']
    pod_of_player = synthetic_six_team_season['pod_of_player']

    schedule = build_pvp_schedule(
        single_pod_players=player_ids,
        pod_of_player=pod_of_player,
        regular_season_weeks=10,
        single_pod_repeats=2,
    )
    with get_connection() as conn:
        written = write_schedule(conn, season_id, ctx_id, schedule)
        set_pvp_cutoff(conn, season_id, 10)
    assert written == 30  # C(6,2)=15 pairs x 2 meetings

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT week, matchup_type, player_a_id, player_b_id FROM matchup_pairings "
            "WHERE season_id = %s AND scoring_context_id = %s",
            (season_id, ctx_id),
        )
        rows = cur.fetchall()
        cur.close()

    assert len(rows) == 30
    for row in rows:
        same_pod = pod_of_player[row['player_a_id']] == pod_of_player[row['player_b_id']]
        assert row['matchup_type'] == ('in_pod' if same_pod else 'cross_pod')

    in_pod_count = sum(1 for r in rows if r['matchup_type'] == 'in_pod')
    cross_pod_count = sum(1 for r in rows if r['matchup_type'] == 'cross_pod')
    assert in_pod_count == 12    # 2 pods x C(3,2)=3 pairs x 2 meetings
    assert cross_pod_count == 18  # 3x3=9 cross pairs x 2 meetings

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pvp_regular_season_end_week FROM seasons WHERE id = %s", (season_id,))
        assert cur.fetchone()[0] == 10
        cur.close()


@pytest.mark.integration
def test_compute_weekly_standings_writes_match_play_pod_results_for_six_players(synthetic_six_team_season):
    """
    Live verification of the DB-writing wrapper (compute_weekly_standings
    -> write_weekly_results) for a real match_play_pod scoring context
    with 6 players. compute_pod_standings() itself is already unit tested
    (tests/test_metagame.py) with the confirmed [5,4,3,2,1,0] ladder; this
    confirms get_scoring_context()'s real points_map JSON round-trip and
    weekly_results' 4 match_play_pod-only columns both work end-to-end for
    this format too, not just the 8-player case already exercised live in
    production. Reuses the fixture's own scoring_context row (mutated in
    place to match_play_pod) rather than inserting a second one, since
    get_scoring_context() picks the season's *active* context with no
    tiebreak -- two active contexts for the same season would make which
    one gets read nondeterministic.
    """
    season_id = synthetic_six_team_season['season_id']
    ctx_id = synthetic_six_team_season['scoring_context_id']
    pod_of_player = synthetic_six_team_season['pod_of_player']
    pa1, pa2, pa3, pb1, pb2, pb3 = synthetic_six_team_season['player_ids']

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE scoring_contexts SET scoring_type = 'match_play_pod', "
            "points_map = '[5,4,3,2,1,0]' WHERE id = %s",
            (ctx_id,),
        )
        conn.commit()
        cur.close()

    # Same shape as tests/test_metagame.py's 8-player fixtures: pod A total
    # (90) beats pod B total (75), pa1 sweeps overall/pod/pod-vs-pod bonuses.
    player_totals = {
        pa1: {'player_name': 'PA1', 'total': 50}, pa2: {'player_name': 'PA2', 'total': 30},
        pa3: {'player_name': 'PA3', 'total': 10}, pb1: {'player_name': 'PB1', 'total': 45},
        pb2: {'player_name': 'PB2', 'total': 25}, pb3: {'player_name': 'PB3', 'total': 5},
    }
    standings = compute_weekly_standings(season_id, week=1, player_totals=player_totals,
                                          pod_of_player=pod_of_player)
    by_id = {r['player_id']: r for r in standings}
    assert by_id[pa1]['base_points'] == 5
    assert by_id[pa1]['match_play_points'] == 8  # 5 base + pod + pod_vs_pod + overall bonus
    assert by_id[pb3]['match_play_points'] == 0

    try:
        write_weekly_results(standings)

        with get_connection() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT player_id, base_points, pod_bonus, pod_vs_pod_bonus, overall_bonus, "
                "match_play_points FROM weekly_results WHERE season_id = %s AND scoring_context_id = %s",
                (season_id, ctx_id),
            )
            rows = {r['player_id']: r for r in cur.fetchall()}
            cur.close()

        assert len(rows) == 6
        assert rows[pa1] == {
            'player_id': pa1, 'base_points': 5, 'pod_bonus': 1,
            'pod_vs_pod_bonus': 1, 'overall_bonus': 1, 'match_play_points': 8,
        }
        assert rows[pb3]['match_play_points'] == 0
    finally:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM weekly_results WHERE season_id = %s AND scoring_context_id = %s",
                (season_id, ctx_id),
            )
            conn.commit()
            cur.close()


@pytest.mark.integration
def test_full_six_team_pipeline_end_to_end(synthetic_six_team_season):
    """
    Mirrors test_full_four_team_pipeline_end_to_end below, but for the
    six_team format (2 pods of 3): confirms the same sequencing --
    schedule -> seed -> playoff advancement -> final placements -- also
    holds for the newer bracket shape. Pod A (pa1/pa2/pa3) is engineered
    to sweep overall ranks 1-3, so this also exercises the pod-seeding
    "bump" rule (seed 2 must come from pod B, not overall #2) live,
    through the real DB-backed pipeline rather than just the pure
    assign_seeds_6_team unit tests in tests/test_playoffs.py.
    """
    season_id = synthetic_six_team_season['season_id']
    ctx_id = synthetic_six_team_season['scoring_context_id']
    pod_of_player = synthetic_six_team_season['pod_of_player']
    # First 3 player_ids are pod A (created first in the fixture), last 3 are pod B.
    pa1, pa2, pa3, pb1, pb2, pb3 = synthetic_six_team_season['player_ids']
    regular_season_weeks = 5  # 1x round robin over 6 players fits exactly, no byes

    # ---- Step 1: regular season (mirrors generate_pvp_schedule.py's six_team branch) ----
    schedule = build_pvp_schedule(
        single_pod_players=[pa1, pa2, pa3, pb1, pb2, pb3],
        pod_of_player=pod_of_player,
        regular_season_weeks=regular_season_weeks,
        single_pod_repeats=1,
    )
    with get_connection() as conn:
        write_schedule(conn, season_id, ctx_id, schedule)
        set_pvp_cutoff(conn, season_id, regular_season_weeks)
        write_bracket_placeholders(conn, season_id, ctx_id, 'six_team', regular_season_weeks)
        write_exhibition_placeholders(conn, season_id, ctx_id, 'six_team', regular_season_weeks)

    # Circle-method schedule for [pa1,pa2,pa3,pb1,pb2,pb3] (5 rounds, no bye);
    # engineer a strict dominance order pa1>pa2>pa3>pb1>pb2>pb3 (winner is
    # always whichever player is earlier in that order) -- a clean, tie-free
    # PVP record, same approach as the four_team test below.
    games = [
        (1, pa1, pb3, 100, 50, pa1), (1, pa2, pb2, 100, 50, pa2), (1, pa3, pb1, 100, 50, pa3),
        (2, pa1, pb2, 100, 50, pa1), (2, pb3, pb1, 50, 100, pb1), (2, pa2, pa3, 100, 50, pa2),
        (3, pa1, pb1, 100, 50, pa1), (3, pb2, pa3, 50, 100, pa3), (3, pb3, pa2, 50, 100, pa2),
        (4, pa1, pa3, 100, 50, pa1), (4, pb1, pa2, 50, 100, pa2), (4, pb2, pb3, 100, 50, pb2),
        (5, pa1, pa2, 100, 50, pa1), (5, pa3, pb3, 100, 50, pa3), (5, pb1, pb2, 100, 50, pb1),
    ]
    for week, a, b, a_pts, b_pts, winner in games:
        _write_weekly_result(season_id, ctx_id, a, week, a_pts)
        _write_weekly_result(season_id, ctx_id, b, week, b_pts)
        _set_matchup_winner(season_id, ctx_id, week, a, b, winner)

    # ---- Step 2: seed the playoffs (mirrors scripts/seed_playoffs.py) ----
    cutoff_week = get_pvp_cutoff_week(season_id)
    assert cutoff_week == regular_season_weeks

    records = get_pvp_regular_season_records(season_id, ctx_id, cutoff_week)
    seeding = compute_seeding(records, manual_tiebreaks={})
    assert [row['player_id'] for row in seeding] == [pa1, pa2, pa3, pb1, pb2, pb3]

    seed_to_player = assign_seeds_6_team(seeding, pod_of_player)
    # Pod A sweeps overall 1-3 -> seed 2 is bumped to pod B's own best (pb1),
    # not overall #2 (pa2). Exactly the "bump" scenario, live.
    assert seed_to_player == {1: pa1, 2: pb1, 3: pa2, 4: pa3, 5: pb2, 6: pb3}

    write_season_standings_seeding(season_id, ctx_id, seeding, seed_to_player)
    n_resolved = resolve_seed_placeholders(season_id, ctx_id, 'six_team', seed_to_player)
    assert n_resolved == 4  # POG1, POG2 (both sides) + POG4, POG5 (seed side only)

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT game_number, player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND bracket_format='six_team' "
            "ORDER BY game_number",
            (season_id, ctx_id),
        )
        rows = {r['game_number']: r for r in cur.fetchall()}
        cur.close()
    assert rows[1]['player_a_id'] == pa2 and rows[1]['player_b_id'] == pb3   # seed3 v seed6
    assert rows[2]['player_a_id'] == pa3 and rows[2]['player_b_id'] == pb2   # seed4 v seed5
    assert rows[4]['player_a_id'] == pa1 and rows[4]['player_b_id'] is None  # seed1, winner of G2 TBD

    # ---- Step 3: playoff week 1 (round 1) -- mirrors scripts/advance_playoffs.py ----
    week1 = regular_season_weeks + 1
    player_totals_wk1 = {
        pa2: {'total': 100, 'slots': []}, pb3: {'total': 50, 'slots': []},   # G1: pa2 beats pb3
        pa3: {'total': 100, 'slots': []}, pb2: {'total': 50, 'slots': []},   # G2: pa3 beats pb2
    }
    decided = resolve_playable_games(season_id, ctx_id, week1, player_totals_wk1)
    assert len(decided) == 2  # G1, G2 (both playable, seed-sourced on both sides)

    n_propagated = propagate_bracket(season_id, ctx_id, 'six_team')
    assert n_propagated == 3  # G3 (both loser_of sides), G4/G5's remaining winner_of side

    # ---- Step 4: playoff week 2 (round 2) ----
    week2 = regular_season_weeks + 2
    player_totals_wk2 = {
        pb3: {'total': 50, 'slots': []}, pb2: {'total': 100, 'slots': []},   # G3: pb2 beats pb3 -> 5th/6th
        pa1: {'total': 100, 'slots': []}, pa3: {'total': 50, 'slots': []},   # G4: pa1 beats pa3
        pb1: {'total': 100, 'slots': []}, pa2: {'total': 50, 'slots': []},   # G5: pb1 beats pa2
    }
    decided_wk2 = resolve_playable_games(season_id, ctx_id, week2, player_totals_wk2)
    assert len(decided_wk2) == 3  # G3, G4, G5

    n_propagated_wk2 = propagate_bracket(season_id, ctx_id, 'six_team')
    assert n_propagated_wk2 == 2  # G6, G7 both now playable

    placements_mid = maybe_finalize_placements(season_id, ctx_id, 'six_team')
    assert placements_mid is None  # 5th/6th known (via G3), but 1-4 aren't yet

    # ---- Step 5: playoff week 3 (final round + exhibition) ----
    week3 = regular_season_weeks + 3
    n_exhibition = assign_exhibition_participants(
        season_id, ctx_id, week3, pod_of_player, cutoff_week, 'six_team',
    )
    assert n_exhibition == 1
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_type='exhibition'",
            (season_id, ctx_id),
        )
        exhibition_row = cur.fetchone()
        cur.close()
    assert {exhibition_row['player_a_id'], exhibition_row['player_b_id']} == {pb2, pb3}

    player_totals_wk3 = {
        pa3: {'total': 50, 'slots': []}, pa2: {'total': 100, 'slots': []},   # G6: pa2 beats pa3 -> 3rd/4th
        pa1: {'total': 100, 'slots': []}, pb1: {'total': 50, 'slots': []},   # G7: pa1 beats pb1 -> champion
        pb2: {'total': 100, 'slots': []}, pb3: {'total': 50, 'slots': []},   # exhibition
    }
    decided_final = resolve_playable_games(season_id, ctx_id, week3, player_totals_wk3)
    assert len(decided_final) == 3  # G6, G7, exhibition

    n_propagated_final = propagate_bracket(season_id, ctx_id, 'six_team')
    assert n_propagated_final == 0  # nothing left to propagate

    placements = maybe_finalize_placements(season_id, ctx_id, 'six_team')
    assert placements == {1: pa1, 2: pb1, 3: pa2, 4: pa3, 5: pb2, 6: pb3}

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT player_id, playoff_placement FROM season_standings "
            "WHERE season_id=%s AND scoring_context_id=%s",
            (season_id, ctx_id),
        )
        standings = {r['player_id']: r['playoff_placement'] for r in cur.fetchall()}
        cur.close()
    assert standings == {pa1: 1, pb1: 2, pa2: 3, pa3: 4, pb2: 5, pb3: 6}


@pytest.mark.integration
def test_full_four_team_pipeline_end_to_end(synthetic_season):
    season_id = synthetic_season['season_id']
    ctx_id = synthetic_season['scoring_context_id']
    p1, p2, p3, p4 = (synthetic_season[k] for k in ('p1', 'p2', 'p3', 'p4'))

    # ---- Step 1: regular season (mirrors scripts/generate_pvp_schedule.py) ----
    schedule = build_pvp_schedule(
        single_pod_players=[p1, p2, p3, p4],
        regular_season_weeks=REGULAR_SEASON_WEEKS,
        single_pod_repeats=1,  # 3 rounds + 1 bye fits in 4 weeks
    )
    with get_connection() as conn:
        write_schedule(conn, season_id, ctx_id, schedule)
        set_pvp_cutoff(conn, season_id, REGULAR_SEASON_WEEKS)
        write_bracket_placeholders(conn, season_id, ctx_id, 'four_team', REGULAR_SEASON_WEEKS)
        write_exhibition_placeholders(conn, season_id, ctx_id, 'four_team', REGULAR_SEASON_WEEKS)

    # Circle-method schedule for [p1,p2,p3,p4] with 1 bye (week 2):
    #   week1: (p1,p4), (p2,p3)   week3: (p1,p3), (p4,p2)   week4: (p1,p2), (p3,p4)
    # Engineer p1: 3-0, p2: 2-1, p3: 1-2, p4: 0-3 -- a clean, tie-free record.
    games = [
        (1, p1, p4, 100, 50, p1),   # p1 beats p4
        (1, p2, p3, 100, 50, p2),   # p2 beats p3
        (3, p1, p3, 100, 50, p1),   # p1 beats p3
        (3, p4, p2, 50, 100, p2),   # p2 beats p4
        (4, p1, p2, 100, 50, p1),   # p1 beats p2
        (4, p3, p4, 100, 50, p3),   # p3 beats p4
    ]
    for week, a, b, a_pts, b_pts, winner in games:
        _write_weekly_result(season_id, ctx_id, a, week, a_pts)
        _write_weekly_result(season_id, ctx_id, b, week, b_pts)
        _set_matchup_winner(season_id, ctx_id, week, a, b, winner)

    # ---- Step 2: seed the playoffs (mirrors scripts/seed_playoffs.py) ----
    cutoff_week = get_pvp_cutoff_week(season_id)
    assert cutoff_week == REGULAR_SEASON_WEEKS

    records = get_pvp_regular_season_records(season_id, ctx_id, cutoff_week)
    seeding = compute_seeding(records, manual_tiebreaks={})
    assert [row['player_id'] for row in seeding] == [p1, p2, p3, p4]

    seed_to_player = assign_seeds_4_team(seeding)
    assert seed_to_player == {1: p1, 2: p2, 3: p3, 4: p4}

    write_season_standings_seeding(season_id, ctx_id, seeding, seed_to_player)
    n_resolved = resolve_seed_placeholders(season_id, ctx_id, 'four_team', seed_to_player)
    assert n_resolved == 2  # both G1 sides resolve immediately; G2 only its seed(1) side

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT game_number, player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND bracket_format='four_team' "
            "ORDER BY game_number",
            (season_id, ctx_id),
        )
        rows = {r['game_number']: r for r in cur.fetchall()}
        cur.close()
    assert rows[1]['player_a_id'] == p2 and rows[1]['player_b_id'] == p3
    assert rows[2]['player_a_id'] == p1 and rows[2]['player_b_id'] is None  # winner of G1 not decided yet

    # ---- Step 3: playoff week 1 (round 1) -- mirrors scripts/advance_playoffs.py ----
    week5 = REGULAR_SEASON_WEEKS + 1
    player_totals_week5 = {
        p2: {'total': 100, 'slots': []},
        p3: {'total': 50, 'slots': []},
    }
    decided = resolve_playable_games(season_id, ctx_id, week5, player_totals_week5)
    assert len(decided) == 1  # only G1 was playable/resolvable this week

    n_propagated = propagate_bracket(season_id, ctx_id, 'four_team')
    assert n_propagated == 1  # G2's winner_of(1) side now resolves

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT winner_id, loser_id, loser_place FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_number=1",
            (season_id, ctx_id),
        )
        g1 = cur.fetchone()
        cur.execute(
            "SELECT player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_number=2",
            (season_id, ctx_id),
        )
        g2 = cur.fetchone()
        cur.close()
    assert g1['winner_id'] == p2 and g1['loser_id'] == p3
    assert g2['player_a_id'] == p1 and g2['player_b_id'] == p2

    placements_mid = maybe_finalize_placements(season_id, ctx_id, 'four_team')
    assert placements_mid is None  # bracket not fully decided yet (G2 still open)

    # ---- Step 4: playoff week 2 (final round + exhibition) ----
    week6 = REGULAR_SEASON_WEEKS + 2
    pod_of_player = {p1: synthetic_season['pod_id'], p2: synthetic_season['pod_id'],
                      p3: synthetic_season['pod_id'], p4: synthetic_season['pod_id']}
    n_exhibition = assign_exhibition_participants(
        season_id, ctx_id, week6, pod_of_player, cutoff_week, 'four_team',
    )
    assert n_exhibition == 1
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_type='exhibition'",
            (season_id, ctx_id),
        )
        exhibition_row = cur.fetchone()
        cur.close()
    assert {exhibition_row['player_a_id'], exhibition_row['player_b_id']} == {p3, p4}

    player_totals_week6 = {
        p1: {'total': 100, 'slots': []},
        p2: {'total': 50, 'slots': []},
        p3: {'total': 100, 'slots': []},
        p4: {'total': 50, 'slots': []},
    }
    decided_final = resolve_playable_games(season_id, ctx_id, week6, player_totals_week6)
    assert len(decided_final) == 2  # G2 (championship) + the exhibition game

    n_propagated_final = propagate_bracket(season_id, ctx_id, 'four_team')
    assert n_propagated_final == 0  # nothing left to propagate

    placements = maybe_finalize_placements(season_id, ctx_id, 'four_team')
    assert placements == {1: p1, 2: p2, 3: p3, 4: p4}

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT player_id, playoff_placement FROM season_standings "
            "WHERE season_id=%s AND scoring_context_id=%s",
            (season_id, ctx_id),
        )
        standings = {r['player_id']: r['playoff_placement'] for r in cur.fetchall()}
        cur.close()
    assert standings == {p1: 1, p2: 2, p3: 3, p4: 4}


@pytest.mark.integration
def test_unresolved_playoff_tie_leaves_game_undecided_and_does_not_break_propagation(synthetic_season):
    season_id = synthetic_season['season_id']
    ctx_id = synthetic_season['scoring_context_id']
    p1, p2, p3, p4 = (synthetic_season[k] for k in ('p1', 'p2', 'p3', 'p4'))

    schedule = build_pvp_schedule(
        single_pod_players=[p1, p2, p3, p4],
        regular_season_weeks=REGULAR_SEASON_WEEKS,
        single_pod_repeats=1,
    )
    with get_connection() as conn:
        write_schedule(conn, season_id, ctx_id, schedule)
        set_pvp_cutoff(conn, season_id, REGULAR_SEASON_WEEKS)
        write_bracket_placeholders(conn, season_id, ctx_id, 'four_team', REGULAR_SEASON_WEEKS)
        write_exhibition_placeholders(conn, season_id, ctx_id, 'four_team', REGULAR_SEASON_WEEKS)

    games = [
        (1, p1, p4, 100, 50, p1), (1, p2, p3, 100, 50, p2),
        (3, p1, p3, 100, 50, p1), (3, p4, p2, 50, 100, p2),
        (4, p1, p2, 100, 50, p1), (4, p3, p4, 100, 50, p3),
    ]
    for week, a, b, a_pts, b_pts, winner in games:
        _write_weekly_result(season_id, ctx_id, a, week, a_pts)
        _write_weekly_result(season_id, ctx_id, b, week, b_pts)
        _set_matchup_winner(season_id, ctx_id, week, a, b, winner)

    cutoff_week = get_pvp_cutoff_week(season_id)
    records = get_pvp_regular_season_records(season_id, ctx_id, cutoff_week)
    seeding = compute_seeding(records, manual_tiebreaks={})
    seed_to_player = assign_seeds_4_team(seeding)
    write_season_standings_seeding(season_id, ctx_id, seeding, seed_to_player)
    resolve_seed_placeholders(season_id, ctx_id, 'four_team', seed_to_player)

    # G1 = seed2(p2) vs seed3(p3). Tie both total points AND pick-record.
    week5 = REGULAR_SEASON_WEEKS + 1
    tied_totals = {
        p2: {'total': 75, 'slots': [{'is_pass': False, 'margin': 10}]},
        p3: {'total': 75, 'slots': [{'is_pass': False, 'margin': 10}]},
    }
    decided = resolve_playable_games(season_id, ctx_id, week5, tied_totals)
    assert decided == []  # UnresolvedPlayoffTieError caught internally, left undecided

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT winner_id, is_tie FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_number=1",
            (season_id, ctx_id),
        )
        g1 = cur.fetchone()
        cur.close()
    assert g1['winner_id'] is None
    assert not g1['is_tie']

    # propagate_bracket must not crash, and G2's winner_of(1) side stays unresolved.
    n_propagated = propagate_bracket(season_id, ctx_id, 'four_team')
    assert n_propagated == 0
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id=%s AND scoring_context_id=%s AND game_number=2",
            (season_id, ctx_id),
        )
        g2 = cur.fetchone()
        cur.close()
    assert g2['player_a_id'] is not None  # seed(1) side, resolved by seed_playoffs
    assert g2['player_b_id'] is None      # winner_of(1), still blocked on the tie
