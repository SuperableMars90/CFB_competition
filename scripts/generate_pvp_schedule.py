"""
scripts/generate_pvp_schedule.py
---------------------------------
Prepares a full PVP season: schedules the regular-season round robin,
reserving enough weeks at the end for the playoffs, and writes it into
matchup_pairings; then writes TBD placeholder rows for every playoff
bracket game and exhibition game into playoff_games (participants left
NULL until scripts/seed_playoffs.py and scripts/advance_playoffs.py
resolve them). Auto-detects single-pod vs. two-pod from how many pods
exist for the season -- 3+ pods are not supported (hard error).

Single pod (four_team playoffs): everyone plays everyone
--single-pod-repeats times (3). Two pods (eight_team playoffs, equal
size): everyone plays their podmates --two-pod-in-pod-repeats times
(2), and every opposite-pod player once. See lib/scheduling.py for the
round-robin generator and lib/playoffs.py for the bracket specs.

The regular-season week count is auto-computed as
seasons.last_week - playoff_round_count (2 for four_team, 3 for
eight_team) unless --weeks overrides it -- this is what actually
reserves the right number of weeks for the playoffs given the real
season's length, which varies year to year.

Safe to re-run: all writes use ON DUPLICATE KEY UPDATE against their
respective unique keys, so re-running against an already-scheduled
season is a no-op rather than a duplicate.

Usage:
    # Dry run -- preview without writing
    PYTHONPATH=. python scripts/generate_pvp_schedule.py --season 2026 --dry-run

    # Live run (uses the season's active scoring context by default)
    PYTHONPATH=. python scripts/generate_pvp_schedule.py --season 2026

    # Explicit scoring context override
    PYTHONPATH=. python scripts/generate_pvp_schedule.py --season 2026 --scoring-context standard_4player
"""

import argparse
import sys

from db.connection import get_connection
from lib.scheduling import build_pvp_schedule
from lib.bracket_engine import Participant
from lib.playoffs import (
    BRACKET_SPECS,
    PLAYOFF_ROUND_COUNT,
    EXHIBITION_GAME_COUNT,
    bracket_format_for_pod_count,
)


def get_season_id(conn, year):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM seasons WHERE year = %s", (year,))
    row = cursor.fetchone()
    cursor.close()
    return row['id'] if row else None


