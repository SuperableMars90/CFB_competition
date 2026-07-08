"""
lib/metagame.py
---------------
Metagame score tabulation for the CFB Fantasy Game.

Sits on top of lib/scoring.py: takes the raw per-player weekly point totals
produced by compute_player_totals() and converts them into standings, match
play points, PVP results, and a cumulative season leaderboard.

Pure data layer — no file I/O, no Streamlit.  Importable from the scoring
engine, a Jupyter notebook, or a commissioner tool.
"""

from __future__ import annotations

import json
from typing import Optional

import pandas as pd

from lib.db import get_connection


# ------------------------------------------------------------------
# Scoring context helpers
# ------------------------------------------------------------------

def get_scoring_context(season_id: int) -> dict:
    """
    Return the active scoring context for a season.

    Raises ValueError if none is configured (seed scoring_contexts first).
    Returns dict with keys: id, name, scoring_type, points_map (parsed list).
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, name, scoring_type, points_map
            FROM scoring_contexts
            WHERE season_id = %s AND is_active = 1
            LIMIT 1
            """,
            (season_id,),
        )
        row = cur.fetchone()
        cur.close()

    if row is None:
        raise ValueError(
            f"No active scoring context for season_id={season_id}. "
            "Seed the scoring_contexts table first."
        )

    row['points_map'] = json.loads(row['points_map']) if row['points_map'] else []
    return row


def _points_for_rank(points_map: list[int], rank: int) -> int:
    """
    Look up match play points for a 1-based rank.
    Returns 0 if rank exceeds the map length.
    """
    idx = rank - 1
    if 0 <= idx < len(points_map):
        return points_map[idx]
    return 0


# ------------------------------------------------------------------
# Weekly standings
# ------------------------------------------------------------------

def compute_weekly_standings(
    season_id: int,
    week: int,
    player_totals: dict[int, dict],
    pod_of_player: Optional[dict[int, int]] = None,
) -> list[dict]:
    """
    Assign pod_rank, overall_rank, and match_play_points to each player
    for the given week.

    player_totals is the dict returned by lib.scoring.compute_player_totals():
      {player_id: {player_name, p4, g6, wildcard, total, slots}}

    Single-pod (scoring_type == 'match_play'): pod_rank == overall_rank,
    match_play_points from ctx['points_map'] indexed by rank -- unchanged
    from the original single-pod behavior.

    Two-pod (scoring_type == 'match_play_pod'): delegates to
    compute_pod_standings() -- requires pod_of_player (raises ValueError
    otherwise). See that function for the cross-pod formula.

    Returns a list of result dicts sorted by total descending:
      {player_id, player_name, week, season_id, scoring_context_id,
       total_points, pod_rank, overall_rank, match_play_points}
      (plus base_points/pod_bonus/pod_vs_pod_bonus/overall_bonus for the
      match_play_pod case)
    """
    ctx = get_scoring_context(season_id)

    if ctx['scoring_type'] == 'match_play_pod':
        if pod_of_player is None:
            raise ValueError(
                "pod_of_player is required for a match_play_pod scoring context "
                f"(season_id={season_id})"
            )
        return compute_pod_standings(week, season_id, ctx, player_totals, pod_of_player)

    points_map = ctx['points_map']

    sorted_players = sorted(
        player_totals.items(),
        key=lambda item: item[1]['total'],
        reverse=True,
    )

    results = []
    for rank, (player_id, data) in enumerate(sorted_players, start=1):
        mp = _points_for_rank(points_map, rank)
        results.append({
            'player_id': player_id,
            'player_name': data['player_name'],
            'week': week,
            'season_id': season_id,
            'scoring_context_id': ctx['id'],
            'total_points': data['total'],
            'pod_rank': rank,
            'overall_rank': rank,
            'match_play_points': mp,
        })

    return results


