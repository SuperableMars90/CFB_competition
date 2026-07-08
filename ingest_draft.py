"""
scripts/ingest_draft.py
-----------------------
Reads a draft spreadsheet (CSV or XLSX) and populates the
draft_order and rosters tables for a given season and pod.

Expected spreadsheet columns (case-insensitive):
    Overall   -- overall pick number (1-100)
    Round     -- round number (1-25)
    Picker    -- player name as it appears in the players table
    Team      -- team name matched against teams.name_short

Usage:
    # Dry run -- see what would be written without touching the DB
    PYTHONPATH=. python scripts/ingest_draft.py --file 2025_draft.xlsx --season 2025 --pod "Pod 1" --dry-run

    # Live run
    PYTHONPATH=. python scripts/ingest_draft.py --file 2025_draft.xlsx --season 2025 --pod "Pod 1"

Notes:
    - Team names are matched against teams.name_short (case-insensitive)
    - Unmatched teams are flagged and must be resolved before a live run
    - Unmatched players are flagged similarly
    - The script is safe to re-run; existing picks are skipped
"""

import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config.loader import get_config
from db.connection import get_connection


# ------------------------------------------------------------------
# Load spreadsheet
# ------------------------------------------------------------------

def load_draft_file(path):
    """Load XLSX or CSV into a normalized DataFrame."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.xlsx':
        df = pd.read_excel(path)
    elif ext == '.csv':
        df = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Use .xlsx or .csv")

    # Normalize column names: lowercase, strip whitespace
    df.columns = [c.strip().lower() for c in df.columns]

    required = {'overall', 'round', 'picker', 'team'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Spreadsheet is missing required columns: {missing}")

    df = df.rename(columns={'overall': 'pick_number', 'round': 'round_number', 'picker': 'player_name', 'team': 'team_name'})
    df['pick_number'] = df['pick_number'].astype(int)
    df['round_number'] = df['round_number'].astype(int)
    df['player_name'] = df['player_name'].str.strip()
    df['team_name'] = df['team_name'].str.strip()

    return df


# ------------------------------------------------------------------
# Database lookups
# ------------------------------------------------------------------

def get_season_id(conn, year):
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM seasons WHERE year = %s", (year,))
    row = cursor.fetchone()
    cursor.close()
    return row['id'] if row else None


def get_pod_id(conn, season_id, pod_name):
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id FROM pods WHERE season_id = %s AND name = %s",
        (season_id, pod_name)
    )
    row = cursor.fetchone()
    cursor.close()
    return row['id'] if row else None


def get_players(conn, pod_id):
    """Returns {name_lower: player_id}. Pod membership is per-season
    (pod_memberships), not a permanent players.pod_id."""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT pl.id, pl.name FROM players pl
        JOIN pod_memberships pm ON pm.player_id = pl.id
        WHERE pm.pod_id = %s
        """,
        (pod_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return {r['name'].strip().lower(): r['id'] for r in rows}


def get_teams(conn, season_id):
    """Returns {name_short_lower: (team_id, name_short, conference)}"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT t.id, t.name_short, t.name_display, c.abbreviation AS conference
        FROM teams t
        JOIN conferences c ON t.conference_id = c.id
        WHERE t.season_id = %s
        """,
        (season_id,)
    )
    rows = cursor.fetchall()
    cursor.close()
    return {r['name_display'].strip().lower(): r for r in rows}


# ------------------------------------------------------------------
# Matching
# ------------------------------------------------------------------

def match_rows(df, player_map, team_map):
    """
    Attempt to match every row to a player_id and team_id.
    Returns (matched_rows, unmatched_players, unmatched_teams).
    matched_rows: list of dicts ready for DB insert
    """
    matched = []
    unmatched_players = set()
    unmatched_teams = []

    for _, row in df.iterrows():
        player_key = row['player_name'].lower()
        team_key = row['team_name'].lower()

        player_id = player_map.get(player_key)
        team_row = team_map.get(team_key)

        if player_id is None:
            unmatched_players.add(row['player_name'])

        if team_row is None:
            unmatched_teams.append({
                'pick': row['pick_number'],
                'name': row['team_name'],
            })

        if player_id and team_row:
            # Calculate pick within round
            players_count = len(player_map)
            pick_in_round = ((row['pick_number'] - 1) % players_count) + 1

            matched.append({
                'pick_number':   row['pick_number'],
                'round_number':  row['round_number'],
                'pick_in_round': pick_in_round,
                'player_name':   row['player_name'],
                'player_id':     player_id,
                'team_id':       team_row['id'],
                'team_name':     team_row['name_short'],
                'conference':    team_row['conference'],
            })

    return matched, list(unmatched_players), unmatched_teams


# ------------------------------------------------------------------
# Dry run output
# ------------------------------------------------------------------