def get_pods(conn, season_id):
    """Returns [{id, name}, ...] ordered by id."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, name FROM pods WHERE season_id = %s ORDER BY id", (season_id,))
    rows = cursor.fetchall()
    cursor.close()
    return rows


def get_pod_players(conn, pod_id):
    """Returns [{id, name}, ...] ordered by name. Pod membership is
    per-season (pod_memberships), not a permanent players.pod_id."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT pl.id, pl.name FROM players pl
        JOIN pod_memberships pm ON pm.player_id = pl.id
        WHERE pm.pod_id = %s ORDER BY pl.name
        """,
        (pod_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def get_player_names(conn, player_ids):
    """Returns {player_id: name} for the given ids."""
    if not player_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(player_ids))
    cursor = conn.cursor(dictionary=True)
    cursor.execute(f"SELECT id, name FROM players WHERE id IN ({placeholders})", tuple(player_ids))
    rows = cursor.fetchall()
    cursor.close()
    return {r['id']: r['name'] for r in rows}


def get_active_scoring_context(conn, season_id):
    """Returns {id, name} for the season's active scoring context, or None."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, name FROM scoring_contexts WHERE season_id = %s AND is_active = 1 LIMIT 1",
        (season_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def get_scoring_context_by_name(conn, season_id, name):
    """Returns {id, name} for the named scoring context in this season, or None."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, name FROM scoring_contexts WHERE season_id = %s AND name = %s",
        (season_id, name),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def existing_pairing_count(conn, season_id, scoring_context_id):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM matchup_pairings WHERE season_id = %s AND scoring_context_id = %s",
        (season_id, scoring_context_id),
    )
    count = cursor.fetchone()[0]
    cursor.close()
    return count


def write_schedule(conn, season_id, scoring_context_id, schedule):
    """
    Inserts matchup_pairings rows for every game in `schedule`
    ({week: [(player_a_id, player_b_id, matchup_type), ...]}).
    Safe to re-run (ON DUPLICATE KEY UPDATE against uq_matchup_pairing).
    Returns the number of games written.
    """
    cursor = conn.cursor()
    count = 0
    for week, games in schedule.items():
        for player_a_id, player_b_id, matchup_type in games:
            cursor.execute(
                """
                INSERT INTO matchup_pairings
                    (scoring_context_id, season_id, week, matchup_type, player_a_id, player_b_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """,
                (scoring_context_id, season_id, week, matchup_type, player_a_id, player_b_id),
            )
            count += 1
    conn.commit()
    cursor.close()
    return count


def set_pvp_cutoff(conn, season_id, last_pvp_week):
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE seasons SET pvp_regular_season_end_week = %s WHERE id = %s",
        (last_pvp_week, season_id),
    )
    conn.commit()
    cursor.close()


def get_season_last_week(conn, season_id):
    cursor = conn.cursor()
    cursor.execute("SELECT last_week FROM seasons WHERE id = %s", (season_id,))
    row = cursor.fetchone()
    cursor.close()
    return row[0]


def _encode_participant(p: Participant) -> tuple[str, int | None]:
    """(source_type, source_ref) for a playoff_games row, matching the
    ENUM('seed','winner_of','loser_of') + nullable ref columns."""
    if p.seed is not None:
        return ('seed', p.seed)
    return (f"{p.from_result}_of", p.from_game)


def write_bracket_placeholders(conn, season_id, scoring_context_id, bracket_format, regular_season_weeks):
    """
    Writes one TBD playoff_games row per lib.playoffs GameSpec slot in
    this bracket_format (game_type='bracket'). Participants are left
    NULL -- resolved later by scripts/seed_playoffs.py (seed-sourced
    sides) and scripts/advance_playoffs.py (winner/loser-sourced sides).
    Safe to re-run (ON DUPLICATE KEY UPDATE against uq_playoff_game).
    Returns the number of rows written.
    """
    cursor = conn.cursor()
    count = 0
    for game in BRACKET_SPECS[bracket_format]:
        side_a_type, side_a_ref = _encode_participant(game.side_a)
        side_b_type, side_b_ref = _encode_participant(game.side_b)
        week = regular_season_weeks + game.round
        cursor.execute(
            """
            INSERT INTO playoff_games
                (season_id, scoring_context_id, bracket_format, game_type,
                 game_number, round, week,
                 side_a_source_type, side_a_source_ref,
                 side_b_source_type, side_b_source_ref,
                 winner_place, loser_place)
            VALUES (%s, %s, %s, 'bracket', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
            """,
            (
                season_id, scoring_context_id, bracket_format,
                game.game_number, game.round, week,
                side_a_type, side_a_ref, side_b_type, side_b_ref,
                game.winner_place, game.loser_place,
            ),
        )
        count += 1
    conn.commit()
    cursor.close()
    return count


def write_exhibition_placeholders(conn, season_id, scoring_context_id, bracket_format, regular_season_weeks):
    """
    Writes the fixed number of TBD exhibition playoff_games rows for the
    final playoff week (1 for four_team, 2 for eight_team -- this count
    is fixed by bracket shape, not data-dependent; see
    lib.playoffs.find_exhibition_pairing's docstring). game_number
    continues past the bracket spec's own numbers. Participants are
    resolved dynamically later by scripts/advance_playoffs.py, not from
    a fixed seed/winner/loser reference. Safe to re-run. Returns the
    number of rows written.
    """
    final_round = PLAYOFF_ROUND_COUNT[bracket_format]
    week = regular_season_weeks + final_round
    max_game_number = max(g.game_number for g in BRACKET_SPECS[bracket_format])

    cursor = conn.cursor()
    count = 0
    for i in range(EXHIBITION_GAME_COUNT[bracket_format]):
        game_number = max_game_number + 1 + i
        cursor.execute(
            """
            INSERT INTO playoff_games
                (season_id, scoring_context_id, bracket_format, game_type,
                 game_number, round, week,
                 side_a_source_type, side_a_source_ref,
                 side_b_source_type, side_b_source_ref)
            VALUES (%s, %s, %s, 'exhibition', %s, %s, %s, 'exhibition', NULL, 'exhibition', NULL)
            ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
            """,
            (season_id, scoring_context_id, bracket_format, game_number, final_round, week),
        )
        count += 1
    conn.commit()
    cursor.close()
    return count


def print_dry_run(schedule, names, weeks, single_pod_repeats, two_pod_in_pod_repeats, scenario):
    print("\n" + "=" * 60)
    print("DRY RUN — nothing written to database")
    print("=" * 60)
    print(f"Scenario: {scenario}")
    print(f"Weeks: 1-{weeks}\n")

    game_counts = {}
    bye_counts = {}
    for week in sorted(schedule):
        games = schedule[week]
        if not games:
            print(f"  Week {week:>2}: (bye)")
            continue
        parts = []
        for a, b, matchup_type in games:
            tag = 'x' if matchup_type == 'cross_pod' else ''
            parts.append(f"{names.get(a, a)} vs {names.get(b, b)}{tag}")
            game_counts[a] = game_counts.get(a, 0) + 1
            game_counts[b] = game_counts.get(b, 0) + 1
        print(f"  Week {week:>2}: " + " | ".join(parts))

    for pid in names:
        bye_counts[pid] = weeks - game_counts.get(pid, 0)

    print("\nPer-player summary (games / byes):")
    for pid, name in sorted(names.items(), key=lambda kv: kv[1]):
        print(f"  {name:<15} {game_counts.get(pid, 0):>2} games, {bye_counts[pid]:>2} bye week(s)")
    print("\n('x' marks a cross-pod game)")


def _describe_source(source_type, source_ref):
    if source_type == 'seed':
        return f"Seed {source_ref}"
    if source_type == 'winner_of':
        return f"Winner G{source_ref}"
    if source_type == 'loser_of':
        return f"Loser G{source_ref}"
    return "TBD (exhibition)"


def print_playoff_dry_run(bracket_format, regular_season_weeks, playoff_rounds):
    print("\n" + "-" * 60)
    print(f"Playoff bracket: {bracket_format}")
    print("-" * 60)
    for game in BRACKET_SPECS[bracket_format]:
        week = regular_season_weeks + game.round
        side_a = _describe_source(*_encode_participant(game.side_a))
        side_b = _describe_source(*_encode_participant(game.side_b))
        places = ""
        if game.winner_place or game.loser_place:
            places = f"  (-> {game.winner_place or '?'}/{game.loser_place or '?'})"
        print(f"  Week {week:>2}  Game {game.game_number}: {side_a} vs {side_b}{places}")

    final_week = regular_season_weeks + playoff_rounds
    n_exhibition = EXHIBITION_GAME_COUNT[bracket_format]
    print(f"  Week {final_week:>2}  {n_exhibition} exhibition game(s) for already-eliminated "
          "players (participants TBD, resolved the week they're played)")


def main():
    parser = argparse.ArgumentParser(
        description='Generate the PVP round-robin schedule for a season and write it to matchup_pairings.'
    )
    parser.add_argument('--season', type=int, required=True, help='Season year (e.g. 2026)')
    parser.add_argument('--scoring-context', type=str, default='',
                        help='Scoring context name; defaults to the season\'s active context')
    parser.add_argument('--season-weeks', type=int, default=None,
                        help='Total metagame season length in weeks (e.g. 13). Recommended to always '
                             'pass this explicitly -- seasons.last_week reflects the full real CFB '
                             'schedule, which can include weeks outside the metagame\'s scope (e.g. '
                             'the Army-Navy game week, which trails conference championships every '
                             'year and is never part of PVP/playoffs). Falls back to seasons.last_week '
                             'if omitted, which may be wrong for exactly that reason.')
    parser.add_argument('--weeks', type=int, default=None,
                        help='Override the auto-computed regular-season week count directly, '
                             'bypassing --season-weeks entirely (default: --season-weeks or '
                             'seasons.last_week, minus the playoff round count)')
    parser.add_argument('--single-pod-repeats', type=int, default=3,
                        help='Times each pair plays in single-pod mode (default 3)')
    parser.add_argument('--two-pod-in-pod-repeats', type=int, default=2,
                        help='Times each in-pod pair plays in two-pod mode (default 2)')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    conn = get_connection()

    season_id = get_season_id(conn, args.season)
    if season_id is None:
        print(f"ERROR: no season found for year={args.season}")
        conn.close()
        sys.exit(1)

    # A scoring context is only required for a live write (matchup_pairings.
    # scoring_context_id must match what write_weekly_results() uses -- see
    # module docstring). --dry-run doesn't write anything, so a missing
    # context shouldn't block previewing the schedule shape.
    if args.scoring_context:
        ctx = get_scoring_context_by_name(conn, season_id, args.scoring_context)
        if ctx is None and not args.dry_run:
            print(f"ERROR: no scoring context named '{args.scoring_context}' for season {args.season}")
            conn.close()
            sys.exit(1)
    else:
        ctx = get_active_scoring_context(conn, season_id)
        if ctx is None and not args.dry_run:
            print(f"ERROR: no active scoring context for season {args.season}. "
                  "Seed scoring_contexts first, or pass --scoring-context explicitly.")
            conn.close()
            sys.exit(1)
        elif ctx is not None:
            print(f"Using season's active scoring context: '{ctx['name']}' (id={ctx['id']})")
    if ctx is None and args.dry_run:
        print(f"Note: no scoring context resolved for season {args.season} -- fine for --dry-run, "
              "but required before a live write.")

    pods = get_pods(conn, season_id)
    try:
        bracket_format = bracket_format_for_pod_count(len(pods))
    except ValueError:
        print(f"ERROR: {len(pods)} pods found for season {args.season}; "
              "this generator only supports 1 or 2 pods.")
        conn.close()
        sys.exit(1)

    playoff_rounds = PLAYOFF_ROUND_COUNT[bracket_format]
    if args.weeks is not None:
        regular_season_weeks = args.weeks
    else:
        if args.season_weeks is not None:
            season_weeks = args.season_weeks
        else:
            season_weeks = get_season_last_week(conn, season_id)
            print(f"Note: --season-weeks not given, falling back to seasons.last_week={season_weeks}. "
                  "Pass --season-weeks explicitly if that column includes weeks outside the "
                  "metagame's scope (e.g. Army-Navy week).")
        regular_season_weeks = season_weeks - playoff_rounds
        print(f"Regular-season weeks auto-computed: season_weeks={season_weeks} - "
              f"playoff_rounds={playoff_rounds} = {regular_season_weeks}")

    if len(pods) == 1:
        players = get_pod_players(conn, pods[0]['id'])
        player_ids = [p['id'] for p in players]
        names = {p['id']: p['name'] for p in players}
        schedule = build_pvp_schedule(
            single_pod_players=player_ids,
            regular_season_weeks=regular_season_weeks,
            single_pod_repeats=args.single_pod_repeats,
        )
        scenario = f"single pod ({len(player_ids)} players, {args.single_pod_repeats}x round robin)"
    else:
        pod_a_players = get_pod_players(conn, pods[0]['id'])
        pod_b_players = get_pod_players(conn, pods[1]['id'])
        pod_a_ids = [p['id'] for p in pod_a_players]
        pod_b_ids = [p['id'] for p in pod_b_players]
        names = {p['id']: p['name'] for p in pod_a_players + pod_b_players}
        schedule = build_pvp_schedule(
            pod_a=pod_a_ids,
            pod_b=pod_b_ids,
            regular_season_weeks=regular_season_weeks,
            two_pod_in_pod_repeats=args.two_pod_in_pod_repeats,
        )
        scenario = (
            f"two pods ('{pods[0]['name']}': {len(pod_a_ids)} players, "
            f"'{pods[1]['name']}': {len(pod_b_ids)} players)"
        )

    if args.dry_run:
        print_dry_run(schedule, names, regular_season_weeks, args.single_pod_repeats,
                      args.two_pod_in_pod_repeats, scenario)
        print_playoff_dry_run(bracket_format, regular_season_weeks, playoff_rounds)
        conn.close()
        return

    existing = existing_pairing_count(conn, season_id, ctx['id'])
    if existing:
        print(f"Note: {existing} matchup_pairings row(s) already exist for this season/context "
              "-- re-running is safe (duplicates are skipped via ON DUPLICATE KEY UPDATE).")

    written = write_schedule(conn, season_id, ctx['id'], schedule)
    print(f"Wrote {written} matchup_pairings row(s).")

    set_pvp_cutoff(conn, season_id, regular_season_weeks)
    print(f"Set seasons.pvp_regular_season_end_week = {regular_season_weeks} for season_id={season_id}.")

    n_bracket = write_bracket_placeholders(conn, season_id, ctx['id'], bracket_format, regular_season_weeks)
    print(f"Wrote {n_bracket} playoff bracket placeholder game(s) ({bracket_format}).")

    n_exhibition = write_exhibition_placeholders(conn, season_id, ctx['id'], bracket_format, regular_season_weeks)
    print(f"Wrote {n_exhibition} exhibition placeholder game(s).")

    conn.close()
    print(f"\nPVP schedule generated successfully for {args.season} ({scenario}).")


if __name__ == '__main__':
    main()
