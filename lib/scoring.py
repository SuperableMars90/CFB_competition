"""
lib/scoring.py
--------------
Core scoring logic for the CFB Fantasy Game.

Pure data layer — no file I/O, no API calls, no Streamlit.
Importable from scripts or a Jupyter notebook for ad-hoc analysis:

    from lib.scoring import load_week_picks, score_picks, compute_player_totals
"""

from __future__ import annotations

from typing import Optional
import pandas as pd

from lib.db import get_connection


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _slot_group(slot_type: str, conference_tier: Optional[str]) -> str:
    """Return 'p4', 'g6', or 'wildcard' for a given slot."""
    if slot_type == 'wildcard':
        return 'wildcard'
    if slot_type == 'p4_flex':
        return 'p4'
    if slot_type == 'g6_flex':
        return 'g6'
    if slot_type == 'conference':
        return 'p4' if conference_tier == 'P4' else 'g6'
    return 'other'


def _net_margin(
    team_id: int,
    home_team_id: int,
    away_team_id: int,
    home_points: Optional[int],
    away_points: Optional[int],
) -> Optional[int]:
    """
    Net margin from the picked team's perspective.
    Positive = they are winning/won; negative = losing/lost.
    Returns None when scores are not yet available (game not started).
    """
    if home_points is None or away_points is None:
        return None
    if team_id == home_team_id:
        return home_points - away_points
    if team_id == away_team_id:
        return away_points - home_points
    return None


