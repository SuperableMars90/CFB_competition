"""
scripts/seed_playoffs.py
-------------------------
Run once, the week the PVP regular season concludes. Computes final
seeding, assigns bracket seeds (pod-aware for six_team/eight_team),
writes them into season_standings, and resolves every seed-sourced
playoff_games placeholder to a real player_id.

Refuses to run if the regular season isn't actually complete yet, same
scope guard as scripts/break_tie.py -- this is for final seeding only.
If compute_seeding() can't fully resolve the standings, run
scripts/break_tie.py first.

Usage:
    PYTHONPATH=. python scripts/seed_playoffs.py --season 2026
    PYTHONPATH=. python scripts/seed_playoffs.py --season 2026 --scoring-context standard_4player
"""

import argparse
from collections import defaultdict

from lib.db import get_connection
from lib.seeding import (
    get_pvp_regular_season_records,
    get_manual_tiebreaks,
    compute_seeding,
    UnresolvedTieError,
)
from lib.playoffs import (
    assign_seeds_4_team,
    assign_seeds_6_team,
    assign_seeds_8_team,
    bracket_format_for_league_shape,
)


def get_season_id(year):
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id FROM seasons WHERE year = %s", (year,))
        row = cur.fetchone()
        cur.close()
    return row['id'] if row else None


def get_scoring_context(season_id, name=None):
    """Returns {id, name} -- the named context, or the season's active one."""
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


def get_scheduled_game_counts(season_id, scoring_context_id, cutoff_week):
    """{player_id: count of matchup_pairings rows involving them through cutoff_week}."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT player_a_id, player_b_id FROM matchup_pairings
            WHERE season_id = %s AND scoring_context_id = %s AND week <= %s
            """,
            (season_id, scoring_context_id, cutoff_week),
        )
        rows = cur.fetchall()
        cur.close()
    counts = defaultdict(int)
    for a_id, b_id in rows:
        counts[a_id] += 1
        counts[b_id] += 1
    return dict(counts)


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


def get_pods_for_season(season_id):
    """Returns [{id, name}, ...] ordered by id."""
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


def write_season_standings_seeding(season_id, scoring_context_id, seeding, seed_to_player):
    """
    Upserts pvp_rank/pvp_wins/pvp_losses/pvp_ties/pvp_win_pct/pvp_net_margin
    and playoff_seed per player -- touches only these columns, leaving
    playoff_placement/match_play_standing/total_points_standing alone.
    """
    player_to_seed = {player_id: seed for seed, player_id in seed_to_player.items()}
    with get_connection() as conn:
        cur = conn.cursor()
        for row in seeding:
            cur.execute(
                """
                INSERT INTO season_standings
                    (season_id, scoring_context_id, player_id,
                     pvp_rank, pvp_wins, pvp_losses, pvp_ties, pvp_win_pct, pvp_net_margin,
                     playoff_seed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    pvp_rank = VALUES(pvp_rank), pvp_wins = VALUES(pvp_wins),
                    pvp_losses = VALUES(pvp_losses), pvp_ties = VALUES(pvp_ties),
                    pvp_win_pct = VALUES(pvp_win_pct), pvp_net_margin = VALUES(pvp_net_margin),
                    playoff_seed = VALUES(playoff_seed)
                """,
                (
                    season_id, scoring_context_id, row['player_id'],
                    row['seed'], row['wins'], row['losses'], row['ties'],
                    row['win_pct'], row['net_margin'],
                    player_to_seed.get(row['player_id']),
                ),
            )
        conn.commit()
        cur.close()


