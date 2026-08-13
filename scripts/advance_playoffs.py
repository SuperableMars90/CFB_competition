"""
scripts/advance_playoffs.py
-----------------------------
Run once per real playoff week. For the season's FINAL playoff week
specifically, first resolves that week's exhibition pairing(s) among
already-eliminated idle players. Then, for every playoff_games row
scheduled this week whose sides are both resolved and undecided:
scores the week (reusing scripts.scoring_engine's fetch/score
pipeline), decides the game, writes the result, and propagates any
now-resolvable later-round placeholder rows via lib.bracket_engine. If
this was the bracket's final round and it's now fully decided, writes
final placements into season_standings.

Usage:
    PYTHONPATH=. python scripts/advance_playoffs.py --season 2026 --week 12
"""

import argparse
from collections import defaultdict

from lib.db import get_connection, update_game_results
from lib.scoring import load_week_picks, score_picks, compute_player_totals
from lib.metagame import compute_weekly_standings, write_weekly_results
from lib.seeding import get_pvp_regular_season_records, MatchupRecord
from lib.bracket_engine import GameResult, playable_games, resolve_participant
from lib.playoffs import (
    BRACKET_SPECS,
    PLAYOFF_ROUND_COUNT,
    bracket_format_for_league_shape,
    compute_final_placements,
    resolve_playoff_game,
    find_exhibition_pairing,
    UnresolvedPlayoffTieError,
)
from scripts.scoring_engine import _get_season_year, _fetch_final_scores
from scripts.cfbd_client import CFBDClient


def get_season_id(year):
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM seasons WHERE year = %s", (year,))
        row = cur.fetchone()
        cur.close()
    return row['id'] if row else None


def get_scoring_context(season_id, name=None):
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        if name:
            cur.execute(
                "SELECT id, name FROM scoring_contexts WHERE season_id = %s AND name = %s",
                (season_id, name),
            )
        else:
            cur.execute(
                "SELECT id, name FROM scoring_contexts WHERE season_id = %s AND is_active = 1 LIMIT 1",
                (season_id,),
            )
        row = cur.fetchone()
        cur.close()
    return row


def get_pvp_cutoff_week(season_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pvp_regular_season_end_week FROM seasons WHERE id = %s", (season_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def get_pods_for_season(season_id):
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM pods WHERE season_id = %s ORDER BY id", (season_id,))
        rows = cur.fetchall()
        cur.close()
    return rows


def get_pod_of_player(season_id):
    """{player_id: pod_id} for every player with a pod assignment this
    season. Pod membership is per-season (pod_memberships), not a
    permanent players.pod_id."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_id, pod_id FROM pod_memberships WHERE season_id = %s",
            (season_id,),
        )
        rows = cur.fetchall()
        cur.close()
    return {player_id: pod_id for player_id, pod_id in rows}


def get_player_names(player_ids):
    if not player_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(player_ids))
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(f"SELECT id, name FROM players WHERE id IN ({placeholders})", tuple(player_ids))
        rows = cur.fetchall()
        cur.close()
    return {r['id']: r['name'] for r in rows}


def get_playoff_games_for_week(season_id, scoring_context_id, week):
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, game_type, game_number, player_a_id, player_b_id, winner_id, is_tie
            FROM playoff_games
            WHERE season_id = %s AND scoring_context_id = %s AND week = %s
            """,
            (season_id, scoring_context_id, week),
        )
        rows = cur.fetchall()
        cur.close()
    return rows