def compute_pod_standings(
    week: int,
    season_id: int,
    ctx: dict,
    player_totals: dict[int, dict],
    pod_of_player: dict[int, int],
) -> list[dict]:
    """
    Pure (no DB) -- the cross-pod match-play formula for exactly 2 pods.
    ctx is an already-fetched scoring context dict (needs 'id' and
    'points_map'; points_map is the base ladder, e.g. [7,6,5,4,3,2,1,0],
    indexed by overall_rank).

    match_play_points = base_points + pod_bonus + pod_vs_pod_bonus + overall_bonus:
      - base_points: points_map looked up by overall_rank (rank across
        ALL players in player_totals, not per-pod).
      - pod_bonus: +1 to the player with the highest total_points within
        their own pod this week (pod_rank == 1).
      - pod_vs_pod_bonus: +1 to every player in whichever pod has the
        higher combined weekly total_points. Assumption, flagged not
        silently decided: an exact tie between the two pods' combined
        totals awards 0 to both, rather than picking a winner some other
        way -- Zach's spec didn't cover this case.
      - overall_bonus: +1 to the single player with overall_rank == 1.

    Ties within a pod (or for the overall #1 spot) break the same way
    the single-pod formula already does -- by sort stability, not an
    explicit tiebreak rule -- since Zach's spec didn't ask for a
    different behavior here either.

    Raises ValueError if pod_of_player doesn't resolve to exactly 2
    distinct pods among player_totals' players (mirrors
    lib.playoffs.assign_seeds_8_team's same 2-pod assumption).
    """
    points_map = ctx['points_map']

    pods_present = {pod_of_player[pid] for pid in player_totals if pid in pod_of_player}
    if len(pods_present) != 2:
        raise ValueError(
            f"compute_pod_standings requires exactly 2 pods among player_totals, "
            f"got {len(pods_present)}"
        )

    sorted_overall = sorted(
        player_totals.items(),
        key=lambda item: item[1]['total'],
        reverse=True,
    )
    overall_rank = {player_id: rank for rank, (player_id, _) in enumerate(sorted_overall, start=1)}

    pod_rank: dict[int, int] = {}
    pod_totals: dict[int, int] = {pod_id: 0 for pod_id in pods_present}
    for pod_id in pods_present:
        pod_members = sorted(
            (item for item in player_totals.items() if pod_of_player[item[0]] == pod_id),
            key=lambda item: item[1]['total'],
            reverse=True,
        )
        for rank, (player_id, data) in enumerate(pod_members, start=1):
            pod_rank[player_id] = rank
            pod_totals[pod_id] += data['total']

    winning_pod = None
    if len(set(pod_totals.values())) > 1:
        winning_pod = max(pod_totals, key=pod_totals.get)

    results = []
    for player_id, data in player_totals.items():
        base = _points_for_rank(points_map, overall_rank[player_id])
        pod_bonus = 1 if pod_rank[player_id] == 1 else 0
        pod_vs_pod_bonus = 1 if pod_of_player[player_id] == winning_pod else 0
        overall_bonus = 1 if overall_rank[player_id] == 1 else 0
        results.append({
            'player_id': player_id,
            'player_name': data['player_name'],
            'week': week,
            'season_id': season_id,
            'scoring_context_id': ctx['id'],
            'total_points': data['total'],
            'pod_rank': pod_rank[player_id],
            'overall_rank': overall_rank[player_id],
            'base_points': base,
            'pod_bonus': pod_bonus,
            'pod_vs_pod_bonus': pod_vs_pod_bonus,
            'overall_bonus': overall_bonus,
            'match_play_points': base + pod_bonus + pod_vs_pod_bonus + overall_bonus,
        })

    results.sort(key=lambda r: r['total_points'], reverse=True)
    return results


# ------------------------------------------------------------------
# DB writes
# ------------------------------------------------------------------

def write_weekly_results(standings: list[dict]) -> None:
    """
    Upsert a list of standing dicts (from compute_weekly_standings) into
    weekly_results.  Safe to call multiple times; last write wins.

    base_points/pod_bonus/pod_vs_pod_bonus/overall_bonus are only present
    on match_play_pod standings (compute_pod_standings) -- written as
    NULL for the single-pod match_play case via .get(), same as before.
    """
    if not standings:
        return

    sql = """
        INSERT INTO weekly_results
            (player_id, season_id, scoring_context_id, week,
             total_points, match_play_points, pod_rank, overall_rank,
             base_points, pod_bonus, pod_vs_pod_bonus, overall_bonus)
        VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            total_points      = VALUES(total_points),
            match_play_points = VALUES(match_play_points),
            pod_rank          = VALUES(pod_rank),
            overall_rank      = VALUES(overall_rank),
            base_points       = VALUES(base_points),
            pod_bonus         = VALUES(pod_bonus),
            pod_vs_pod_bonus  = VALUES(pod_vs_pod_bonus),
            overall_bonus     = VALUES(overall_bonus),
            computed_at       = CURRENT_TIMESTAMP
    """
    rows = [
        (
            s['player_id'],
            s['season_id'],
            s['scoring_context_id'],
            s['week'],
            s['total_points'],
            s['match_play_points'],
            s['pod_rank'],
            s['overall_rank'],
            s.get('base_points'),
            s.get('pod_bonus'),
            s.get('pod_vs_pod_bonus'),
            s.get('overall_bonus'),
        )
        for s in standings
    ]

    with get_connection() as conn:
        cur = conn.cursor()
        cur.executemany(sql, rows)
        conn.commit()
        cur.close()


# ------------------------------------------------------------------
# PVP matchup resolution
# ------------------------------------------------------------------

