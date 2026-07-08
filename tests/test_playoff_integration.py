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
from lib.playoffs import assign_seeds_4_team


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