def print_dry_run(matched, unmatched_players, unmatched_teams, season_id, pod_id):
    print("\n" + "=" * 60)
    print("DRY RUN — nothing written to database")
    print("=" * 60)
    print(f"\nSeason ID: {season_id}  |  Pod ID: {pod_id}")

    if unmatched_players:
        print(f"\n⚠️  UNMATCHED PLAYERS ({len(unmatched_players)}) — add these to the players table first:")
        for name in sorted(unmatched_players):
            print(f"    - '{name}'")

    if unmatched_teams:
        print(f"\n⚠️  UNMATCHED TEAMS ({len(unmatched_teams)}) — names not found in teams.name_short:")
        for t in unmatched_teams:
            print(f"    Pick {t['pick']:>3}: '{t['name']}'")
        print("\n  Tip: run --dry-run to see DB team names, then update name_overrides or the spreadsheet.")

    if matched:
        print(f"\n✅ MATCHED PICKS ({len(matched)}):")
        # Group by player for readability
        by_player = {}
        for m in matched:
            by_player.setdefault(m['player_name'], []).append(m)
        for player, picks in sorted(by_player.items()):
            print(f"\n  {player} ({len(picks)} picks):")
            for p in picks:
                print(f"    R{p['round_number']:>2} Pick {p['pick_number']:>3}: {p['team_name']} ({p['conference']})")

    print("\n" + "=" * 60)
    if unmatched_players or unmatched_teams:
        print("Resolve issues above before running without --dry-run.")
    else:
        print("All picks matched. Ready to run without --dry-run.")
    print("=" * 60 + "\n")


# ------------------------------------------------------------------
# Database writes
# ------------------------------------------------------------------

def write_draft(conn, matched, season_id, pod_id):
    cursor = conn.cursor()
    draft_inserted = 0
    roster_inserted = 0
    skipped = 0

    for m in matched:
        # draft_order
        try:
            cursor.execute(
                """
                INSERT INTO draft_order
                    (season_id, pod_id, player_id, pick_number, round_number, pick_in_round)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
                """,
                (season_id, pod_id, m['player_id'],
                 m['pick_number'], m['round_number'], m['pick_in_round'])
            )
            if cursor.rowcount == 1:
                draft_inserted += 1
            else:
                skipped += 1
        except Exception as e:
            conn.rollback()
            print(f"ERROR on draft_order pick {m['pick_number']}: {e}")
            sys.exit(1)

        # rosters
        try:
            cursor.execute(
                """
                INSERT INTO rosters
                    (player_id, team_id, season_id, pod_id, draft_round, draft_pick,
                     is_active, acquisition_type)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'draft')
                ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
                """,
                (m['player_id'], m['team_id'], season_id, pod_id,
                 m['round_number'], m['pick_number'])
            )
            if cursor.rowcount == 1:
                roster_inserted += 1
        except Exception as e:
            conn.rollback()
            print(f"ERROR on rosters pick {m['pick_number']}: {e}")
            sys.exit(1)

    conn.commit()
    cursor.close()
    return draft_inserted, roster_inserted, skipped


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Ingest a draft spreadsheet into the database.')
    parser.add_argument('--file',    required=True, help='Path to draft .xlsx or .csv file')
    parser.add_argument('--season',  required=True, type=int, help='Season year (e.g. 2025)')
    parser.add_argument('--pod',     required=True, help='Pod name as it appears in the pods table (e.g. "Pod 1")')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing to DB')
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"ERROR: File not found: {args.file}")
        sys.exit(1)

    print(f"Loading draft file: {args.file}")
    df = load_draft_file(args.file)
    print(f"  {len(df)} picks loaded")

    conn = get_connection()

    season_id = get_season_id(conn, args.season)
    if not season_id:
        print(f"ERROR: Season {args.season} not found in database. Run init_season.py first.")
        sys.exit(1)

    pod_id = get_pod_id(conn, season_id, args.pod)
    if not pod_id:
        print(f"ERROR: Pod '{args.pod}' not found for season {args.season}.")
        print("  Create it first:")
        print(f"    INSERT INTO pods (season_id, name) VALUES ({season_id}, '{args.pod}');")
        sys.exit(1)

    player_map = get_players(conn, pod_id)
    if not player_map:
        print(f"ERROR: No players found in pod '{args.pod}'. Add players to the database first.")
        print("  Tip: see the player setup instructions in the README.")
        sys.exit(1)

    team_map = get_teams(conn, season_id)

    print(f"  Season ID: {season_id}  |  Pod: '{args.pod}' (ID: {pod_id})")
    print(f"  Players in pod: {list(player_map.keys())}")
    print(f"  Teams in DB: {len(team_map)}")

    matched, unmatched_players, unmatched_teams = match_rows(df, player_map, team_map)

    if args.dry_run:
        print_dry_run(matched, unmatched_players, unmatched_teams, season_id, pod_id)
        return

    # Block live run if there are unmatched rows
    if unmatched_players or unmatched_teams:
        print("\nERROR: Cannot run live with unmatched picks. Run --dry-run to review issues.")
        sys.exit(1)

    draft_inserted, roster_inserted, skipped = write_draft(conn, matched, season_id, pod_id)
    conn.close()

    print(f"\nDone.")
    print(f"  draft_order rows inserted: {draft_inserted} (skipped {skipped} already existing)")
    print(f"  roster rows inserted:      {roster_inserted}")


if __name__ == '__main__':
    main()