def get_decided_bracket_results(season_id, scoring_context_id, bracket_format):
    """{game_number: GameResult} for every decided bracket game so far."""
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT game_number, winner_id, loser_id, decided_by
            FROM playoff_games
            WHERE season_id = %s AND scoring_context_id = %s AND bracket_format = %s
              AND game_type = 'bracket' AND winner_id IS NOT NULL
            """,
            (season_id, scoring_context_id, bracket_format),
        )
        rows = cur.fetchall()
        cur.close()
    return {
        r['game_number']: GameResult(
            winner_id=r['winner_id'], loser_id=r['loser_id'], decided_by=r['decided_by'] or 'result',
        )
        for r in rows
    }


def get_seed_to_player(season_id, scoring_context_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT playoff_seed, player_id FROM season_standings "
            "WHERE season_id = %s AND scoring_context_id = %s AND playoff_seed IS NOT NULL",
            (season_id, scoring_context_id),
        )
        rows = cur.fetchall()
        cur.close()
    return {seed: player_id for seed, player_id in rows}


def get_idle_players_for_final_week(season_id, scoring_context_id, week, pod_of_player):
    """Pod players not already assigned to a bracket game this week."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT player_a_id, player_b_id FROM playoff_games "
            "WHERE season_id = %s AND scoring_context_id = %s AND week = %s AND game_type = 'bracket'",
            (season_id, scoring_context_id, week),
        )
        rows = cur.fetchall()
        cur.close()

    assigned = set()
    for a, b in rows:
        if a is None or b is None:
            raise ValueError(
                f"bracket game at week {week} has an unresolved side -- "
                "run propagate_bracket (or the prior week's advance) first"
            )
        assigned.add(a)
        assigned.add(b)
    return [pid for pid in pod_of_player if pid not in assigned]


def get_combined_matchup_records(season_id, scoring_context_id, cutoff_week, bracket_format):
    """
    Regular-season MatchupRecords plus a synthetic record for every
    already-decided playoff bracket game, so the exhibition tiebreak
    cascade isn't blind to a meeting that happened in an earlier
    playoff round (e.g. eight_team's Game 3).
    """
    records = get_pvp_regular_season_records(season_id, scoring_context_id, cutoff_week)
    combined = defaultdict(list, {k: list(v) for k, v in records.items()})

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT week, player_a_id, player_b_id, winner_id, is_tie
            FROM playoff_games
            WHERE season_id = %s AND scoring_context_id = %s AND bracket_format = %s
              AND game_type = 'bracket' AND winner_id IS NOT NULL
            """,
            (season_id, scoring_context_id, bracket_format),
        )
        rows = cur.fetchall()
        cur.close()

    for row in rows:
        a, b = row['player_a_id'], row['player_b_id']
        if row['is_tie']:
            ra, rb = 'T', 'T'
        elif row['winner_id'] == a:
            ra, rb = 'W', 'L'
        else:
            ra, rb = 'L', 'W'
        # margin=0 placeholder -- find_exhibition_pairing's tiebreak cascade
        # never reads .margin, only .week/.opponent_id/.result.
        combined[a].append(MatchupRecord(week=row['week'], opponent_id=b, result=ra, margin=0))
        combined[b].append(MatchupRecord(week=row['week'], opponent_id=a, result=rb, margin=0))
    return dict(combined)


def assign_exhibition_participants(season_id, scoring_context_id, week, pod_of_player, cutoff_week, bracket_format):
    """
    Resolves this week's exhibition playoff_games rows' participants.
    Idempotent -- skips if already assigned. Returns the number of
    exhibition games assigned.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, player_a_id FROM playoff_games "
            "WHERE season_id = %s AND scoring_context_id = %s AND week = %s AND game_type = 'exhibition' "
            "ORDER BY game_number",
            (season_id, scoring_context_id, week),
        )
        exhibition_rows = cur.fetchall()
        cur.close()

    if not exhibition_rows:
        return 0
    if all(r['player_a_id'] is not None for r in exhibition_rows):
        return 0  # already assigned -- idempotent rerun

    idle_players = get_idle_players_for_final_week(season_id, scoring_context_id, week, pod_of_player)
    records = get_combined_matchup_records(season_id, scoring_context_id, cutoff_week, bracket_format)
    pairs = find_exhibition_pairing(idle_players, records, pod_of_player, current_week=week)
    canonical_pairs = sorted(tuple(sorted(p)) for p in pairs)

    with get_connection() as conn:
        cur = conn.cursor()
        for row, (a, b) in zip(exhibition_rows, canonical_pairs):
            cur.execute(
                "UPDATE playoff_games SET player_a_id = %s, player_b_id = %s WHERE id = %s",
                (a, b, row['id']),
            )
        conn.commit()
        cur.close()
    return len(canonical_pairs)


