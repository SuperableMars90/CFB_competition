"""
scripts/init_season.py
----------------------
Initializes a new season in the database by pulling data from CFBD.

Imports:
  - All FBS and FCS conferences
  - All FBS and FCS teams (FCS included for scheduling context)
  - Full regular season game schedule with week 0 corrections

Tier assignment (P4/G6) is a separate manual step after import --
run with --assign-tiers to do it interactively, or update the DB
directly via SQL.

Usage:
    # Dry run -- preview without writing
    PYTHONPATH=. python scripts/init_season.py --year 2025 --dry-run

    # Live run
    PYTHONPATH=. python scripts/init_season.py --year 2025

    # With week 0 game IDs
    PYTHONPATH=. python scripts/init_season.py --year 2025 --week0-ids 401628573,401628574

    # With canonical name overrides
    PYTHONPATH=. python scripts/init_season.py --year 2025 --overrides config/name_overrides_2025.json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.cfbd_client import CFBDClient, CFBDError
from db.connection import get_connection

# Only import these classification levels
VALID_CLASSIFICATIONS = {'fbs', 'fcs'}

# P4/G6 tier assignments -- applied to conferences after import.
# Keys match CFBD conference names exactly.
# Update each season as needed for realignment.
CONFERENCE_TIERS = {
    # P4
    'SEC':          'P4',
    'Big Ten':      'P4',
    'Big 12':       'P4',
    'ACC':          'P4',
    # G6
    'Sun Belt':     'G6',
    'MAC':          'G6',
    'Conference USA': 'G6',
    'Mountain West': 'G6',
    'American Athletic': 'G6',
    'Pac-12':       'G6',
    # Pseudo-conferences (created manually, not from CFBD)
    'P4 Independent': 'P4',
    'G6 Independent': 'G6',
}

# Teams to manually assign to pseudo-conferences.
# Key: CFBD team id (int). Value: pseudo-conference CFBD name.
# Update each season.
INDEPENDENT_ASSIGNMENTS = {
    87: 'P4 Independent',   # Notre Dame
    41: 'G6 Independent',   # UConn
}


# ------------------------------------------------------------------
# Name overrides
# ------------------------------------------------------------------

def load_overrides(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items() if not k.startswith('_')}


def build_canonical_names(team, overrides):
    """
    Build name_short, name_display, name_full from CFBD data.
    Apply any manual overrides on top.
    """
    school  = team.get('school', '')
    mascot  = team.get('mascot', '')
    abbrev  = team.get('abbreviation', '') or ''
    cfbd_id = team.get('id')

    defaults = {
        'name_short':   abbrev or school,
        'name_display': school,
        'name_full':    f"{school} {mascot}".strip() if mascot else school,
        'abbreviation': abbrev,
    }

    override = overrides.get(cfbd_id, {})
    return {**defaults, **override}


# ------------------------------------------------------------------
# Stage 1: Conferences
# ------------------------------------------------------------------

def build_conference_rows(cfbd_conferences, season_id):
    rows = []
    skipped = []

    for c in cfbd_conferences:
        classification = c.get('classification', '').lower()
        if classification not in VALID_CLASSIFICATIONS:
            skipped.append(c.get('name'))
            continue

        tier = CONFERENCE_TIERS.get(c.get('name'), None)
        level = 'FBS' if classification == 'fbs' else 'FCS'

        rows.append({
            'season_id':    season_id,
            'name':         c.get('name', ''),
            'abbreviation': c.get('abbreviation') or c.get('name', ''),
            'tier':         tier,   # NULL for FCS and unrecognized FBS conferences
            'cfbd_id':      str(c.get('id', '')),
            'level':        level,
            'notes':        c.get('shortName'),
        })

    # Pseudo-conferences for INDEPENDENT_ASSIGNMENTS — not returned by CFBD API
    rows.append({
        'season_id':    season_id,
        'name':         'P4 Independent',
        'abbreviation': 'P4_IND',
        'tier':         'P4',
        'cfbd_id':      None,
        'level':        'FBS',
        'notes':        'FBS independents assigned P4 tier (e.g. Notre Dame)',
    })
    rows.append({
        'season_id':    season_id,
        'name':         'G6 Independent',
        'abbreviation': 'G6_IND',
        'tier':         'G6',
        'cfbd_id':      None,
        'level':        'FBS',
        'notes':        'FBS independents assigned G6 tier (e.g. UConn)',
    })
    # Catch-all for FCS teams whose sub-conference name doesn't match (independents, name drift)
    rows.append({
        'season_id':    season_id,
        'name':         'FCS Independent',
        'abbreviation': 'FCS_IND',
        'tier':         None,
        'cfbd_id':      None,
        'level':        'FCS',
        'notes':        'Catch-all for FCS teams with no matched sub-conference',
    })

    return rows, skipped


# ------------------------------------------------------------------
# Stage 2: Teams
# ------------------------------------------------------------------

def build_team_rows(cfbd_teams, conf_rows, overrides, season_id):
    # Build lookup by conference name
    conf_by_name = {r['name']: r for r in conf_rows}
    rows = []
    skipped = []

    for team in cfbd_teams:
        classification = str(team.get('classification', '')).lower()
        if classification not in VALID_CLASSIFICATIONS:
            continue

        cfbd_id     = team.get('id')
        cfbd_conf   = INDEPENDENT_ASSIGNMENTS.get(cfbd_id) or team.get('conference', '')
        conf_row    = conf_by_name.get(cfbd_conf)

        if conf_row is None:
            if classification == 'fcs':
                # FCS independents or sub-conference name mismatch — use catch-all
                conf_row = conf_by_name.get('FCS Independent')
            if conf_row is None:
                skipped.append({
                    'school':     team.get('school'),
                    'conference': cfbd_conf,
                    'cfbd_id':    cfbd_id,
                })
                continue

        names = build_canonical_names(team, overrides)
        level = 'FBS' if classification == 'fbs' else 'FCS'

        rows.append({
            'season_id':    season_id,
            'cfbd_id':      str(cfbd_id),
            'name_short':   names['name_short'],
            'name_display': names['name_display'],
            'name_full':    names['name_full'],
            'abbreviation': names['abbreviation'],
            'level':        level,
            '_conf_name':   cfbd_conf,
        })

    return rows, skipped


# ------------------------------------------------------------------
# Stage 3: Games
# ------------------------------------------------------------------

def build_game_rows(cfbd_games, team_id_map, season_id, week0_cfbd_ids):
    rows = []
    skipped = []

    for game in cfbd_games:
        cfbd_game_id = game.get('id')
        home_cfbd    = str(game.get('homeId', ''))
        away_cfbd    = str(game.get('awayId', ''))
        home_db_id   = team_id_map.get(home_cfbd)
        away_db_id   = team_id_map.get(away_cfbd)

        if not home_db_id or not away_db_id:
            skipped.append({
                'cfbd_game_id': cfbd_game_id,
                'home':         game.get('homeTeam'),
                'away':         game.get('awayTeam'),
            })
            continue

        cfbd_week   = game.get('week', 1)
        is_week0    = cfbd_game_id in week0_cfbd_ids or cfbd_week == 0
        week        = 0 if is_week0 else cfbd_week
        date_str    = game.get('startDate', '') or ''
        game_date   = date_str[:10] if date_str else None
        game_time   = date_str[11:19] if len(date_str) > 10 else None

        rows.append({
            'season_id':    season_id,
            'home_team_id': home_db_id,
            'away_team_id': away_db_id,
            'cfbd_game_id': cfbd_game_id,
            'week':         week,
            'is_week0':     is_week0,
            'is_neutral':   bool(game.get('neutralSite', False)),
            'game_date':    game_date,
            'game_time':    game_time,
            'home_score':   game.get('homePoints'),
            'away_score':   game.get('awayPoints'),
            'status':       'scheduled',
        })

    return rows, skipped


# ------------------------------------------------------------------
# Database writes
# ------------------------------------------------------------------

def write_season(conn, year, week0_cutoff_week):
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO seasons (year, first_week, last_week, week0_cutoff_week)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
        """,
        (year, 1, 15, week0_cutoff_week)
    )
    conn.commit()
    season_id = cursor.lastrowid
    cursor.close()
    return season_id