def _picked_and_opp_score(
    team_id: int,
    home_team_id: int,
    away_team_id: int,
    home_points: Optional[int],
    away_points: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """
    (picked team's points, opponent's points) from the picked team's
    perspective. Returns (None, None) when scores aren't available yet.
    Used by lib.records for offense/defense-style records that need the
    raw points, not just the net margin.
    """
    if home_points is None or away_points is None:
        return None, None
    if team_id == home_team_id:
        return home_points, away_points
    if team_id == away_team_id:
        return away_points, home_points
    return None, None


# ------------------------------------------------------------------
# DB loading
# ------------------------------------------------------------------

def load_week_picks(season_id: int, week: int) -> list[dict]:
    """
    Load all lineup slots for the given (season, week) from the DB.

    Each returned dict has everything needed for scoring:
      - player info, team info, slot classification
      - cfbd_game_id: the CFBD game id to match against scoreboard data
      - is_week0: True → scores come from DB (historical); False → live API
      - For week 0 picks, w0_home_score / w0_away_score are pre-populated.
      - is_pass: True → slot was deliberately passed (scores 0)
    """
    query = """
        SELECT
            ls.id                               AS slot_id,
            wl.player_id,
            p.name                              AS player_name,
            ls.team_id,
            t.name_display                      AS team_name,
            ls.slot_type,
            ls.conference_slug,
            ls.game_id                          AS w0_internal_game_id,
            -- Use team's conference tier when available; fall back to the slot's
            -- conference entry (covers passed conference slots where team_id is NULL)
            COALESCE(c.tier, c_slot.tier)       AS conference_tier,
            COALESCE(c.abbreviation, ls.conference_slug) AS conference_abbr,
            -- Week 0 game fields (populated only when ls.game_id IS NOT NULL)
            g_w0.cfbd_game_id                   AS w0_cfbd_game_id,
            g_w0.home_team_id                   AS w0_home_team_id,
            g_w0.away_team_id                   AS w0_away_team_id,
            g_w0.home_score                     AS w0_home_score,
            g_w0.away_score                     AS w0_away_score,
            -- Regular game fields (populated only when ls.game_id IS NULL)
            g_reg.cfbd_game_id                  AS reg_cfbd_game_id,
            g_reg.home_team_id                  AS reg_home_team_id,
            g_reg.away_team_id                  AS reg_away_team_id
        FROM lineup_slots ls
        JOIN weekly_lineups wl ON wl.id = ls.lineup_id
        JOIN players p ON p.id = wl.player_id
        -- Team and its conference (NULL for passed slots)
        LEFT JOIN teams t ON t.id = ls.team_id
        LEFT JOIN conferences c ON c.id = t.conference_id
        -- Fallback conference lookup for passed conference slots
        LEFT JOIN conferences c_slot
            ON ls.slot_type = 'conference'
            AND c_slot.abbreviation = ls.conference_slug
            AND c_slot.season_id = wl.season_id
        -- Week 0 game (when lineup_slots.game_id points to a historical game)
        LEFT JOIN games g_w0 ON g_w0.id = ls.game_id
        -- Current-week game via schedule view (when game_id IS NULL)
        LEFT JOIN v_team_weekly_schedule v
            ON ls.game_id IS NULL
            AND v.team_id = ls.team_id
            AND v.season_id = wl.season_id
            AND v.week = wl.week
        LEFT JOIN games g_reg
            ON ls.game_id IS NULL
            AND g_reg.id = v.game_id
        WHERE wl.season_id = %s AND wl.week = %s
        ORDER BY wl.player_id, ls.id
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, week))
        rows = cur.fetchall()
        cur.close()

    slots = []
    for row in rows:
        is_pass = row['team_id'] is None
        is_week0 = row['w0_internal_game_id'] is not None

        if is_week0:
            cfbd_game_id = row['w0_cfbd_game_id']
            home_team_id = row['w0_home_team_id']
            away_team_id = row['w0_away_team_id']
            w0_home_score = row['w0_home_score']
            w0_away_score = row['w0_away_score']
        else:
            cfbd_game_id = row['reg_cfbd_game_id']
            home_team_id = row['reg_home_team_id']
            away_team_id = row['reg_away_team_id']
            w0_home_score = None
            w0_away_score = None

        group = _slot_group(row['slot_type'], row['conference_tier'])

        slots.append({
            'slot_id': row['slot_id'],
            'player_id': row['player_id'],
            'player_name': row['player_name'],
            'team_id': row['team_id'],
            'team_name': row['team_name'],
            'conference_tier': row['conference_tier'],
            'conference_abbr': row['conference_abbr'],
            'slot_type': row['slot_type'],
            'conference_slug': row['conference_slug'],
            'is_pass': is_pass,
            'is_week0': is_week0,
            'cfbd_game_id': cfbd_game_id,
            'home_team_id': home_team_id,
            'away_team_id': away_team_id,
            'w0_home_score': w0_home_score,
            'w0_away_score': w0_away_score,
            'group': group,
        })
    return slots


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def score_picks(
    week_picks: list[dict],
    live_scores: dict[int, dict],
) -> list[dict]:
    """
    Merge live scoreboard data onto pre-loaded picks.

    live_scores maps cfbd_game_id -> {status, home_points, away_points}.
    Week 0 picks use their stored DB scores and are never looked up in
    live_scores (those games are historical and already final).

    Returns a new list of dicts (original pick data + scored fields added):
      margin:       int | None   net points for the picked team; None = not scored
      picked_score: int | None   raw points scored by the picked team
      opp_score:    int | None   raw points scored by the opponent
      game_status:  str | None   'scheduled', 'in_progress', 'final', None
    """
    scored = []
    for pick in week_picks:
        p = dict(pick)

        if p['is_pass']:
            p['margin'] = 0
            p['picked_score'] = None
            p['opp_score'] = None
            p['game_status'] = None
        elif p['is_week0']:
            # Historical game — score from DB, always treat as final
            p['margin'] = _net_margin(
                p['team_id'], p['home_team_id'], p['away_team_id'],
                p['w0_home_score'], p['w0_away_score'],
            )
            p['picked_score'], p['opp_score'] = _picked_and_opp_score(
                p['team_id'], p['home_team_id'], p['away_team_id'],
                p['w0_home_score'], p['w0_away_score'],
            )
            p['game_status'] = 'final' if p['margin'] is not None else None
        else:
            game = live_scores.get(p['cfbd_game_id']) if p['cfbd_game_id'] else None
            if game is None:
                # Team on bye or game not on scoreboard
                p['margin'] = None
                p['picked_score'] = None
                p['opp_score'] = None
                p['game_status'] = None
            else:
                p['game_status'] = game.get('status')
                p['margin'] = _net_margin(
                    p['team_id'], p['home_team_id'], p['away_team_id'],
                    game.get('home_points'), game.get('away_points'),
                )
                p['picked_score'], p['opp_score'] = _picked_and_opp_score(
                    p['team_id'], p['home_team_id'], p['away_team_id'],
                    game.get('home_points'), game.get('away_points'),
                )

        scored.append(p)
    return scored


# ------------------------------------------------------------------
# Totals and output
# ------------------------------------------------------------------

def compute_player_totals(scored_picks: list[dict]) -> dict[int, dict]:
    """
    Aggregate scored picks into per-player subtotals.

    Returns {player_id: {player_name, p4, g6, wildcard, total, slots}}.
    None margins (game not yet scored) count as 0 in subtotals.
    """
    players: dict[int, dict] = {}

    for pick in scored_picks:
        pid = pick['player_id']
        if pid not in players:
            players[pid] = {
                'player_name': pick['player_name'],
                'p4': 0,
                'g6': 0,
                'wildcard': 0,
                'total': 0,
                'slots': [],
            }

        margin = pick['margin'] if pick['margin'] is not None else 0
        group = pick['group']

        if group == 'p4':
            players[pid]['p4'] += margin
        elif group == 'g6':
            players[pid]['g6'] += margin
        elif group == 'wildcard':
            players[pid]['wildcard'] += margin

        players[pid]['total'] += margin
        players[pid]['slots'].append(pick)

    return players


def build_score_table(player_totals: dict[int, dict]) -> pd.DataFrame:
    """
    Build a human-readable summary DataFrame suitable for printing or display.

    Columns: Player | P4 | G6 | Wildcard | Total
    Sorted by Total descending.
    """
    rows = []
    for pid, data in player_totals.items():
        rows.append({
            'Player': data['player_name'],
            'P4': data['p4'],
            'G6': data['g6'],
            'Wildcard': data['wildcard'],
            'Total': data['total'],
        })
    df = pd.DataFrame(rows).sort_values('Total', ascending=False).reset_index(drop=True)
    return df