def compute_pvp_resolutions(
    pairings: list[dict],
    player_totals: dict[int, dict],
) -> list[dict]:
    """
    Pure computation, no DB access. `pairings`: [{id, player_a_id,
    player_b_id}, ...]. Returns [{pairing_id, winner_id, is_tie}, ...] --
    winner_id is None when is_tie is True. Pairings whose totals aren't
    (both) present in player_totals are skipped (not included in the
    result) rather than guessed at.
    """
    resolutions = []
    for pairing in pairings:
        a_id = pairing['player_a_id']
        b_id = pairing['player_b_id']
        a_total = player_totals.get(a_id, {}).get('total')
        b_total = player_totals.get(b_id, {}).get('total')

        if a_total is None or b_total is None:
            continue

        if a_total == b_total:
            resolutions.append({'pairing_id': pairing['id'], 'winner_id': None, 'is_tie': True})
        else:
            winner_id = a_id if a_total > b_total else b_id
            resolutions.append({'pairing_id': pairing['id'], 'winner_id': winner_id, 'is_tie': False})

    return resolutions


def write_pvp_resolutions(resolutions: list[dict]) -> int:
    """Persist compute_pvp_resolutions() output. Returns rows written."""
    if not resolutions:
        return 0

    with get_connection() as conn:
        cur = conn.cursor()
        for r in resolutions:
            cur.execute(
                "UPDATE matchup_pairings SET winner_id = %s, is_tie = %s WHERE id = %s",
                (r['winner_id'], r['is_tie'], r['pairing_id']),
            )
        conn.commit()
        cur.close()

    return len(resolutions)


def resolve_pvp_matchups(
    season_id: int,
    week: int,
    player_totals: dict[int, dict],
) -> int:
    """
    For each matchup_pairing in this (season, week) not yet resolved
    (winner_id IS NULL AND is_tie = FALSE), compare both players' totals
    and set winner_id to the higher scorer, or is_tie = TRUE on an exact
    tie. A confirmed tie is now distinguishable from "not yet played" --
    is_tie explicitly marks it, rather than leaving winner_id NULL for
    both cases as before.

    Returns the count of matchups resolved (wins and ties both count).
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, player_a_id, player_b_id
            FROM matchup_pairings
            WHERE season_id = %s AND week = %s
              AND winner_id IS NULL AND is_tie = FALSE
            """,
            (season_id, week),
        )
        pairings = cur.fetchall()
        cur.close()

    resolutions = compute_pvp_resolutions(pairings, player_totals)
    return write_pvp_resolutions(resolutions)


# ------------------------------------------------------------------
# Season leaderboard
# ------------------------------------------------------------------

def build_season_leaderboard(season_id: int) -> pd.DataFrame:
    """
    Build cumulative standings from weekly_results for a season.

    Aggregates all recorded weeks.  Match play points are the primary sort
    key; total_points (cumulative net margin) is the tiebreaker.

    Columns: Rank | Player | PlayerId | Weeks | MatchPlayPts | TotalPts
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                p.id                             AS player_id,
                p.name                          AS player_name,
                COUNT(wr.week)                  AS weeks_played,
                SUM(wr.match_play_points)       AS match_play_pts,
                SUM(wr.total_points)            AS total_pts
            FROM weekly_results wr
            JOIN players p ON p.id = wr.player_id
            WHERE wr.season_id = %s
            GROUP BY wr.player_id, p.name
            ORDER BY match_play_pts DESC, total_pts DESC
            """,
            (season_id,),
        )
        rows = cur.fetchall()
        cur.close()

    if not rows:
        return pd.DataFrame(columns=['Rank', 'Player', 'PlayerId', 'Weeks', 'MatchPlayPts', 'TotalPts'])

    df = pd.DataFrame(rows).rename(columns={
        'player_id': 'PlayerId',
        'player_name': 'Player',
        'weeks_played': 'Weeks',
        'match_play_pts': 'MatchPlayPts',
        'total_pts': 'TotalPts',
    })
    df.insert(0, 'Rank', range(1, len(df) + 1))
    return df


def build_weekly_breakdown(season_id: int) -> pd.DataFrame:
    """
    Return a week-by-week score matrix for the season.

    Columns: Player | Wk1_pts | Wk1_rank | Wk2_pts | Wk2_rank | ...
    Useful for a detailed standings page or export.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                p.name          AS player_name,
                wr.week,
                wr.total_points,
                wr.pod_rank,
                wr.match_play_points
            FROM weekly_results wr
            JOIN players p ON p.id = wr.player_id
            WHERE wr.season_id = %s
            ORDER BY p.name, wr.week
            """,
            (season_id,),
        )
        rows = cur.fetchall()
        cur.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    pivot_pts = df.pivot(index='player_name', columns='week', values='total_points')
    pivot_mp = df.pivot(index='player_name', columns='week', values='match_play_points')

    pivot_pts.columns = [f'Wk{w}_Pts' for w in pivot_pts.columns]
    pivot_mp.columns = [f'Wk{w}_MP' for w in pivot_mp.columns]

    # Interleave pts and mp columns by week
    combined = pd.concat([pivot_pts, pivot_mp], axis=1)
    weeks = sorted(df['week'].unique())
    col_order = [col for w in weeks for col in (f'Wk{w}_Pts', f'Wk{w}_MP')]
    combined = combined[col_order].reset_index().rename(columns={'player_name': 'Player'})
    return combined