def write_conferences(conn, rows, season_id):
    cursor = conn.cursor()
    inserted = {}
    for row in rows:
        cursor.execute(
            """
            INSERT INTO conferences
                (season_id, name, abbreviation, tier, cfbd_id, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """,
            (season_id, row['name'], row['abbreviation'],
             row.get('tier'), row.get('cfbd_id'), row.get('notes'))
        )
        inserted[row['name']] = cursor.lastrowid
    conn.commit()
    cursor.close()
    return inserted  # {name: db_id}


def write_teams(conn, rows, conf_id_map, season_id):
    cursor = conn.cursor()
    inserted = {}
    skipped  = 0
    for row in rows:
        conf_id = conf_id_map.get(row['_conf_name'])
        if not conf_id:
            skipped += 1
            continue
        cursor.execute(
            """
            INSERT INTO teams
                (season_id, conference_id, cfbd_id, name_short,
                 name_display, name_full, abbreviation, level)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """,
            (season_id, conf_id, row['cfbd_id'], row['name_short'],
             row['name_display'], row['name_full'],
             row['abbreviation'], row['level'])
        )
        inserted[row['cfbd_id']] = cursor.lastrowid
    conn.commit()
    cursor.close()
    return inserted, skipped


def write_games(conn, rows):
    cursor = conn.cursor()
    count = 0
    for row in rows:
        cursor.execute(
            """
            INSERT INTO games
                (season_id, home_team_id, away_team_id, cfbd_game_id,
                 week, is_week0, is_neutral, game_date, game_time,
                 home_score, away_score, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)
            """,
            (row['season_id'], row['home_team_id'], row['away_team_id'],
             row['cfbd_game_id'], row['week'], row['is_week0'],
             row['is_neutral'], row['game_date'], row['game_time'],
             row['home_score'], row['away_score'], row['status'])
        )
        count += 1
    conn.commit()
    cursor.close()
    return count