def fetch_week_player_totals(season_id, week):
    """The only CFBD/IO-touching function -- mirrors finalize_week()'s
    first half in scripts/scoring_engine.py."""
    client = CFBDClient()
    year = _get_season_year(season_id)
    final_scores = _fetch_final_scores(client, year, week)
    update_game_results(final_scores)

    picks = load_week_picks(season_id, week)
    scored = score_picks(picks, final_scores)
    return compute_player_totals(scored)


def write_weekly_results_for_week(season_id, week, player_totals):
    """Written unconditionally every week, including playoff weeks --
    read by build_season_leaderboard() and (eventually) the
    total-points-standing column."""
    standings = compute_weekly_standings(season_id, week, player_totals)
    write_weekly_results(standings)


def write_game_result(game_id, result: GameResult):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE playoff_games SET winner_id = %s, loser_id = %s, is_tie = FALSE, decided_by = %s "
            "WHERE id = %s",
            (result.winner_id, result.loser_id, result.decided_by, game_id),
        )
        conn.commit()
        cur.close()


def write_exhibition_result(game_id, player_totals, a_id, b_id):
    """Exhibition games decide by plain total-points comparison and
    allow a tie -- no elimination stakes, so resolve_playoff_game()'s
    forced pick-record tiebreak is the wrong tool here."""
    a_total = player_totals[a_id]['total']
    b_total = player_totals[b_id]['total']
    with get_connection() as conn:
        cur = conn.cursor()
        if a_total == b_total:
            cur.execute(
                "UPDATE playoff_games SET is_tie = TRUE, decided_by = 'total_points' WHERE id = %s",
                (game_id,),
            )
        else:
            winner_id, loser_id = (a_id, b_id) if a_total > b_total else (b_id, a_id)
            cur.execute(
                "UPDATE playoff_games SET winner_id = %s, loser_id = %s, is_tie = FALSE, "
                "decided_by = 'total_points' WHERE id = %s",
                (winner_id, loser_id, game_id),
            )
        conn.commit()
        cur.close()


def resolve_playable_games(season_id, scoring_context_id, week, player_totals):
    """
    For each undecided game this week with both sides resolved: bracket
    games go through resolve_playoff_game() (an UnresolvedPlayoffTieError
    prints a warning and leaves it undecided -- no override tool yet);
    exhibition games decide by plain total-points comparison, allowing
    a tie. Returns the list of games decided this call.
    """
    games = get_playoff_games_for_week(season_id, scoring_context_id, week)
    decided = []
    for g in games:
        if g['winner_id'] is not None or g['is_tie']:
            continue
        a_id, b_id = g['player_a_id'], g['player_b_id']
        if a_id is None or b_id is None:
            continue
        if a_id not in player_totals or b_id not in player_totals:
            print(f"[WARNING] Game {g['game_number']}: no lineup submitted this week for "
                  f"player {a_id} or {b_id} -- skipping.")
            continue

        if g['game_type'] == 'bracket':
            try:
                result = resolve_playoff_game(player_totals, a_id, b_id)
            except UnresolvedPlayoffTieError as e:
                print(f"[WARNING] Unresolved playoff tie in game {g['game_number']}: "
                      f"player {e.player_a_id} vs player {e.player_b_id}. No override tool yet -- "
                      "leaving undecided; rerun this script after a manual resolution exists.")
                continue
            write_game_result(g['id'], result)
        else:
            write_exhibition_result(g['id'], player_totals, a_id, b_id)
        decided.append(g)
    return decided