def resolve_seed_placeholders(season_id, scoring_context_id, bracket_format, seed_to_player):
    """
    Resolves every playoff_games row (game_type='bracket') with a
    seed-sourced side still NULL to its real player_id. Each seed number
    appears in exactly one GameSpec slot -- a 1:1 resolution, no fan-out.
    Returns the number of rows updated.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, side_a_source_type, side_a_source_ref, player_a_id,
                   side_b_source_type, side_b_source_ref, player_b_id
            FROM playoff_games
            WHERE season_id = %s AND scoring_context_id = %s
              AND bracket_format = %s AND game_type = 'bracket'
            """,
            (season_id, scoring_context_id, bracket_format),
        )
        rows = cur.fetchall()

        updated = 0
        for row in rows:
            player_a_id = row['player_a_id']
            player_b_id = row['player_b_id']
            if row['side_a_source_type'] == 'seed' and player_a_id is None:
                player_a_id = seed_to_player[row['side_a_source_ref']]
            if row['side_b_source_type'] == 'seed' and player_b_id is None:
                player_b_id = seed_to_player[row['side_b_source_ref']]

            if player_a_id != row['player_a_id'] or player_b_id != row['player_b_id']:
                cur.execute(
                    "UPDATE playoff_games SET player_a_id = %s, player_b_id = %s WHERE id = %s",
                    (player_a_id, player_b_id, row['id']),
                )
                updated += 1
        conn.commit()
        cur.close()
    return updated


def main():
    parser = argparse.ArgumentParser(
        description='Compute final PVP regular-season seeding and populate the playoff bracket.'
    )
    parser.add_argument('--season', type=int, required=True, help='Season year (e.g. 2026)')
    parser.add_argument('--scoring-context', type=str, default='',
                        help='Scoring context name; defaults to the season\'s active context')
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

    records = get_pvp_regular_season_records(season_id, ctx['id'], cutoff_week)

    # Scope guard: refuse if the regular season isn't actually complete --
    # same stance as scripts/break_tie.py.
    scheduled = get_scheduled_game_counts(season_id, ctx['id'], cutoff_week)
    incomplete = {
        pid: (n_scheduled, len(records.get(pid, [])))
        for pid, n_scheduled in scheduled.items()
        if n_scheduled != len(records.get(pid, []))
    }
    if incomplete:
        names = get_player_names(list(incomplete.keys()))
        print(f"ERROR: the PVP regular season (through week {cutoff_week}) isn't fully scored yet:")
        for pid, (n_sched, n_scored) in incomplete.items():
            print(f"  {names.get(pid, pid)}: {n_scored}/{n_sched} games scored")
        print("Finish scoring all regular-season weeks before running this tool.")
        return

    manual = get_manual_tiebreaks(season_id, ctx['id'])
    try:
        seeding = compute_seeding(records, manual)
    except UnresolvedTieError as e:
        print(f"ERROR: unresolved tie among player_ids {sorted(e.group)} -- "
              f"run scripts/break_tie.py --season {args.season} first.")
        return

    pods = get_pods_for_season(season_id)
    try:
        bracket_format = bracket_format_for_league_shape(len(pods), len(seeding))
    except ValueError as e:
        print(f"ERROR: {e} (season {args.season})")
        return

    if bracket_format == 'four_team':
        seed_to_player = assign_seeds_4_team(seeding)
    elif bracket_format == 'six_team':
        seed_to_player = assign_seeds_6_team(seeding, get_pod_of_player(season_id))
    else:
        seed_to_player = assign_seeds_8_team(seeding, get_pod_of_player(season_id))

    write_season_standings_seeding(season_id, ctx['id'], seeding, seed_to_player)
    n_resolved = resolve_seed_placeholders(season_id, ctx['id'], bracket_format, seed_to_player)
    print(f"Wrote {len(seeding)} season_standings row(s); resolved {n_resolved} "
          f"seed-sourced playoff_games row(s) ({bracket_format}).")

    names = get_player_names([row['player_id'] for row in seeding])
    player_to_seed = {player_id: seed for seed, player_id in seed_to_player.items()}
    print("\nFinal PVP regular-season standings + bracket seeding:")
    for row in seeding:
        pid = row['player_id']
        print(
            f"  rank {row['seed']:>2}  seed {player_to_seed.get(pid, '?'):>2}  "
            f"{names[pid]:<15} {row['wins']}-{row['losses']}-{row['ties']}  "
            f"({row['win_pct']:.3f})  margin={row['net_margin']:+d}"
        )


if __name__ == '__main__':
    main()
