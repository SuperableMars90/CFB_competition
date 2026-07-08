"""
scripts/break_tie.py
---------------------
Interactive commissioner tool for the 4th-tier manual tiebreak override
in playoff seeding (lib.seeding.compute_seeding, criteria 1-3).

Run this once the PVP regular season has fully concluded and
compute_seeding() raises UnresolvedTieError. It prints the tied group
with their record/margin context, prompts you for the resolved order,
persists it to manual_tiebreaks, and re-runs seeding automatically --
repeating until fully resolved or you Ctrl-C.

Refuses to run if the regular season isn't actually complete yet (some
scheduled PVP games have no weekly_results yet) -- this is meant for
final seeding only, never for mid-season standings.

Usage:
    PYTHONPATH=. python scripts/break_tie.py --season 2026
    PYTHONPATH=. python scripts/break_tie.py --season 2026 --scoring-context standard_4player
"""

import argparse
from collections import defaultdict

from lib.db import get_connection
from lib.seeding import (
    get_pvp_regular_season_records,
    get_manual_tiebreaks,
    save_manual_tiebreak,
    record_and_margin,
    compute_seeding,
    UnresolvedTieError,
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


def get_commissioners():
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM players WHERE is_commissioner = 1")
        rows = cur.fetchall()
        cur.close()
    return rows


def prompt_for_commissioner():
    commissioners = get_commissioners()
    if len(commissioners) == 1:
        return commissioners[0]['id']
    print("\nWho is entering this decision?")
    for c in commissioners:
        print(f"  {c['id']}: {c['name']}")
    while True:
        choice = input("Commissioner player_id: ").strip()
        if choice.isdigit() and int(choice) in {c['id'] for c in commissioners}:
            return int(choice)
        print("Not a valid commissioner id, try again.")


def print_tied_group(group, all_records, names):
    print(f"\nUnresolved tie among: {', '.join(names[p] for p in sorted(group))}")
    print(f"{'Player':<15}{'In-group W-L-T':<18}{'In-group margin':<18}{'Season margin':<15}")
    for pid in sorted(group):
        w, l, t, m = record_and_margin(all_records[pid], group - {pid})
        _, _, _, season_margin = record_and_margin(all_records[pid], None)
        print(f"{names[pid]:<15}{f'{w}-{l}-{t}':<18}{m:<+18}{season_margin:<+15}")


def prompt_for_order(group, names):
    print("\nEnter the resolved order (best to worst) as comma-separated player_ids.")
    print(f"  Must include exactly: {', '.join(str(p) for p in sorted(group))}")
    while True:
        raw = input("Resolved order: ").strip()
        try:
            order = [int(x.strip()) for x in raw.split(',') if x.strip()]
        except ValueError:
            print("Couldn't parse that as a list of player_ids, try again.")
            continue
        if set(order) != group or len(order) != len(group):
            print(f"That doesn't match the tied group exactly ({sorted(group)}), try again.")
            continue
        return order


def main():
    parser = argparse.ArgumentParser(
        description='Interactively resolve a playoff-seeding tie the automated criteria couldn\'t.'
    )
    parser.add_argument('--season', type=int, required=True, help='Season year (e.g. 2026)')
    parser.add_argument('--scoring-context', type=str, default='',
                        help='Scoring context name; defaults to the season\'s active context')
    parser.add_argument('--notes', type=str, default='', help='Optional note applied to any decision made this run')
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
    # this tool is for final seeding only, never mid-season standings.
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

    while True:
        manual = get_manual_tiebreaks(season_id, ctx['id'])
        try:
            seeding = compute_seeding(records, manual)
        except UnresolvedTieError as e:
            names = get_player_names(list(e.group))
            print_tied_group(e.group, records, names)
            order = prompt_for_order(e.group, names)
            commissioner_id = prompt_for_commissioner()
            notes = args.notes or input("Notes (optional): ").strip() or None
            save_manual_tiebreak(season_id, ctx['id'], e.group, order, commissioner_id, notes)
            print("Saved -- re-running seeding computation...")
            continue
        else:
            names = get_player_names([row['player_id'] for row in seeding])
            print("\nSeeding fully resolved:")
            for row in seeding:
                print(
                    f"  {row['seed']:>2}. {names[row['player_id']]:<15} "
                    f"{row['wins']}-{row['losses']}-{row['ties']}  "
                    f"({row['win_pct']:.3f})  margin={row['net_margin']:+d}"
                )
            break


if __name__ == '__main__':
    main()
