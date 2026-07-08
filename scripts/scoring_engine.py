"""
scripts/scoring_engine.py
--------------------------
Live scoring engine for the CFB Fantasy Game.

Runs on game days: fetches the CFBD scoreboard every 5 minutes, scores all
players' lineup picks for the week, writes a checkpoint to the week's JSON
file, updates the score plot, and calls push_website_update().

Usage:
    python scripts/scoring_engine.py --season 1 --week 3
    python scripts/scoring_engine.py --season 1 --week 3 --interval 300
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use('Agg')   # headless — no display required
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.db import get_connection, update_game_results, get_pod_membership_map
from lib.scoring import (
    load_week_picks,
    score_picks,
    compute_player_totals,
    build_score_table,
)
from scripts.html_scoring import (
    generate_week_page,
    update_week_page,
    render_fragment,
    render_recap_fragment,
    merge_recap_summary,
    write_final_page,
)
from lib.records import check_weekly_records, write_records
from scripts.html_records import push_records_table
from lib.metagame import (
    compute_weekly_standings,
    write_weekly_results,
    resolve_pvp_matchups,
    build_season_leaderboard,
)
from scripts.cfbd_client import CFBDClient
from scripts.wordpress_client import WordPressClient


# ------------------------------------------------------------------
# Output paths
# ------------------------------------------------------------------

OUTPUT_DIR = Path(__file__).parent.parent / 'output' / 'scoring'


def _week_json_path(week: int) -> Path:
    return OUTPUT_DIR / f'week_{week}.json'


def _week_plot_path(week: int) -> Path:
    return OUTPUT_DIR / f'week_{week}_scores.png'


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------

def initialize_week(season_id: int, week: int) -> dict:
    """
    Prepare the scoring state for the week.

    - Loads all lineup picks from the DB.
    - Pre-scores any week 0 banked games (historical — scores won't change).
    - Builds the set of cfbd_game_ids that need live tracking this week.
    - Returns a state dict passed to each checkpoint run.
    """
    picks = load_week_picks(season_id, week)

    # Week 0 games are already final; mark their cfbd_game_ids as complete upfront.
    completed_game_ids: set[int] = set()
    for pick in picks:
        if pick['is_week0'] and pick['cfbd_game_id'] is not None:
            completed_game_ids.add(pick['cfbd_game_id'])

    # The live games we need to track are regular picks with a cfbd_game_id
    # not already in completed_game_ids.
    live_game_ids: set[int] = {
        pick['cfbd_game_id']
        for pick in picks
        if not pick['is_week0']
        and not pick['is_pass']
        and pick['cfbd_game_id'] is not None
    }

    return {
        'season_id': season_id,
        'week': week,
        'picks': picks,
        'completed_game_ids': completed_game_ids,
        'live_game_ids': live_game_ids,
    }


# ------------------------------------------------------------------
# Scoreboard fetch
# ------------------------------------------------------------------

def fetch_live_scores(client: CFBDClient) -> dict[int, dict]:
    """
    Call the CFBD scoreboard endpoint and return a dict keyed by cfbd_game_id.

    Each value: {status, home_points, away_points, home_id, away_id, period, clock}
    Status values from CFBD: 'scheduled', 'in_progress', 'final' (or similar).
    """
    raw = client.get_scoreboard()
    scores: dict[int, dict] = {}
    for game in raw:
        gid = game.get('id')
        if gid is None:
            continue
        home = game.get('homeTeam', {})
        away = game.get('awayTeam', {})
        scores[gid] = {
            'status': game.get('status', 'scheduled'),
            'home_points': home.get('points'),
            'away_points': away.get('points'),
            'home_cfbd_id': home.get('id'),
            'away_cfbd_id': away.get('id'),
            'period': game.get('period'),
            'clock': game.get('clock'),
        }
    return scores


# ------------------------------------------------------------------
# Checkpoint
# ------------------------------------------------------------------

def run_checkpoint(state: dict, client: CFBDClient, include_all_games: bool = False) -> dict:
    """
    Score all picks using the current scoreboard, persist results, and update
    the plot. Returns the checkpoint dict that was written.

    include_all_games: if True, the raw scoreboard is included in full in the
    checkpoint JSON for retrospective analysis (e.g. theoretical lineups).
    """
    live_scores = fetch_live_scores(client)
    now = datetime.now(timezone.utc)

    # Mark any newly completed games
    for gid, game in live_scores.items():
        if game['status'] in ('final', 'completed'):
            state['completed_game_ids'].add(gid)

    scored = score_picks(state['picks'], live_scores)
    player_totals = compute_player_totals(scored)
    table = build_score_table(player_totals)

    # Game counts for status summary
    week_game_ids = state['live_game_ids']
    n_total = len(week_game_ids)
    n_complete = sum(1 for gid in week_game_ids if gid in state['completed_game_ids'])
    n_in_progress = sum(
        1 for gid in week_game_ids
        if gid in live_scores and live_scores[gid]['status'] == 'in_progress'
    )
    n_pending = n_total - n_complete - n_in_progress

    checkpoint = {
        'timestamp': now.isoformat(),
        'games_total': n_total,
        'games_complete': n_complete,
        'games_in_progress': n_in_progress,
        'games_pending': n_pending,
        'players': {
            data['player_name']: {
                'p4_subtotal': data['p4'],
                'g6_subtotal': data['g6'],
                'wildcard_score': data['wildcard'],
                'total': data['total'],
                'slots': [
                    {
                        'slot_type': s['slot_type'],
                        'conference_slug': s['conference_slug'],
                        'team': s['team_name'],
                        'group': s['group'],
                        'is_week0': s['is_week0'],
                        'is_pass': s['is_pass'],
                        'game_status': s.get('game_status'),
                        'margin': s.get('margin'),
                    }
                    for s in data['slots']
                ],
            }
            for data in player_totals.values()
        },
    }

    if include_all_games:
        checkpoint['all_scoreboard_games'] = live_scores

    # Print the score table to stdout
    print(f"\n[{now.strftime('%H:%M:%S UTC')}] Week {state['week']} — "
          f"{n_complete}/{n_total} games complete, {n_in_progress} in progress")
    print(table.to_string(index=False))

    # Append to week JSON
    _append_checkpoint(state['week'], state['season_id'], checkpoint)

    # Update plot
    _update_plot(state['week'])

    # Update HTML score page
    try:
        update_week_page(state['season_id'], state['week'], live_scores)
    except Exception as e:
        print(f"[WARNING] HTML page update failed: {e}")

    # Push live scores to WordPress
    try:
        push_website_update(state['season_id'], state['week'], live_scores)
    except Exception as e:
        print(f"[WARNING] WordPress push failed: {e}")

    return checkpoint


def _append_checkpoint(week: int, season_id: int, checkpoint: dict) -> None:
    """Append a checkpoint to the week's JSON file, creating it if needed."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = _week_json_path(week)

    if path.exists():
        with open(path) as f:
            data = json.load(f)
    else:
        data = {'season_id': season_id, 'week': week, 'checkpoints': []}

    data['checkpoints'].append(checkpoint)

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _update_plot(week: int) -> None:
    """
    Regenerate the score-over-time line plot from the week's checkpoint file.
    Saved to output/scoring/week_{N}_scores.png.
    """
    path = _week_json_path(week)
    if not path.exists():
        return

    with open(path) as f:
        data = json.load(f)

    checkpoints = data.get('checkpoints', [])
    if not checkpoints:
        return

    timestamps = [datetime.fromisoformat(c['timestamp']) for c in checkpoints]
    players = list(checkpoints[0]['players'].keys())

    fig, ax = plt.subplots(figsize=(10, 5))
    for player in players:
        scores = [c['players'][player]['total'] for c in checkpoints]
        ax.plot(timestamps, scores, marker='o', markersize=3, label=player)

    ax.set_title(f'Week {week} Live Scores')
    ax.set_xlabel('Time (UTC)')
    ax.set_ylabel('Total Points')
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    plot_path = _week_plot_path(week)
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------
# Finalization (end-of-week DB write)
# ------------------------------------------------------------------

def _get_season_year(season_id: int) -> int:
    """Look up the calendar year for a season_id."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT year FROM seasons WHERE id = %s", (season_id,))
        row = cur.fetchone()
        cur.close()
    if row is None:
        raise ValueError(f"season_id={season_id} not found in DB")
    return row[0]


def _fetch_final_scores(client: CFBDClient, year: int, week: int) -> dict[int, dict]:
    """
    Fetch authoritative final scores for a completed week from the CFBD
    /games endpoint (not /scoreboard, which only shows live games).

    Returns the same dict format as fetch_live_scores():
      {cfbd_game_id: {status, home_points, away_points, home_cfbd_id, away_cfbd_id}}
    """
    raw = client.get_games_for_week(year, week)
    scores: dict[int, dict] = {}
    for game in raw:
        gid = game.get('id')
        if gid is None:
            continue
        completed = game.get('completed', False)
        scores[gid] = {
            'status': 'final' if completed else 'scheduled',
            'home_points': game.get('homePoints'),
            'away_points': game.get('awayPoints'),
            'home_cfbd_id': game.get('homeId'),
            'away_cfbd_id': game.get('awayId'),
        }
    return scores


def finalize_week(state: dict, client: CFBDClient) -> None:
    """
    Write authoritative end-of-week results to the DB.

    Fetches final scores from the CFBD /games endpoint (works both at the
    natural end of the scoring run and when called via --finalize-only for
    score corrections).  Overwrites any prior weekly_results rows for the
    week so corrections are idempotent.

    Steps:
      1. Fetch final scores from CFBD /games (not live scoreboard)
      2. Write those final scores/status onto the games table itself
         (games.status otherwise stays stuck at 'scheduled' forever — see
         update_game_results())
      3. Re-score all picks against those authoritative scores
      4. Compute player totals
      5. Assign pod_rank, overall_rank, match_play_points
      6. Upsert into weekly_results
      7. Resolve any open PVP matchup_pairings
      8. Print final leaderboard
    """
    season_id = state['season_id']
    week = state['week']

    print(f"\n[FINALIZE] Week {week} — fetching authoritative scores from CFBD...")
    year = _get_season_year(season_id)
    final_scores = _fetch_final_scores(client, year, week)

    n_games_updated = update_game_results(final_scores)
    print(f"[FINALIZE] {n_games_updated} game row(s) marked final in the DB.")

    scored = score_picks(state['picks'], final_scores)
    player_totals = compute_player_totals(scored)

    pod_of_player = get_pod_membership_map(season_id)
    standings = compute_weekly_standings(season_id, week, player_totals, pod_of_player)
    write_weekly_results(standings)

    n_pvp = resolve_pvp_matchups(season_id, week, player_totals)

    # T21.1: check this week's results against the record book, then push
    # the current Records and Awards table live (Zach, 2026-07-05:
    # "checked for and posted weekly") -- regenerated from DB state every
    # week regardless of whether anything actually changed, same as the
    # rosters push's philosophy.
    record_candidates = check_weekly_records(season_id, week, player_totals)
    n_records = write_records(record_candidates)

    try:
        push_records_table()
    except Exception as e:
        print(f"[WARNING] Records table WordPress push failed: {e}")

    table = build_score_table(player_totals)
    leaderboard = build_season_leaderboard(season_id)

    print(f"[FINALIZE] Week {week} results written to DB.")
    if n_pvp:
        print(f"[FINALIZE] {n_pvp} PVP matchup(s) resolved.")
    if n_records:
        print(f"[FINALIZE] {n_records} new record(s) set:")
        for c in record_candidates:
            print(f"    {c['record_name']}: {c['value']} (player_id={c['player_id']})")
    print("\n— Week results —")
    print(table.to_string(index=False))
    print("\n— Season leaderboard —")
    print(leaderboard.to_string(index=False))

    # Push recap to WordPress + write the local output/web/week_N_final.html artifact
    try:
        push_recap_update(season_id, week, standings, leaderboard, final_scores)
        print(f"[FINALIZE] Week {week} recap pushed to WordPress and written locally.")
    except Exception as e:
        print(f"[WARNING] WordPress recap push failed: {e}. Run --finalize-only to retry.")


# ------------------------------------------------------------------
# WordPress push
# ------------------------------------------------------------------

def push_website_update(season_id: int, week: int, live_scores: dict) -> None:
    """
    Push the live scoring fragment to WordPress. Idempotent by slug (see
    WordPressClient.upsert): creates the week's "Status" post on first
    call, updates it in place on every subsequent checkpoint.
    """
    year = _get_season_year(season_id)
    fragment = render_fragment(season_id, week, live_scores)
    WordPressClient().upsert(
        'posts',
        slug=f"{year}-week-{week}-status",
        title=f"{year} Week {week} Status",
        content=fragment,
    )


def push_recap_update(
    season_id: int,
    week: int,
    standings: list[dict],
    leaderboard,
    live_scores: dict,
) -> None:
    """
    Push the week's recap fragment to WordPress ("Recap" post, upserted by
    slug) and write the matching local output/web/week_N_final.html
    artifact (T13).

    Preserves the "Commissioner's Notes" summary Zach may have typed
    directly into a prior version of THIS SAME week's recap post (e.g.
    if this is a --finalize-only re-push/correction): fetches whatever's
    currently live at this slug first, so merge_recap_summary() can
    splice its existing notes back in rather than resetting to blank.
    A brand-new week (nothing live yet at this slug) just gets the
    fresh, empty notes section untouched.
    """
    year = _get_season_year(season_id)
    slug = f"{year}-week-{week}-recap"
    fragment = render_recap_fragment(season_id, week, standings, leaderboard, live_scores)

    client = WordPressClient()
    existing = client.get_by_slug('posts', slug)
    existing_content = existing['content']['raw'] if existing else None
    fragment = merge_recap_summary(existing_content, fragment)

    client.upsert('posts', slug=slug, title=f"{year} Week {week} Recap", content=fragment)
    write_final_page(season_id, week, standings, leaderboard, live_scores)


# ------------------------------------------------------------------
# Scheduler
# ------------------------------------------------------------------

def run_scheduler(
    season_id: int,
    week: int,
    interval_seconds: int = 300,
    include_all_games: bool = False,
) -> None:
    """
    Initialize the week and run checkpoints every interval_seconds until all
    games tracked in this week's lineups are marked complete.
    """
    client = CFBDClient()
    state = initialize_week(season_id, week)

    n_tracked = len(state['live_game_ids'])
    print(f"Scoring engine started — season {season_id}, week {week}, "
          f"{n_tracked} live games to track.")
    print(f"Week 0 games pre-scored: {len(state['completed_game_ids'])} game(s) locked.")

    try:
        path = generate_week_page(season_id, week)
        print(f"Initial HTML page written: {path}")
    except Exception as e:
        print(f"[WARNING] Initial HTML page generation failed: {e}")

    try:
        push_website_update(season_id, week, live_scores={})
        print("Initial WordPress status post created/updated.")
    except Exception as e:
        print(f"[WARNING] Initial WordPress push failed: {e}")

    while True:
        try:
            run_checkpoint(state, client, include_all_games=include_all_games)
        except Exception as e:
            print(f"[WARNING] Checkpoint failed: {e}")

        # Stop when every tracked game is complete
        if state['live_game_ids'] and state['live_game_ids'].issubset(state['completed_game_ids']):
            print("All games complete. Scoring engine stopping.")
            try:
                finalize_week(state, client)
            except Exception as e:
                print(f"[WARNING] Finalization failed: {e}. Run --finalize-only to retry.")
            break

        # Also stop when there are no live games to track (no lineups submitted?)
        if not state['live_game_ids']:
            print("No live games to track this week. Stopping.")
            break

        print(f"Next checkpoint in {interval_seconds}s.")
        time.sleep(interval_seconds)


# ------------------------------------------------------------------
# Finalize-only entry point (score corrections / reruns)
# ------------------------------------------------------------------

def run_finalize_only(season_id: int, week: int) -> None:
    """
    Skip live polling entirely — load picks from DB, fetch authoritative
    final scores from CFBD /games, and write results to weekly_results.

    Use this to:
      - Re-run finalization after a score correction
      - Write results for a week that was scored before DB writes were wired up
      - Force overwrite of any previously written results
    """
    client = CFBDClient()
    state = initialize_week(season_id, week)
    print(f"Finalize-only mode — season {season_id}, week {week}, "
          f"{len(state['live_game_ids'])} game(s) tracked.")
    finalize_week(state, client)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='CFB Fantasy live scoring engine')
    parser.add_argument('--season', type=int, required=True, help='season_id in DB')
    parser.add_argument('--week', type=int, required=True, help='Week number to score')
    parser.add_argument('--interval', type=int, default=300, help='Seconds between checkpoints (default 300)')
    parser.add_argument('--all-games', action='store_true',
                        help='Include full scoreboard in each checkpoint JSON')
    parser.add_argument('--finalize-only', action='store_true',
                        help='Skip live polling; fetch final scores from CFBD and write to DB. '
                             'Use after a score correction or to rerun a past week.')
    args = parser.parse_args()

    if args.finalize_only:
        run_finalize_only(season_id=args.season, week=args.week)
    else:
        run_scheduler(
            season_id=args.season,
            week=args.week,
            interval_seconds=args.interval,
            include_all_games=args.all_games,
        )