def propagate_bracket(season_id, scoring_context_id, bracket_format):
    """
    Re-evaluates playable_games() against the whole spec and however
    many results exist so far (format- and round-agnostic on purpose --
    a game whose both dependencies resolved this same week gets filled
    immediately, not just "next week's round"). Returns the number of
    placeholder rows resolved.
    """
    spec = BRACKET_SPECS[bracket_format]
    seed_to_player = get_seed_to_player(season_id, scoring_context_id)
    results = get_decided_bracket_results(season_id, scoring_context_id, bracket_format)

    updated = 0
    with get_connection() as conn:
        cur = conn.cursor()
        for game in playable_games(spec, seed_to_player, results):
            a = resolve_participant(game.side_a, seed_to_player, results)
            b = resolve_participant(game.side_b, seed_to_player, results)
            # No "IS NULL" guard here on purpose: a game can have one side
            # already resolved (e.g. a seed-sourced side, filled in by
            # seed_playoffs.py) while the other is still NULL (e.g.
            # winner_of/loser_of) -- playable_games() already guarantees
            # we only reach here for games not yet decided, so writing
            # both resolved sides again is safe and idempotent.
            cur.execute(
                "UPDATE playoff_games SET player_a_id = %s, player_b_id = %s "
                "WHERE season_id = %s AND scoring_context_id = %s AND game_number = %s",
                (a, b, season_id, scoring_context_id, game.game_number),
            )
            updated += cur.rowcount
        conn.commit()
        cur.close()
    return updated


def maybe_finalize_placements(season_id, scoring_context_id, bracket_format):
    """If the bracket is now fully decided, writes final placements into
    season_standings.playoff_placement and returns them, else None."""
    seed_to_player = get_seed_to_player(season_id, scoring_context_id)
    results = get_decided_bracket_results(season_id, scoring_context_id, bracket_format)
    placements = compute_final_placements(bracket_format, seed_to_player, results)
    if placements is None:
        return None

    with get_connection() as conn:
        cur = conn.cursor()
        for place, player_id in placements.items():
            cur.execute(
                "UPDATE season_standings SET playoff_placement = %s "
                "WHERE season_id = %s AND scoring_context_id = %s AND player_id = %s",
                (place, season_id, scoring_context_id, player_id),
            )
        conn.commit()
        cur.close()
    return placements


def main():
    parser = argparse.ArgumentParser(
        description='Advance the playoff bracket for one real playoff week.'
    )
    parser.add_argument('--season', type=int, required=True, help='Season year (e.g. 2026)')
    parser.add_argument('--scoring-context', type=str, default='',
                        help='Scoring context name; defaults to the season\'s active context')
    parser.add_argument('--week', type=int, required=True, help='Real calendar week to advance')
    args = parser.parse_args()

    season_id = get_season_id(args.season)
    if season_id is None:
        print(f"ERROR: no season found for year={args.season}")
        return

    ctx = get_scoring_context(season_id, args.scoring_context or None)
    if ctx is None:
        print(f"ERROR: no matching scoring context for season {args.season}")
        return

    cutoff_week = get_pvp_cutoff_week(season_id)
    if cutoff_week is None:
        print(f"ERROR: seasons.pvp_regular_season_end_week isn't set for season {args.season}. "
              "Run scripts/generate_pvp_schedule.py first.")
        return

    pods = get_pods_for_season(season_id)
    pod_of_player = get_pod_of_player(season_id)
    try:
        bracket_format = bracket_format_for_league_shape(len(pods), len(pod_of_player))
    except ValueError as e:
        print(f"ERROR: {e} (season {args.season})")
        return

    final_week = cutoff_week + PLAYOFF_ROUND_COUNT[bracket_format]

    if args.week == final_week:
        n = assign_exhibition_participants(
            season_id, ctx['id'], args.week, pod_of_player, cutoff_week, bracket_format,
        )
        if n:
            print(f"Assigned {n} exhibition participant pair(s) for the final week.")

    player_totals = fetch_week_player_totals(season_id, args.week)
    write_weekly_results_for_week(season_id, args.week, player_totals)

    decided = resolve_playable_games(season_id, ctx['id'], args.week, player_totals)
    print(f"Decided {len(decided)} game(s) this week.")

    n_propagated = propagate_bracket(season_id, ctx['id'], bracket_format)
    if n_propagated:
        print(f"Resolved {n_propagated} later-round placeholder game(s).")

    placements = maybe_finalize_placements(season_id, ctx['id'], bracket_format)
    if placements:
        names = get_player_names(list(placements.values()))
        print("\nFinal placements:")
        for place in sorted(placements):
            print(f"  {place}. {names[placements[place]]}")


if __name__ == '__main__':
    main()