# ------------------------------------------------------------------
# Dry run output
# ------------------------------------------------------------------

def print_dry_run(conf_rows, team_rows, game_rows,
                  skipped_confs, skipped_teams, skipped_games):
    print("\n" + "=" * 60)
    print("DRY RUN — nothing written to database")
    print("=" * 60)

    fbs_confs = [r for r in conf_rows if r['level'] == 'FBS']
    fcs_confs = [r for r in conf_rows if r['level'] == 'FCS']
    print(f"\nCONFERENCES: {len(conf_rows)} total "
          f"({len(fbs_confs)} FBS, {len(fcs_confs)} FCS)")
    print("  FBS conferences:")
    for r in sorted(fbs_confs, key=lambda x: x['name']):
        tier = r['tier'] or 'untiered'
        print(f"    [{tier:6}] {r['name']}")
    print("  FCS conferences:")
    for r in sorted(fcs_confs, key=lambda x: x['name']):
        print(f"    {r['name']}")

    fbs_teams = [r for r in team_rows if r['level'] == 'FBS']
    fcs_teams = [r for r in team_rows if r['level'] == 'FCS']
    print(f"\nTEAMS: {len(team_rows)} total "
          f"({len(fbs_teams)} FBS, {len(fcs_teams)} FCS)")

    if skipped_teams:
        print(f"\n  SKIPPED ({len(skipped_teams)} teams — conference not imported):")
        for t in skipped_teams[:10]:
            print(f"    - {t['school']} (conf: '{t['conference']}')")
        if len(skipped_teams) > 10:
            print(f"    ... and {len(skipped_teams) - 10} more")

    week0 = [g for g in game_rows if g['is_week0']]
    week_counts = {}
    for g in game_rows:
        week_counts[g['week']] = week_counts.get(g['week'], 0) + 1
    print(f"\nGAMES: {len(game_rows)} total, {len(week0)} week 0")
    for week in sorted(week_counts):
        label = 'Week 0' if week == 0 else f'Week {week:>2}'
        print(f"  {label}: {week_counts[week]} games")

    if week0:
        print(f"\n  WEEK 0 GAME IDs (for --week0-ids if re-running):")
        id_list = ','.join(str(g['cfbd_game_id']) for g in week0)
        print(f"    {id_list}")
        print(f"\n  WEEK 0 GAMES:")
        # Build reverse team map for display (cfbd_id -> cfbd_id, since dry run uses fake ids)
        for g in week0:
            neutral = ' (neutral)' if g.get('is_neutral') else ''
            print(f"    [{g['cfbd_game_id']}] date={g['game_date']}{neutral}")

    if skipped_games:
        print(f"\n  SKIPPED GAMES ({len(skipped_games)} — at least one team not imported):")
        for g in skipped_games[:10]:
            print(f"    {g['home']} vs {g['away']}")
        if len(skipped_games) > 10:
            print(f"    ... and {len(skipped_games) - 10} more")

    print("\n" + "=" * 60 + "\n")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Initialize a CFB game season from CFBD data.'
    )
    parser.add_argument('--year',         type=int, required=True)
    parser.add_argument('--dry-run',      action='store_true')
    parser.add_argument('--week0-ids',    type=str, default='', help='Comma-separated CFBD game IDs')
    parser.add_argument('--week0-file',   type=str, default='', help='Path to file with one game ID per line')
    parser.add_argument('--overrides',    type=str, default='')
    parser.add_argument('--week0-cutoff', type=int, default=4)
    args = parser.parse_args()

    week0_ids = set()
    if args.week0_ids:
        week0_ids.update(int(x.strip()) for x in args.week0_ids.split(',') if x.strip())
    if args.week0_file:
        with open(args.week0_file) as f:
            week0_ids.update(int(x.strip()) for x in f if x.strip())

    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"Loaded {len(overrides)} name override(s)")

    print(f"\nFetching {args.year} season data from CFBD...")
    client = CFBDClient()

    try:
        cfbd_conferences = client.get_conferences()
        print(f"  Conferences: {len(cfbd_conferences)} returned")

        cfbd_teams_fbs = client.get_teams(year=args.year, classification='fbs')
        print(f"  FBS teams: {len(cfbd_teams_fbs)} returned")

        cfbd_teams_fcs = client.get_teams(classification='fcs')
        print(f"  FCS teams: {len(cfbd_teams_fcs)} returned")

        # Merge; FBS year-filtered list takes precedence for any overlapping IDs
        fbs_ids = {t['id'] for t in cfbd_teams_fbs}
        cfbd_teams = cfbd_teams_fbs + [t for t in cfbd_teams_fcs if t['id'] not in fbs_ids]
        print(f"  Teams total: {len(cfbd_teams)}")

        cfbd_games = client.get_games(year=args.year)
        print(f"  Games: {len(cfbd_games)} returned")

    except CFBDError as e:
        print(f"\nCFBD API error: {e}")
        sys.exit(1)

    season_id = 0  # placeholder for dry run
    conf_rows, skipped_confs = build_conference_rows(cfbd_conferences, season_id)
    team_rows, skipped_teams = build_team_rows(cfbd_teams, conf_rows, overrides, season_id)
    fake_team_map = {r['cfbd_id']: i + 1 for i, r in enumerate(team_rows)}
    game_rows, skipped_games = build_game_rows(cfbd_games, fake_team_map, season_id, week0_ids)

    if args.dry_run:
        print_dry_run(conf_rows, team_rows, game_rows,
                      skipped_confs, skipped_teams, skipped_games)
        return

    # Live run
    print(f"\nWriting {args.year} season to database...")
    conn = get_connection()

    season_id = write_season(conn, args.year, args.week0_cutoff)
    print(f"  Season record: id={season_id}")

    conf_rows, _ = build_conference_rows(cfbd_conferences, season_id)
    conf_id_map  = write_conferences(conn, conf_rows, season_id)
    print(f"  Conferences written: {len(conf_id_map)}")

    team_rows, skipped_teams = build_team_rows(
        cfbd_teams, conf_rows, overrides, season_id
    )
    team_id_map, teams_skipped = write_teams(conn, team_rows, conf_id_map, season_id)
    print(f"  Teams written: {len(team_id_map)} (skipped {teams_skipped})")

    game_rows, skipped_games = build_game_rows(
        cfbd_games, team_id_map, season_id, week0_ids
    )
    game_count = write_games(conn, game_rows)
    print(f"  Games written: {game_count} (skipped {len(skipped_games)})")

    conn.close()
    print(f"\nSeason {args.year} initialized successfully.")
    print(f"\nNext step: assign tiers to FBS conferences.")
    print(f"  Run this to see which need tiers:")
    print(f"  SELECT name, tier FROM conferences WHERE season_id={season_id} AND tier IS NULL;")


if __name__ == '__main__':
    main()
