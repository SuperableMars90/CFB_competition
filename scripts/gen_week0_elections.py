"""
scripts/gen_week0_elections.py
------------------------------
Generate INSERT statements for `week0_elections` from a JSON file of
per-player team elections. Does NOT touch the DB except to read lookups
(players, teams, games, rosters) -- it only prints SQL to stdout, so you
can eyeball it before running it against Aiven.

Input JSON: { "<username>": ["<team name>", ...], ... }
  - key   = players.username (case-insensitive)
  - value = list of team names the player is banking for week 0; each is
            matched against teams.name_display / name_short / name_full /
            abbreviation (case-insensitive), scoped to the season.

For each (player, team) the script finds the season's week-0 game that
team plays in, checks the team is on the player's active roster, and
emits one INSERT. Anything it can't resolve is printed as a `-- ERROR`
comment and makes the script exit non-zero (valid rows still print).

Usage:
    PYTHONPATH=. python scripts/gen_week0_elections.py elections.json
    PYTHONPATH=. python scripts/gen_week0_elections.py elections.json --season 2026 > week0.sql
    PYTHONPATH=. python scripts/gen_week0_elections.py elections.json --upsert
"""

import argparse
import json
import sys

from db.connection import get_connection


def get_season_id(cur, year):
    cur.execute("SELECT id FROM seasons WHERE year = %s", (year,))
    row = cur.fetchone()
    if not row:
        sys.exit(f"No seasons row for year {year}")
    return row["id"]


def resolve_player(cur, username):
    cur.execute(
        "SELECT id, name, username FROM players WHERE LOWER(username) = LOWER(%s)",
        (username,),
    )
    return cur.fetchall()


def resolve_team(cur, season_id, name):
    cur.execute(
        """
        SELECT id, name_display
        FROM teams
        WHERE season_id = %s
          AND %s IN (LOWER(name_display), LOWER(name_short),
                     LOWER(name_full), LOWER(abbreviation))
        """,
        (season_id, name.lower()),
    )
    return cur.fetchall()


def week0_games_for_team(cur, season_id, team_id):
    cur.execute(
        """
        SELECT g.id, ht.name_display AS home, at.name_display AS away
        FROM games g
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at ON at.id = g.away_team_id
        WHERE g.season_id = %s
          AND g.is_week0 = 1
          AND %s IN (g.home_team_id, g.away_team_id)
        """,
        (season_id, team_id),
    )
    return cur.fetchall()


def on_active_roster(cur, season_id, player_id, team_id):
    cur.execute(
        """
        SELECT 1 FROM rosters
        WHERE season_id = %s AND player_id = %s AND team_id = %s AND is_active = 1
        LIMIT 1
        """,
        (season_id, player_id, team_id),
    )
    return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json_path", help="Path to the elections JSON file")
    ap.add_argument("--season", type=int, default=2026, help="Season year (default: 2026)")
    ap.add_argument(
        "--upsert",
        action="store_true",
        help="Emit INSERT ... ON DUPLICATE KEY UPDATE (idempotent re-runs)",
    )
    args = ap.parse_args()

    with open(args.json_path) as f:
        elections = json.load(f)

    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    season_id = get_season_id(cur, args.season)

    rows = []   # (player_id, season_id, game_id, team_id) + comment
    errors = []

    for username, team_names in elections.items():
        players = resolve_player(cur, username)
        if len(players) != 1:
            errors.append(f"player {username!r}: found {len(players)} matches, expected 1")
            continue
        player = players[0]

        for name in team_names:
            teams = resolve_team(cur, season_id, name)
            if len(teams) != 1:
                errors.append(
                    f"{username} / team {name!r}: found {len(teams)} matches in season {args.season}"
                )
                continue
            team = teams[0]

            games = week0_games_for_team(cur, season_id, team["id"])
            if len(games) != 1:
                errors.append(
                    f"{username} / {team['name_display']}: found {len(games)} week-0 games, expected 1"
                )
                continue
            game = games[0]

            if not on_active_roster(cur, season_id, player["id"], team["id"]):
                errors.append(
                    f"{username} / {team['name_display']}: team is NOT on this player's active roster"
                )
                continue

            rows.append(
                {
                    "player_id": player["id"],
                    "season_id": season_id,
                    "game_id": game["id"],
                    "team_id": team["id"],
                    "comment": f"{username} -> {team['name_display']} ({game['away']} @ {game['home']})",
                }
            )

    cur.close()
    conn.close()

    print(f"-- week0_elections inserts for season {args.season} (season_id={season_id})")
    print(f"-- {len(rows)} row(s), {len(errors)} error(s)\n")

    if rows:
        print("START TRANSACTION;")
        for r in rows:
            print(f"-- {r['comment']}")
            if args.upsert:
                print(
                    "INSERT INTO week0_elections "
                    "(player_id, season_id, game_id, team_id, declared, week_deployed, commissioner_assigned) "
                    f"VALUES ({r['player_id']}, {r['season_id']}, {r['game_id']}, {r['team_id']}, 0, NULL, 0) "
                    "ON DUPLICATE KEY UPDATE team_id = VALUES(team_id);"
                )
            else:
                print(
                    "INSERT INTO week0_elections "
                    "(player_id, season_id, game_id, team_id, declared, week_deployed, commissioner_assigned) "
                    f"VALUES ({r['player_id']}, {r['season_id']}, {r['game_id']}, {r['team_id']}, 0, NULL, 0);"
                )
        print("COMMIT;")

    if errors:
        print("\n-- ERRORS (not emitted above):")
        for e in errors:
            print(f"-- ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
