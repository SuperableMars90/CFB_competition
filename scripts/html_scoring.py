"""
scripts/html_scoring.py
-----------------------
Generates HTML score content for the CFB Fantasy Game — both the local
standalone live page and reusable fragments for pushing to WordPress
(see scripts/wordpress_client.py).

Public entry points:
  generate_week_page(season_id, week)                 -- local file, week start
  update_week_page(season_id, week, live_scores)       -- local file, each checkpoint
  render_fragment(season_id, week, live_scores)        -- WP-postable fragment (live)
  render_recap_fragment(season_id, week, standings,
                         leaderboard, live_scores)      -- WP-postable fragment (recap)
  write_final_page(season_id, week, standings,
                    leaderboard, live_scores)           -- local file, T13

Local file output: output/web/week_N_live.html, output/web/week_N_final.html

The *_fragment() functions return HTML with no <!DOCTYPE>/<html>/<head>/
<body> wrapper (but the inline <style> block IS included, so each fragment
is still fully self-contained/styled) — that's what makes them safe to
embed directly in a WordPress post body. The local-file writers wrap the
same fragment in a minimal doc shell.

live_scores format mirrors fetch_live_scores() / _fetch_final_scores():
  {cfbd_game_id: {status, home_points, away_points, period, clock, ...}}
Pass an empty dict when no live/final data is available yet (all-pending).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.db import (
    get_connection,
    get_conference_slot_tiers,
    get_lineup_team_ids,
    get_matchups_for_week,
    get_pick_results_by_player,
    get_pod_membership_map,
    get_pod_vs_pod_bonus_by_week,
    get_pods,
)
from lib.scoring import _slot_group
from lib.metagame import get_scoring_context, compute_weekly_standings, build_season_leaderboard
from lib.seeding import get_pvp_regular_season_records, record_and_margin
from lib.optimal_lineup import P4_FLEX_CATEGORY, G6_FLEX_CATEGORY, WILDCARD_CATEGORY
from lib.performance import compute_max_optimal
from lib.recap import (
    build_overall_results,
    build_pod_vs_pod_summary,
    build_player_recap,
    build_pvp_matchups_table,
    build_scrappy_by_pod,
    build_top_free_agents,
    build_top_unplayed_winners,
    tally_pick_record,
)

OUTPUT_DIR = Path(__file__).parent.parent / 'output' / 'web'
ESPN_URL = 'https://www.espn.com/college-football/game/_/gameId/{}'


# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _format_kickoff(game_time) -> str:
    """Convert a DB game_time (timedelta from mysql-connector) to '7:30 PM ET'."""
    if game_time is None:
        return 'TBD'
    total_secs = int(game_time.total_seconds())
    hours, rem = divmod(total_secs, 3600)
    minutes = rem // 60
    period = 'AM' if hours < 12 else 'PM'
    hours = hours % 12 or 12
    return f'{hours}:{minutes:02d} {period} ET'


def _slot_label(slot_type: str, conference_slug: Optional[str]) -> str:
    if slot_type == 'conference':
        return conference_slug or '—'
    if slot_type == 'p4_flex':
        return 'P4 Flex'
    if slot_type == 'g6_flex':
        return 'G6 Flex'
    if slot_type == 'wildcard':
        return 'WC'
    return '—'


def _margin_fmt(margin: Optional[int]) -> str:
    if margin is None:
        return '—'
    return f'+{margin}' if margin > 0 else str(margin)


def _margin_color(margin: Optional[int]) -> str:
    if margin is None or margin == 0:
        return 'inherit'
    return '#2a7a2a' if margin > 0 else '#cc0000'


def _score_cell(picked: Optional[int], opp: Optional[int], cfbd_game_id: Optional[int]) -> str:
    """HTML for the score cell: a linked 'X–Y'."""
    p = picked if picked is not None else 0
    o = opp if opp is not None else 0
    score_str = f'{p}–{o}'
    if cfbd_game_id:
        return f'<a href="{ESPN_URL.format(cfbd_game_id)}" target="_blank">{score_str}</a>'
    return score_str


# ------------------------------------------------------------------
# DB loading
# ------------------------------------------------------------------

def _load_picks_with_display(season_id: int, week: int) -> list[dict]:
    """
    Load all lineup slots for the week with opponent names, ESPN IDs, and
    kickoff times joined from the games table.  Similar to load_week_picks()
    but adds display-only fields (opponent name_display, game_time).
    """
    query = """
        SELECT
            ls.id                                               AS slot_id,
            wl.player_id,
            p.name                                              AS player_name,
            ls.team_id,
            t.name_display                                      AS team_name,
            ls.slot_type,
            ls.conference_slug,
            (ls.game_id IS NOT NULL)                            AS is_week0,
            COALESCE(c.tier, c_slot.tier)                       AS conference_tier,
            COALESCE(g_w0.cfbd_game_id, g_reg.cfbd_game_id)    AS cfbd_game_id,
            COALESCE(g_w0.home_team_id, g_reg.home_team_id)    AS home_team_id,
            COALESCE(g_w0.away_team_id, g_reg.away_team_id)    AS away_team_id,
            COALESCE(g_w0.home_score,   g_reg.home_score)      AS home_score,
            COALESCE(g_w0.away_score,   g_reg.away_score)      AS away_score,
            COALESCE(g_w0.status,       g_reg.status)          AS db_status,
            COALESCE(g_w0.game_time,    g_reg.game_time)       AS game_time,
            ht.name_display                                     AS home_name,
            awt.name_display                                    AS away_name
        FROM lineup_slots ls
        JOIN weekly_lineups wl  ON wl.id  = ls.lineup_id
        JOIN players p          ON p.id   = wl.player_id
        LEFT JOIN teams t       ON t.id   = ls.team_id
        LEFT JOIN conferences c ON c.id   = t.conference_id
        LEFT JOIN conferences c_slot
            ON ls.slot_type = 'conference'
            AND c_slot.abbreviation = ls.conference_slug
            AND c_slot.season_id    = wl.season_id
        LEFT JOIN games g_w0    ON g_w0.id = ls.game_id
        LEFT JOIN v_team_weekly_schedule v
            ON ls.game_id IS NULL
            AND v.team_id    = ls.team_id
            AND v.season_id  = wl.season_id
            AND v.week       = wl.week
        LEFT JOIN games g_reg   ON ls.game_id IS NULL AND g_reg.id = v.game_id
        LEFT JOIN teams ht      ON ht.id  = COALESCE(g_w0.home_team_id, g_reg.home_team_id)
        LEFT JOIN teams awt     ON awt.id = COALESCE(g_w0.away_team_id, g_reg.away_team_id)
        WHERE wl.season_id = %s AND wl.week = %s
        ORDER BY wl.player_id, ls.id
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, week))
        rows = cur.fetchall()
        cur.close()
    return rows


def _load_prior_season_totals(season_id: int, week: int) -> dict[int, int]:
    """
    Sum total_points from weekly_results for all weeks BEFORE the given week.
    Returns {player_id: cumulative_total}.  Zero for players with no prior weeks.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT p.id AS player_id,
                   COALESCE(SUM(wr.total_points), 0) AS prior_total
            FROM players p
            LEFT JOIN weekly_results wr
                ON wr.player_id = p.id
                AND wr.season_id = %s
                AND wr.week < %s
            GROUP BY p.id
            """,
            (season_id, week),
        )
        rows = cur.fetchall()
        cur.close()
    return {r['player_id']: int(r['prior_total']) for r in rows}


# ------------------------------------------------------------------
# Slot resolution
# ------------------------------------------------------------------

def _build_slot(row: dict, live_scores: dict) -> dict:
    """
    Merge one DB row with live_scores data into a fully-resolved slot dict
    ready for rendering.
    """
    is_pass = row['team_id'] is None
    is_week0 = bool(row['is_week0'])
    cfbd_game_id = row.get('cfbd_game_id')
    conference_tier = row.get('conference_tier')
    group = _slot_group(row['slot_type'], conference_tier)

    if is_pass:
        return {
            'label': _slot_label(row['slot_type'], row['conference_slug']),
            'group': group,
            'team': 'PASS',
            'is_pass': True,
            'is_week0': False,
            'opponent': None,
            'is_home': None,
            'cfbd_game_id': None,
            'game_status': None,
            'game_time': None,
            'picked_score': None,
            'opp_score': None,
            'margin': 0,
        }

    team_id = row['team_id']
    home_id = row['home_team_id']

    opponent = row['away_name'] if home_id == team_id else row['home_name']

    # live_scores takes priority over DB (DB scores may be stale during game day)
    live = live_scores.get(cfbd_game_id) if cfbd_game_id else None
    if live:
        game_status = live.get('status')
        hp = live.get('home_points')
        ap = live.get('away_points')
    else:
        game_status = row.get('db_status')
        hp = row.get('home_score')
        ap = row.get('away_score')

    if hp is not None and ap is not None:
        picked_score = hp if home_id == team_id else ap
        opp_score = ap if home_id == team_id else hp
        margin = picked_score - opp_score
    else:
        picked_score = opp_score = margin = None

    return {
        'label': _slot_label(row['slot_type'], row['conference_slug']),
        'group': group,
        'team': row['team_name'],
        'is_pass': False,
        'is_week0': is_week0,
        'opponent': opponent,
        'is_home': home_id == team_id,
        'cfbd_game_id': cfbd_game_id,
        'game_status': game_status,
        'game_time': row.get('game_time'),
        'picked_score': picked_score,
        'opp_score': opp_score,
        'margin': margin,
    }


def _game_counts(slots: list[dict]) -> dict:
    final_pass = sum(1 for s in slots if s['is_pass'] or s['game_status'] == 'final')
    in_progress = sum(1 for s in slots if s['game_status'] == 'in_progress')
    pending = len(slots) - final_pass - in_progress
    pass_count = sum(1 for s in slots if s['is_pass'])
    return {
        'final_pass': final_pass,
        'in_progress': in_progress,
        'pending': pending,
        'pass_count': pass_count,
    }


# ------------------------------------------------------------------
# HTML rendering
# ------------------------------------------------------------------

def _status_display(slot: dict) -> str:
    gs = slot['game_status']
    if gs == 'final':
        return 'FINAL'
    if gs == 'in_progress':
        return 'In Progress'
    return f'pending {_format_kickoff(slot["game_time"])}'


def _render_slot_row(slot: dict) -> str:
    label = slot['label']
    is_pass = slot['is_pass']
    team_display = slot['team']
    if slot['is_week0']:
        team_display += ' <span class="w0-badge">W0</span>'

    if is_pass:
        return (
            f'<tr>'
            f'<td class="slot-label">{label}</td>'
            f'<td class="team-name">PASS</td>'
            f'<td class="score">—</td>'
            f'<td class="opponent">—</td>'
            f'<td class="status">—</td>'
            f'<td class="margin">0</td>'
            f'</tr>'
        )

    score_html = _score_cell(slot['picked_score'], slot['opp_score'], slot['cfbd_game_id'])
    opponent = slot['opponent'] or '—'
    status = _status_display(slot)
    margin = _margin_fmt(slot['margin'])
    color = _margin_color(slot['margin'])

    return (
        f'<tr>'
        f'<td class="slot-label">{label}</td>'
        f'<td class="team-name">{team_display}</td>'
        f'<td class="score">{score_html}</td>'
        f'<td class="opponent">{opponent}</td>'
        f'<td class="status">{status}</td>'
        f'<td class="margin" style="color:{color}">{margin}</td>'
        f'</tr>'
    )


def _render_slot_group(title: str, slots: list[dict]) -> str:
    if not slots:
        return ''
    count = len(slots)
    noun = 'slot' if count == 1 else 'slots'
    rows = '\n        '.join(_render_slot_row(s) for s in slots)
    return f"""    <h3>{title} ({count} {noun})</h3>
    <table class="slot-table">
      <thead><tr>
        <th>Slot</th><th>Team</th><th>Score</th>
        <th>Opponent</th><th>Status</th><th>Margin</th>
      </tr></thead>
      <tbody>
        {rows}
      </tbody>
    </table>"""


def _render_player_card(name: str, slots: list[dict], week_total: int, season_total: int) -> str:
    p4 = [s for s in slots if s['group'] == 'p4']
    g6 = [s for s in slots if s['group'] == 'g6']
    wc = [s for s in slots if s['group'] == 'wildcard']

    p4_sum = sum(s['margin'] or 0 for s in p4)
    g6_sum = sum(s['margin'] or 0 for s in g6)
    wc_sum = sum(s['margin'] or 0 for s in wc)

    anchor = name.lower().replace(' ', '-')
    ws = '+' if week_total >= 0 else ''
    ss = '+' if season_total >= 0 else ''

    return f"""  <div class="player-card" id="{anchor}">
    <h2>{name.upper()} <span class="player-totals">Week: {ws}{week_total}&nbsp;&nbsp;·&nbsp;&nbsp;Season: {ss}{season_total}</span></h2>
{_render_slot_group('P4', p4)}
{_render_slot_group('G6', g6)}
{_render_slot_group('Wildcard', wc)}
    <p class="subtotals">P4: {_margin_fmt(p4_sum)}&nbsp;&nbsp;·&nbsp;&nbsp;G6: {_margin_fmt(g6_sum)}&nbsp;&nbsp;·&nbsp;&nbsp;WC: {_margin_fmt(wc_sum)}&nbsp;&nbsp;·&nbsp;&nbsp;Week Total: {ws}{week_total}</p>
  </div>"""


# ------------------------------------------------------------------
# Shared inline styles — used by every fragment (live + recap), local
# file or WordPress push, so the whole site's generated pages look
# consistent. Kept as literal <style> (not a stylesheet link) since it
# needs to travel with the content wherever it's embedded.
# ------------------------------------------------------------------

_STYLE_BLOCK = """<style>
    body { font-family: sans-serif; max-width: 920px; margin: 0 auto; padding: 16px; color: #222; }
    .page-header { background: #1a1a2e; color: #fff; padding: 16px 20px; border-radius: 6px; margin-bottom: 24px; }
    .page-header h1 { margin: 0 0 4px; font-size: 1.4rem; }
    .page-header p { margin: 0; font-size: 0.85rem; opacity: 0.8; }
    h1.section { font-size: 1.2rem; margin: 28px 0 8px; border-bottom: 2px solid #1a1a2e; padding-bottom: 4px; }
    h2.section { font-size: 1rem; margin: 20px 0 6px; color: #555; border-bottom: 1px solid #ccc; padding-bottom: 3px; }
    .player-card { background: #f9f9f9; border: 1px solid #ddd; border-radius: 6px; padding: 16px 20px; margin-bottom: 24px; }
    .player-card h2 { margin: 0 0 14px; font-size: 1.05rem; display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1px solid #ddd; padding-bottom: 8px; }
    .player-totals { font-weight: normal; font-size: 0.88rem; color: #555; }
    .player-card h3 { margin: 14px 0 4px; font-size: 0.8rem; text-transform: uppercase; color: #888; letter-spacing: 0.06em; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 4px; font-size: 0.84rem; }
    thead th { background: #1a1a2e; color: #fff; padding: 5px 8px; text-align: left; font-weight: normal; }
    tbody tr:nth-child(even) td { background: #f0f0f0; }
    td { padding: 4px 8px; border-bottom: 1px solid #e8e8e8; vertical-align: middle; }
    tbody tr:last-child td { border-bottom: none; }
    .slot-label { font-weight: 600; width: 68px; white-space: nowrap; }
    .team-name { width: 150px; }
    .score { width: 65px; }
    .score a { color: #1a1a2e; font-weight: 600; text-decoration: none; }
    .score a:hover { text-decoration: underline; }
    .opponent { width: 150px; color: #555; }
    .status { width: 145px; color: #555; font-size: 0.8rem; }
    .margin { width: 55px; font-weight: 700; text-align: right; }
    .subtotals { font-size: 0.84rem; color: #444; margin: 12px 0 0; padding-top: 8px; border-top: 1px solid #ddd; }
    .w0-badge { font-size: 0.7rem; background: #e8a020; color: #fff; padding: 1px 4px; border-radius: 3px; vertical-align: middle; margin-left: 4px; }
    .pass-note { font-size: 0.78rem; color: #888; }
  </style>"""


def _pod_names(season_id: int) -> dict[int, str]:
    """{pod_id: pod_name} for this season."""
    return {p['id']: p['name'] for p in get_pods(season_id)}


def _live_standings(season_id: int, week: int, player_totals: dict[int, dict]) -> dict[int, dict]:
    """
    Wraps lib.metagame.compute_weekly_standings() for the LIVE status
    page: recomputed on every call off whatever's in player_totals right
    now (in-progress scores included), not just at finalize_week().
    Zach, 2026-07-05: match points should be live and understood as
    tentative until games finish, the same way the raw week totals
    already are -- not withheld until the week is final.

    Returns {player_id: standings_row}. Returns {} (not a raised error)
    when standings genuinely can't be computed yet -- no scoring context
    configured, or (match_play_pod seasons only) one whole pod has no
    lineups submitted this week at all, so compute_pod_standings can't
    form both sides of the pod-vs-pod comparison. A missing player_id
    means "not available yet", not "zero".
    """
    if not player_totals:
        return {}
    try:
        ctx = get_scoring_context(season_id)
    except ValueError:
        return {}

    if ctx['scoring_type'] == 'match_play_pod':
        pod_of_player = get_pod_membership_map(season_id)
        # compute_pod_standings assumes every player_totals key has a pod
        # assignment; scope down to the ones that actually do rather than
        # risk a KeyError on a not-yet-assigned player.
        scoped_totals = {pid: t for pid, t in player_totals.items() if pid in pod_of_player}
        if not scoped_totals:
            return {}
        try:
            standings = compute_weekly_standings(season_id, week, scoped_totals, pod_of_player)
        except ValueError:
            return {}
    else:
        standings = compute_weekly_standings(season_id, week, player_totals)

    return {s['player_id']: s for s in standings}


def _matchups_with_live_scores(
    season_id: int, week: int, scoring_context_id: int, player_totals: dict[int, dict],
) -> list[dict]:
    """
    This week's PVP matchups (lib.db.get_matchups_for_week), each
    annotated with both sides' live totals and a leader_id (None if
    either side hasn't submitted a lineup yet, or the two sides are
    currently tied) -- computed from player_totals directly rather than
    matchup_pairings.winner_id, which stays NULL until finalize_week()
    resolves it.
    """
    matchups = get_matchups_for_week(season_id, scoring_context_id, week)
    for m in matchups:
        a_total = player_totals.get(m['player_a_id'], {}).get('total')
        b_total = player_totals.get(m['player_b_id'], {}).get('total')
        m['player_a_total'] = a_total
        m['player_b_total'] = b_total
        if a_total is None or b_total is None or a_total == b_total:
            m['leader_id'] = None
        else:
            m['leader_id'] = m['player_a_id'] if a_total > b_total else m['player_b_id']
    return matchups


def _pvp_records_through_week(season_id: int, week: int, scoring_context_id: int) -> dict[int, dict]:
    """
    {player_id: {'w', 'l', 't', 'margin'}} -- season-cumulative PVP
    record through the last FINALIZED week. Reuses
    lib.seeding.get_pvp_regular_season_records()/record_and_margin() (built
    for playoff seeding) purely as data readers -- no tie-forcing or
    seeding logic is invoked here. Naturally excludes the current
    in-progress week (its matchup_pairings row has no weekly_results yet,
    so the inner join in get_pvp_regular_season_records skips it) --
    exactly the "settled through last week" behavior Zach asked for, with
    no extra filtering needed here.

    Depends on matchup_pairings.winner_id/is_tie already being resolved
    for every included week -- confirmed live: a row with winner_id still
    NULL reads as a loss for player_a regardless of the actual scores,
    not "unplayed" (get_pvp_regular_season_records's W/L branch only
    checks whether winner_id == player_a_id). This is never an issue in
    practice, since resolve_pvp_matchups() runs inside the same
    finalize_week() call that writes the week's weekly_results rows --
    by the time a week has weekly_results at all, its matchup_pairings
    row is already resolved too. Flagging it here because it's a real
    footgun if this function is ever called against hand-inserted or
    partially-written data.
    """
    all_records = get_pvp_regular_season_records(season_id, scoring_context_id, cutoff_week=week)
    return {
        pid: dict(zip(('w', 'l', 't', 'margin'), record_and_margin(recs)))
        for pid, recs in all_records.items()
    }


def _build_summaries(season_id: int, week: int, live_scores: dict) -> list[dict]:
    """
    Load this week's picks, merge with live_scores, and build one summary
    dict per player: name, anchor, p4/g6/wc margin sums, week/season
    totals, game-status counts, rendered card HTML, pod info, and live
    pod-aware match-play standings (see _live_standings()). Shared by
    both render_fragment() (live) and render_recap_fragment() (final) so
    the DB-loading/aggregation logic exists in exactly one place.
    """
    rows = _load_picks_with_display(season_id, week)
    prior_totals = _load_prior_season_totals(season_id, week)
    pod_of_player = get_pod_membership_map(season_id)
    pod_names = _pod_names(season_id)

    players: dict[int, dict] = {}
    for row in rows:
        pid = row['player_id']
        if pid not in players:
            players[pid] = {'name': row['player_name'], 'slots': []}
        players[pid]['slots'].append(_build_slot(row, live_scores))

    player_totals = {}
    partials = {}
    for pid, data in players.items():
        slots = data['slots']
        p4_sum = sum(s['margin'] or 0 for s in slots if s['group'] == 'p4')
        g6_sum = sum(s['margin'] or 0 for s in slots if s['group'] == 'g6')
        wc_sum = sum(s['margin'] or 0 for s in slots if s['group'] == 'wildcard')
        week_total = p4_sum + g6_sum + wc_sum
        player_totals[pid] = {'player_name': data['name'], 'total': week_total}
        partials[pid] = {'name': data['name'], 'slots': slots, 'p4': p4_sum, 'g6': g6_sum, 'wc': wc_sum, 'week': week_total}

    live_standings = _live_standings(season_id, week, player_totals)

    summaries = []
    for pid, p in partials.items():
        name = p['name']
        season_total = prior_totals.get(pid, 0) + p['week']
        standing = live_standings.get(pid)

        summaries.append({
            'player_id': pid,
            'name': name,
            'anchor': name.lower().replace(' ', '-'),
            'pod_id': pod_of_player.get(pid),
            'pod_name': pod_names.get(pod_of_player.get(pid)),
            'p4': p['p4'],
            'g6': p['g6'],
            'wc': p['wc'],
            'week': p['week'],
            'season': season_total,
            'pod_rank': standing['pod_rank'] if standing else None,
            'overall_rank': standing['overall_rank'] if standing else None,
            'match_pts': standing['match_play_points'] if standing else None,
            'base_pts': standing.get('base_points') if standing else None,
            'pod_bonus': standing.get('pod_bonus') if standing else None,
            'vs_pod_bonus': standing.get('pod_vs_pod_bonus') if standing else None,
            'overall_bonus': standing.get('overall_bonus') if standing else None,
            'counts': _game_counts(p['slots']),
            'slots': p['slots'],
            'card_html': _render_player_card(name, p['slots'], p['week'], season_total),
        })

    summaries.sort(key=lambda x: x['week'], reverse=True)
    return summaries


def _render_fragment_body(week: int, timestamp: str, summaries: list[dict]) -> str:
    """Style + header + standings + game-status + player cards. No outer <html> wrapper."""
    standing_rows = []
    for rank, ps in enumerate(summaries, start=1):
        ws = '+' if ps['week'] >= 0 else ''
        ss = '+' if ps['season'] >= 0 else ''
        p4s = '+' if ps['p4'] >= 0 else ''
        g6s = '+' if ps['g6'] >= 0 else ''
        wcs = '+' if ps['wc'] >= 0 else ''
        standing_rows.append(
            f'<tr><td>{rank}</td>'
            f'<td><a href="#{ps["anchor"]}">{ps["name"]}</a></td>'
            f'<td>{p4s}{ps["p4"]}</td><td>{g6s}{ps["g6"]}</td><td>{wcs}{ps["wc"]}</td>'
            f'<td>{ws}{ps["week"]}</td><td>{ss}{ps["season"]}</td></tr>'
        )

    status_rows = []
    for ps in summaries:
        c = ps['counts']
        pass_note = f' <span class="pass-note">(incl. {c["pass_count"]} pass)</span>' if c['pass_count'] else ''
        status_rows.append(
            f'<tr><td>{ps["name"]}</td>'
            f'<td>{c["final_pass"]}{pass_note}</td>'
            f'<td>{c["in_progress"]}</td>'
            f'<td>{c["pending"]}</td></tr>'
        )

    cards = '\n'.join(ps['card_html'] for ps in summaries)

    return f"""{_STYLE_BLOCK}

<div class="page-header">
  <h1>CFB Fantasy &mdash; Week {week} Live Scores</h1>
  <p>Last updated: {timestamp}</p>
</div>

<h1 class="section">Standings</h1>
<table>
  <thead><tr>
    <th>Rank</th><th>Player</th><th>P4</th><th>G6</th><th>WC</th><th>Week</th><th>Season</th>
  </tr></thead>
  <tbody>
    {''.join(standing_rows)}
  </tbody>
</table>

<h2 class="section">Game Status</h2>
<table>
  <thead><tr>
    <th>Player</th><th>Final&nbsp;/&nbsp;Pass</th><th>In Progress</th><th>Pending</th>
  </tr></thead>
  <tbody>
    {''.join(status_rows)}
  </tbody>
</table>

{cards}"""


# ------------------------------------------------------------------
# Post-week recap page -- JSON payload + shared JS renderer, same
# architecture as the live status page above (Zach, 2026-07-06: keep
# both pages consistent, and keep the payload sectioned so a new
# section can be bolted on later without touching the existing ones).
# ------------------------------------------------------------------

SUMMARY_MARKER_START = '<!-- SUMMARY:START -->'
SUMMARY_MARKER_END = '<!-- SUMMARY:END -->'

_DEFAULT_SUMMARY_BLOCK = f"""{SUMMARY_MARKER_START}
<!-- wp:heading {{"level":2}} -->
<h2>Commissioner's Notes</h2>
<!-- /wp:heading -->
<!-- wp:paragraph -->
<p></p>
<!-- /wp:paragraph -->
{SUMMARY_MARKER_END}"""


def merge_recap_summary(existing_content: Optional[str], new_content: str) -> str:
    """
    Pure string surgery. new_content already has _DEFAULT_SUMMARY_BLOCK
    (an empty "Commissioner's Notes" heading + paragraph) baked in at
    its summary spot. If existing_content (the previously-published
    version of this exact week's recap post, if this is a re-push/
    correction) already has a SUMMARY block, whatever's between its
    markers is extracted and substituted in place of the fresh empty
    one -- so a paragraph Zach wrote directly in WordPress survives a
    --finalize-only re-push. existing_content=None (first-ever push for
    this week) or a missing marker both fall through to the fresh
    (empty) block untouched.
    """
    if not existing_content:
        return new_content
    if SUMMARY_MARKER_START not in existing_content or SUMMARY_MARKER_END not in existing_content:
        return new_content

    preserved = existing_content.split(SUMMARY_MARKER_START, 1)[1].split(SUMMARY_MARKER_END, 1)[0]
    preserved_block = f"{SUMMARY_MARKER_START}{preserved}{SUMMARY_MARKER_END}"

    before, rest = new_content.split(SUMMARY_MARKER_START, 1)
    _, after = rest.split(SUMMARY_MARKER_END, 1)
    return f"{before}{preserved_block}{after}"


def _pick_group(category: str, conference_slot_tiers: dict[str, str]) -> str:
    """
    'p4'/'g6'/'wildcard' for an OptimalPick's category -- used to group
    the optimal-lineup display into the same P4/G6/Wildcard sections the
    scorecard uses (matching _build_slot()'s 'group' field/values
    exactly). Derived from the real conference_slot_tiers mapping, not a
    hardcoded conference list, so it can't silently go stale if
    conferences are ever added/renamed/realigned.
    """
    if category == P4_FLEX_CATEGORY:
        return 'p4'
    if category == G6_FLEX_CATEGORY:
        return 'g6'
    if category == WILDCARD_CATEGORY:
        return 'wildcard'
    return conference_slot_tiers.get(category, 'wildcard').lower()


def _optimal_picks_payload(picks, conference_slot_tiers: dict[str, str], played_ids: Optional[set] = None) -> list[dict]:
    """OptimalPick objects -> JSON-ready dicts, optionally flagging which were actually played."""
    return [{
        'category': p.category,
        'grp': _pick_group(p.category, conference_slot_tiers),
        'teamId': p.team_id,
        'teamName': p.team_name,
        'margin': p.margin,
        'played': (p.team_id in played_ids) if played_ids is not None else None,
    } for p in picks]


def _week_result_payload(rows: list[dict]) -> list[dict]:
    """get_team_week_results()-shaped rows -> JSON-ready dicts (camelCase for the JS side)."""
    return [{
        'teamId': r['team_id'],
        'teamName': r['name'],
        'conference': r['conference_abbreviation'],
        'margin': r['margin'],
    } for r in rows]


def _leaderboard_payload(leaderboard, pvp_records: dict[int, dict]) -> list[dict]:
    """Season leaderboard DataFrame -> JSON-ready rows; [] if empty (e.g. week 1).
    pvp_records is _pvp_records_through_week()'s output, keyed by player_id --
    same source T20's Season Standings table already uses, so both pages'
    PVP Record columns can never disagree."""
    if leaderboard is None or leaderboard.empty:
        return []
    rows = []
    for _, r in leaderboard.iterrows():
        pid = int(r['PlayerId'])
        rec = pvp_records.get(pid, {'w': 0, 'l': 0, 't': 0})
        rows.append({
            'rank': int(r['Rank']),
            'playerId': pid,
            'name': r['Player'],
            'matchPts': int(r['MatchPlayPts']),
            'totalPts': int(r['TotalPts']),
            'pvp': {'w': rec['w'], 'l': rec['l'], 't': rec['t']},
        })
    return rows


def _build_recap_payload(
    season_id: int,
    week: int,
    standings: list[dict],
    leaderboard,
    live_scores: dict,
) -> dict:
    """Pure-ish assembly (does its own DB reads via lib.recap/lib.performance, but returns one plain JSON-ready dict) -- see render_recap_fragment() for what each piece means."""
    summaries = _build_summaries(season_id, week, live_scores)
    pod_of_player = get_pod_membership_map(season_id)
    pod_names_map = _pod_names(season_id)
    pods = get_pods(season_id)
    conference_slot_tiers = get_conference_slot_tiers(season_id)

    try:
        scoring_context_id = get_scoring_context(season_id)['id']
    except ValueError:
        scoring_context_id = None
    matchups = get_matchups_for_week(season_id, scoring_context_id, week) if scoring_context_id else []

    scrappy_by_pod = build_scrappy_by_pod(season_id, week, pods)
    overall_results = [{
        'kind': r['kind'],
        'playerId': r['player_id'],
        'name': r['name'],
        'podName': r['pod_name'],
        'totalPoints': r['total_points'],
        'podRank': r['pod_rank'],
        'overallRank': r['overall_rank'],
        'matchPts': r['match_pts'],
        'basePts': r['base_pts'],
        'podBonus': r['pod_bonus'],
        'vsPodBonus': r['vs_pod_bonus'],
        'overallBonus': r['overall_bonus'],
        'pvpResult': r['pvp_result'],
        'pvpOpponent': r['pvp_opponent'],
    } for r in build_overall_results(standings, matchups, scrappy_by_pod, pod_of_player, pod_names_map)]

    season_pod_results = get_pod_vs_pod_bonus_by_week(season_id, week)
    pod_vs_pod = build_pod_vs_pod_summary(standings, pod_of_player, pod_names_map, season_pod_results)

    pvp_matchups = [{
        'matchupType': r['matchupType'],
        'aPlayerId': r['aPlayerId'], 'aName': r['aName'], 'aPodName': r['aPodName'],
        'aPoints': r['aPoints'], 'aResult': r['aResult'],
        'bPlayerId': r['bPlayerId'], 'bName': r['bName'], 'bPodName': r['bPodName'],
        'bPoints': r['bPoints'], 'bResult': r['bResult'],
    } for r in build_pvp_matchups_table(matchups, standings, pod_of_player, pod_names_map)]

    pvp_records = _pvp_records_through_week(season_id, week, scoring_context_id) if scoring_context_id else {}

    lineup_team_ids_by_player = get_lineup_team_ids(season_id, week)
    season_pick_rows_by_player = get_pick_results_by_player(season_id, week)

    players_payload = []
    for s in summaries:
        pid = s['player_id']
        actual_ids = lineup_team_ids_by_player.get(pid, set())
        player_recap = build_player_recap(season_id, week, pid, actual_ids)
        week_slots = [{'is_pass': sl['is_pass'], 'margin': sl['margin']} for sl in s['slots']]
        players_payload.append({
            'playerId': pid,
            'name': s['name'],
            'podName': s['pod_name'],
            'weekTotal': s['week'],
            'seasonTotal': s['season'],
            'pickRecordWeek': tally_pick_record(week_slots),
            'pickRecordSeason': tally_pick_record(season_pick_rows_by_player.get(pid, [])),
            'slots': [_slot_payload(sl) for sl in s['slots']],
            'unplayedGames': _week_result_payload(player_recap['unplayed_games']),
            'optimalTotal': player_recap['optimal_total'],
            'optimalPicks': [{
                'category': p['category'],
                'grp': _pick_group(p['category'], conference_slot_tiers),
                'teamId': p['team_id'],
                'teamName': p['team_name'],
                'margin': p['margin'],
                'played': p['played'],
            } for p in player_recap['optimal_picks']],
        })

    max_result = compute_max_optimal(season_id, week)

    player_ids = [s['player_id'] for s in summaries]
    player_names = {s['player_id']: s['name'] for s in summaries}
    top_unplayed_winners = build_top_unplayed_winners(
        season_id, week, player_ids, player_names, pod_of_player, pod_names_map, lineup_team_ids_by_player,
    )
    free_agents_by_pod = {
        pod['name']: _week_result_payload(build_top_free_agents(season_id, week, pod['id']))
        for pod in pods
    }

    return {
        'week': week,
        'updatedAt': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'overallResults': overall_results,
        'podVsPod': pod_vs_pod,
        'pvpMatchups': pvp_matchups,
        'seasonLeaderboard': _leaderboard_payload(leaderboard, pvp_records),
        'players': players_payload,
        'maxLineup': {
            'total': max_result.total,
            'picks': _optimal_picks_payload(max_result.picks, conference_slot_tiers),
        },
        'topUnplayedWinners': [{
            'teamId': r['team_id'], 'teamName': r['name'], 'conference': r['conference_abbreviation'],
            'margin': r['margin'], 'playerName': r['player_name'], 'podName': r['pod_name'],
        } for r in top_unplayed_winners],
        'freeAgentsByPod': free_agents_by_pod,
    }


# ------------------------------------------------------------------
# Live status page (T20) -- JSON payload + shared JS renderer, rather
# than Python-built HTML strings like the functions above. Zach's call
# (2026-07-06): less duplicative than hand-building markup per row in
# Python, at the cost of being a real departure from this file's
# existing convention -- kept deliberately separate from
# render_recap_fragment()'s still-Python-string approach above rather
# than converting both at once.
# ------------------------------------------------------------------

def _slot_payload(s: dict) -> dict:
    """One lineup slot, JSON-ready (drops the non-serializable game_time
    timedelta and the fields the new UI doesn't use)."""
    return {
        'lbl': s['label'],
        'grp': s['group'],
        'team': s['team'],
        'opp': s['opponent'],
        'isHome': s['is_home'],
        'isPass': s['is_pass'],
        'status': s['game_status'],
        'picked': s['picked_score'],
        'oppScore': s['opp_score'],
        'margin': s['margin'],
        'cfbdGameId': s['cfbd_game_id'],
    }


def _build_status_payload(
    week: int,
    timestamp: str,
    summaries: list[dict],
    matchups: list[dict],
    pvp_records: dict[int, dict],
    leaderboard,
) -> dict:
    """
    Pure transform -- no DB access here, everything's already been
    fetched by the caller. Turns _build_summaries()/
    _matchups_with_live_scores()/_pvp_records_through_week()/
    build_season_leaderboard()'s output into the one JSON blob the
    live status page's JS renderer consumes.
    """
    players = [{
        'playerId': s['player_id'],
        'name': s['name'],
        'podId': s['pod_id'],
        'podName': s['pod_name'],
        'week': s['week'],
        'season': s['season'],
        'podRank': s['pod_rank'],
        'overallRank': s['overall_rank'],
        'matchPts': s['match_pts'],
        'basePts': s['base_pts'],
        'podBonus': s['pod_bonus'],
        'vsPodBonus': s['vs_pod_bonus'],
        'overallBonus': s['overall_bonus'],
        'counts': s['counts'],
        'slots': [_slot_payload(sl) for sl in s['slots']],
    } for s in summaries]

    matchup_payload = [{
        'id': m['id'],
        'type': m['matchup_type'],
        'aId': m['player_a_id'], 'aName': m['player_a_name'], 'aTotal': m['player_a_total'],
        'bId': m['player_b_id'], 'bName': m['player_b_name'], 'bTotal': m['player_b_total'],
        'leaderId': m['leader_id'],
    } for m in matchups]

    lb_by_id = {}
    if leaderboard is not None and not leaderboard.empty:
        lb_by_id = {int(row['PlayerId']): row for _, row in leaderboard.iterrows()}

    season_rows = []
    for s in summaries:
        pid = s['player_id']
        lb = lb_by_id.get(pid)
        rec = pvp_records.get(pid, {'w': 0, 'l': 0, 't': 0})
        season_rows.append({
            'playerId': pid,
            'name': s['name'],
            'podName': s['pod_name'],
            'matchPts': int(lb['MatchPlayPts']) if lb is not None else None,
            'totalPts': int(lb['TotalPts']) if lb is not None else None,
            'pvp': {'w': rec['w'], 'l': rec['l'], 't': rec['t']},
        })

    return {
        'week': week,
        'updatedAt': timestamp,
        'players': players,
        'matchups': matchup_payload,
        'seasonRows': season_rows,
    }


_STATUS_PAGE_TEMPLATE = """<!-- wp:html -->
<style>
  #cfb-status { --paper: #f2efe4; --paper-raised: #fdfcf8; --ink: #1c2119; --ink-dim: #62695f;
    --line: #dbd5c3; --line-strong: #c4bda6; --accent: #b8501f; --accent-ink: #fff8f0;
    --accent-tint: #f4e0cf; --pod-orange: #b8501f; --pod-white: #48607a; --positive: #2c7a4b;
    --negative: #ae3a2c; --live: #b57d13; --live-tint: #f3e3bf; --focus: #2f6690;
    --shadow: 0 1px 2px rgba(30,24,10,.06), 0 6px 20px rgba(30,24,10,.07);
    --masthead-bg: #171310; --masthead-ink: #f2efe4;
    background: var(--paper); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
    line-height: 1.45; }
  /* Deliberately no @media (prefers-color-scheme: dark) override --
     Zach's call (2026-07-06): the site around this fragment is always
     light, so a visitor in dark mode seeing this section flip to a dark
     palette read as a jarring, out-of-place contrast rather than a nice
     dark-mode courtesy. Fixed light palette regardless of the visitor's
     OS/browser setting. */
  #cfb-status * { box-sizing: border-box; }
  #cfb-status a { color: var(--focus); }
  #cfb-status :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 3px; }
  #cfb-status .cfb-wrap { max-width: 980px; margin: 0 auto; }
  #cfb-status .cfb-masthead { background: var(--masthead-bg); color: var(--masthead-ink);
    padding: 18px 20px; border-radius: 8px; margin-bottom: 20px;
    display: flex; align-items: baseline; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
  #cfb-status .cfb-masthead h1 { margin: 0; font-size: 1.4rem; font-weight: 800; letter-spacing: -0.01em;
    color: var(--masthead-ink); text-wrap: balance; }
  #cfb-status .cfb-masthead .cfb-meta { font-size: 0.8rem; color: var(--masthead-ink); opacity: 0.65; white-space: nowrap; }
  #cfb-status .cfb-live-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: var(--live); margin-right: 6px; position: relative; top: -1px;
    animation: cfb-pulse 1.8s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) { #cfb-status .cfb-live-dot { animation: none; } }
  @keyframes cfb-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  #cfb-status .cfb-section-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.13em;
    font-weight: 700; color: var(--ink-dim); margin: 0 0 10px; }
  #cfb-status .cfb-pod-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 8px; }
  @media (max-width: 640px) { #cfb-status .cfb-pod-row { grid-template-columns: 1fr; } }
  #cfb-status .cfb-pod-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 10px;
    padding: 16px 18px; box-shadow: var(--shadow); position: relative; overflow: hidden; }
  #cfb-status .cfb-pod-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
  #cfb-status .cfb-pod-card.is-orange::before { background: var(--pod-orange); }
  #cfb-status .cfb-pod-card.is-white::before { background: var(--pod-white); }
  #cfb-status .cfb-pod-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  #cfb-status .cfb-pod-name { font-weight: 800; font-size: 1.02rem; letter-spacing: -0.01em; }
  #cfb-status .cfb-pod-card.is-orange .cfb-pod-name { color: var(--pod-orange); }
  #cfb-status .cfb-pod-card.is-white .cfb-pod-name { color: var(--pod-white); }
  #cfb-status .cfb-pod-total { font-variant-numeric: tabular-nums; font-weight: 800; font-size: 1.15rem; }
  #cfb-status .cfb-pod-winner-tag { font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.08em;
    font-weight: 700; background: var(--positive); color: #fff; padding: 2px 7px; border-radius: 20px; margin-left: 8px; }
  #cfb-status .cfb-pod-player-row { display: flex; align-items: center; gap: 8px; font-size: 0.86rem; padding: 4px 0;
    border-top: 1px solid var(--line); }
  #cfb-status .cfb-pod-player-row:first-child { border-top: none; }
  #cfb-status .cfb-pod-player-row .cfb-rank { color: var(--ink-dim); font-variant-numeric: tabular-nums; width: 1.2em; }
  #cfb-status .cfb-pod-player-row .cfb-name { flex: 1; font-weight: 600; }
  #cfb-status .cfb-pod-player-row .cfb-pts { font-variant-numeric: tabular-nums; font-weight: 700; }
  #cfb-status .cfb-legend { font-size: 0.78rem; color: var(--ink-dim); background: var(--paper-raised);
    border: 1px dashed var(--line-strong); border-radius: 8px; padding: 10px 14px; margin: 14px 0 26px; }
  #cfb-status .cfb-legend b { color: var(--ink); }
  #cfb-status .cfb-matchup-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 28px; }
  @media (max-width: 640px) { #cfb-status .cfb-matchup-grid { grid-template-columns: 1fr; } }
  #cfb-status .cfb-matchup-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 14px; box-shadow: var(--shadow); }
  #cfb-status .cfb-matchup-type { font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
    color: var(--ink-dim); margin-bottom: 8px; display: block; }
  #cfb-status .cfb-matchup-sides { display: flex; align-items: center; gap: 10px; }
  #cfb-status .cfb-matchup-side { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
  #cfb-status .cfb-matchup-side.leading .cfb-side-name { color: var(--ink); }
  #cfb-status .cfb-matchup-side.trailing .cfb-side-name { color: var(--ink-dim); }
  #cfb-status .cfb-pod-chip { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  #cfb-status .cfb-pod-chip.orange { background: var(--pod-orange); }
  #cfb-status .cfb-pod-chip.white { background: var(--pod-white); }
  #cfb-status .cfb-side-name { font-weight: 700; font-size: 0.88rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #cfb-status .cfb-side-score { font-variant-numeric: tabular-nums; font-weight: 800; font-size: 1.05rem; flex-shrink: 0; }
  #cfb-status .cfb-matchup-vs { color: var(--ink-dim); font-size: 0.72rem; font-weight: 700; padding: 0 2px; }
  #cfb-status .cfb-table-scroll { overflow-x: auto; margin-bottom: 30px; }
  #cfb-status table.cfb-standings { width: 100%; border-collapse: collapse; font-size: 0.86rem; min-width: 520px; }
  #cfb-status table.cfb-standings th { text-align: left; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--ink-dim); font-weight: 700; padding: 6px 10px; border-bottom: 2px solid var(--line-strong); white-space: nowrap; }
  #cfb-status table.cfb-standings td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  #cfb-status table.cfb-standings tbody tr:hover { background: var(--paper); }
  #cfb-status .cfb-rank-badge { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px;
    border-radius: 50%; font-weight: 800; font-size: 0.78rem; background: var(--paper); border: 1px solid var(--line-strong);
    font-variant-numeric: tabular-nums; }
  #cfb-status .cfb-rank-badge.top { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  #cfb-status td.cfb-num { font-variant-numeric: tabular-nums; text-align: right; }
  #cfb-status .cfb-pts-total { font-weight: 800; font-size: 0.98rem; }
  #cfb-status .cfb-pts-break { display: block; font-size: 0.68rem; color: var(--ink-dim); font-weight: 500; white-space: nowrap; }
  #cfb-status .cfb-name-cell { display: inline-flex; align-items: center; gap: 7px; font-weight: 700; }
  #cfb-status .cfb-pvp-record { font-variant-numeric: tabular-nums; }
  #cfb-status .cfb-updated-line { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  #cfb-status .cfb-updated-txt { font-size: 0.78rem; color: var(--ink-dim); white-space: nowrap; }
  #cfb-status .cfb-toolbar { position: sticky; top: 0; z-index: 5; background: var(--paper); display: flex; flex-wrap: wrap;
    gap: 10px; align-items: center; padding: 10px 0; margin-bottom: 14px; border-bottom: 1px solid var(--line); }
  #cfb-status select, #cfb-status input[type="search"] { font: inherit; font-size: 0.84rem; color: var(--ink);
    background: var(--paper-raised); border: 1px solid var(--line-strong); border-radius: 7px; padding: 7px 10px; }
  #cfb-status input[type="search"] { min-width: 170px; }
  #cfb-status .cfb-toolbar-spacer { flex: 1; }
  #cfb-status button.cfb-tbtn { font: inherit; font-size: 0.8rem; font-weight: 700; color: var(--ink);
    background: var(--paper-raised); border: 1px solid var(--line-strong); border-radius: 7px; padding: 7px 12px; cursor: pointer; }
  #cfb-status button.cfb-tbtn:hover { border-color: var(--accent); color: var(--accent); }
  #cfb-status .cfb-roster { display: flex; flex-direction: column; gap: 10px; }
  #cfb-status details.cfb-player-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: var(--shadow); overflow: hidden; }
  #cfb-status details.cfb-player-card[open] summary { border-bottom: 1px solid var(--line); }
  #cfb-status summary.cfb-card-summary { list-style: none; cursor: pointer; padding: 12px 16px; display: flex;
    align-items: center; gap: 12px; flex-wrap: wrap; }
  #cfb-status summary.cfb-card-summary::-webkit-details-marker { display: none; }
  #cfb-status summary.cfb-card-summary::before { content: "\\25b8"; color: var(--ink-dim); font-size: 0.75rem; width: 0.9em;
    flex-shrink: 0; transition: transform .15s ease; }
  #cfb-status details[open] summary.cfb-card-summary::before { transform: rotate(90deg); }
  #cfb-status .cfb-name-block { display: flex; align-items: center; gap: 8px; min-width: 150px; }
  #cfb-status .cfb-card-summary .cfb-name { font-weight: 800; font-size: 0.98rem; }
  #cfb-status .cfb-spacer { flex: 1; }
  #cfb-status .cfb-stat-chip { font-size: 0.76rem; color: var(--ink-dim); display: flex; flex-direction: column;
    align-items: flex-end; min-width: 62px; }
  #cfb-status .cfb-stat-chip b { font-variant-numeric: tabular-nums; font-size: 0.94rem; color: var(--ink); }
  #cfb-status .cfb-status-pill { font-size: 0.68rem; font-weight: 700; padding: 3px 9px; border-radius: 20px; white-space: nowrap; }
  #cfb-status .cfb-status-pill.done { background: var(--paper); color: var(--ink-dim); border: 1px solid var(--line-strong); }
  #cfb-status .cfb-status-pill.live { background: var(--live-tint); color: var(--live); }
  #cfb-status .cfb-card-body { padding: 4px 16px 16px; }
  #cfb-status .cfb-slot-group-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
    color: var(--ink-dim); margin: 14px 0 4px; }
  #cfb-status table.cfb-slots { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  #cfb-status table.cfb-slots td, #cfb-status table.cfb-slots th { padding: 5px 8px; }
  #cfb-status table.cfb-slots th { text-align: left; font-size: 0.65rem; color: var(--ink-dim); text-transform: uppercase;
    letter-spacing: 0.06em; border-bottom: 1px solid var(--line); }
  #cfb-status table.cfb-slots td { border-bottom: 1px solid var(--line); }
  #cfb-status table.cfb-slots tbody tr:last-child td { border-bottom: none; }
  #cfb-status .cfb-slot-label { color: var(--ink-dim); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; width: 58px; }
  #cfb-status td.cfb-margin { font-weight: 800; font-variant-numeric: tabular-nums; text-align: right; width: 48px; }
  #cfb-status td.cfb-margin.pos { color: var(--positive); }
  #cfb-status td.cfb-margin.neg { color: var(--negative); }
  #cfb-status .cfb-score-txt { font-variant-numeric: tabular-nums; }
  #cfb-status .cfb-pass-txt { color: var(--ink-dim); font-style: italic; }
  #cfb-status .cfb-game-status-txt { font-size: 0.72rem; color: var(--ink-dim); }
  #cfb-status .cfb-game-status-txt.live { color: var(--live); font-weight: 700; }
  #cfb-status .cfb-subtotal-row td { font-weight: 700; border-top: 1px solid var(--line-strong); border-bottom: none !important; }
  #cfb-status .cfb-empty-state { text-align: center; padding: 30px 0; color: var(--ink-dim); font-size: 0.9rem; }
</style>

<div id="cfb-status">
  <div class="cfb-wrap">
    <div class="cfb-masthead">
      <h1 id="cfb-title">CFB Fantasy &mdash; Week Status</h1>
      <div class="cfb-meta"><span class="cfb-live-dot"></span>Live &middot; updated <span id="cfb-masthead-updated"></span></div>
    </div>

    <div id="cfb-pod-section" hidden>
      <p class="cfb-section-label">Pod Standing</p>
      <div class="cfb-pod-row" id="cfb-pod-row"></div>
      <div class="cfb-legend"><b>Match points</b> = base (rank 1st&ndash;Nth: high&rarr;0) + pod winner (+1) + pod-vs-pod winner (+1) + overall winner (+1)</div>
    </div>

    <div id="cfb-matchup-section" hidden>
      <p class="cfb-section-label">This Week's Matchups</p>
      <div class="cfb-matchup-grid" id="cfb-matchup-grid"></div>
    </div>

    <div id="cfb-standings-section" hidden>
      <div class="cfb-updated-line">
        <p class="cfb-section-label" style="margin-bottom:0">This Week's Standings</p>
        <span class="cfb-updated-txt" id="cfb-updated-txt"></span>
      </div>
      <div class="cfb-table-scroll">
        <table class="cfb-standings" id="cfb-standings-table"><thead></thead><tbody id="cfb-standings-body"></tbody></table>
      </div>

      <p class="cfb-section-label">Season Standings</p>
      <div class="cfb-table-scroll">
        <table class="cfb-standings">
          <thead><tr><th>Player</th><th class="cfb-num">Match&nbsp;Pts</th><th class="cfb-num">Total&nbsp;Pts</th><th>PVP&nbsp;Record</th></tr></thead>
          <tbody id="cfb-season-body"></tbody>
        </table>
      </div>
    </div>

    <div id="cfb-roster-section" hidden>
      <p class="cfb-section-label">Player Detail</p>
      <div class="cfb-toolbar">
        <select id="cfb-pod-filter" aria-label="Filter by pod"><option value="all">All pods</option></select>
        <select id="cfb-sort-by" aria-label="Sort players">
          <option value="week">Sort: Week net (high&rarr;low)</option>
          <option value="rank">Sort: Overall rank</option>
          <option value="match">Sort: Match points</option>
          <option value="name">Sort: Name (A&rarr;Z)</option>
        </select>
        <input type="search" id="cfb-search" placeholder="Find a player&hellip;" aria-label="Search players">
        <div class="cfb-toolbar-spacer"></div>
        <button class="cfb-tbtn" id="cfb-expand-all">Expand all</button>
        <button class="cfb-tbtn" id="cfb-collapse-all">Collapse all</button>
      </div>
      <div class="cfb-roster" id="cfb-roster"></div>
    </div>

    <p class="cfb-empty-state" id="cfb-empty" hidden>No lineups submitted yet this week.</p>
  </div>
</div>

<script>
(function () {
  var DATA = __CFB_STATUS_DATA__;
  var ESPN_URL = 'https://www.espn.com/college-football/game/_/gameId/';
  var root = document.getElementById('cfb-status');

  function fmtSigned(n) { return (n > 0 ? '+' : '') + n; }
  function podChip(podName) {
    if (!podName) return '';
    var cls = /orange/i.test(podName) ? 'orange' : 'white';
    return '<span class="cfb-pod-chip ' + cls + '" title="' + podName + '"></span>';
  }

  document.getElementById('cfb-title').textContent = 'CFB Fantasy \\u2014 Week ' + DATA.week + ' Status';
  document.getElementById('cfb-masthead-updated').textContent = DATA.updatedAt;
  var updatedTxt = document.getElementById('cfb-updated-txt');
  if (updatedTxt) updatedTxt.textContent = 'Last updated ' + DATA.updatedAt;

  if (!DATA.players.length) {
    document.getElementById('cfb-empty').hidden = false;
    return;
  }

  var players = DATA.players.slice();
  var byId = {};
  players.forEach(function (p) { byId[p.playerId] = p; });

  // Pods present, derived from the players themselves (no separate pod list needed).
  var pods = {};
  players.forEach(function (p) {
    if (!p.podName) return;
    (pods[p.podName] = pods[p.podName] || []).push(p);
  });
  var podNames = Object.keys(pods);
  var multiPod = podNames.length > 1;

  // ---- Pod comparison (only when there's more than one pod this season) ----
  if (multiPod) {
    document.getElementById('cfb-pod-section').hidden = false;
    var totals = {};
    podNames.forEach(function (name) {
      pods[name].sort(function (a, b) { return b.week - a.week; });
      totals[name] = pods[name].reduce(function (s, p) { return s + p.week; }, 0);
    });
    podNames.sort(function (a, b) { return totals[b] - totals[a]; });
    var winningPod = totals[podNames[0]] === totals[podNames[1]] ? null : podNames[0];

    document.getElementById('cfb-pod-row').innerHTML = podNames.map(function (name) {
      var cls = /orange/i.test(name) ? 'is-orange' : 'is-white';
      var isWinner = winningPod === name;
      var rows = pods[name].map(function (p, i) {
        return '<div class="cfb-pod-player-row"><span class="cfb-rank">' + (i + 1) + '</span>' +
          '<span class="cfb-name">' + p.name + '</span>' +
          '<span class="cfb-pts">' + fmtSigned(p.week) + '</span></div>';
      }).join('');
      return '<div class="cfb-pod-card ' + cls + '"><div class="cfb-pod-head">' +
        '<span class="cfb-pod-name">' + name + (isWinner ? ' <span class="cfb-pod-winner-tag">Leading</span>' : '') + '</span>' +
        '<span class="cfb-pod-total">' + fmtSigned(totals[name]) + '</span></div>' +
        '<div class="cfb-pod-players">' + rows + '</div></div>';
    }).join('');
  }

  // ---- Matchups ----
  if (DATA.matchups.length) {
    document.getElementById('cfb-matchup-section').hidden = false;
    document.getElementById('cfb-matchup-grid').innerHTML = DATA.matchups.map(function (m) {
      var aLead = m.leaderId === m.aId;
      var bLead = m.leaderId === m.bId;
      var aPod = byId[m.aId] ? byId[m.aId].podName : null;
      var bPod = byId[m.bId] ? byId[m.bId].podName : null;
      var aTotalTxt = m.aTotal == null ? '&mdash;' : fmtSigned(m.aTotal);
      var bTotalTxt = m.bTotal == null ? '&mdash;' : fmtSigned(m.bTotal);
      var typeLabel = m.type === 'cross_pod' ? 'Cross-Pod' : 'In-Pod';
      return '<div class="cfb-matchup-card"><span class="cfb-matchup-type">' + typeLabel + '</span>' +
        '<div class="cfb-matchup-sides">' +
        '<div class="cfb-matchup-side ' + (aLead ? 'leading' : 'trailing') + '">' + podChip(aPod) +
        '<span class="cfb-side-name">' + m.aName + '</span></div>' +
        '<span class="cfb-side-score">' + aTotalTxt + '</span>' +
        '<span class="cfb-matchup-vs">vs</span>' +
        '<span class="cfb-side-score">' + bTotalTxt + '</span>' +
        '<div class="cfb-matchup-side ' + (bLead ? 'leading' : 'trailing') + '" style="flex-direction:row-reverse;text-align:right;">' +
        podChip(bPod) + '<span class="cfb-side-name">' + m.bName + '</span></div>' +
        '</div></div>';
    }).join('');
  }

  // ---- This week's standings + season standings ----
  document.getElementById('cfb-standings-section').hidden = false;
  var haveMatchPts = players.some(function (p) { return p.matchPts != null; });
  var theadCells = multiPod
    ? '<th>Player</th><th>Rk</th><th>Pod</th><th class="cfb-num">Week&nbsp;Net</th><th class="cfb-num">Match&nbsp;Pts</th>'
    : '<th>Player</th><th>Rk</th><th class="cfb-num">Week&nbsp;Net</th><th class="cfb-num">Match&nbsp;Pts</th>';
  document.querySelector('#cfb-standings-table thead').innerHTML = '<tr>' + theadCells + '</tr>';

  var byWeek = players.slice().sort(function (a, b) { return b.week - a.week; });
  document.getElementById('cfb-standings-body').innerHTML = byWeek.map(function (p) {
    var parts = [];
    if (p.basePts != null) parts.push(p.basePts + ' base');
    if (p.podBonus) parts.push('+1 pod');
    if (p.vsPodBonus) parts.push('+1 v-pod');
    if (p.overallBonus) parts.push('+1 top');
    var matchCell = p.matchPts == null
      ? '<span class="cfb-pts-break">not available yet</span>'
      : '<span class="cfb-pts-total">' + p.matchPts + '</span><span class="cfb-pts-break">' + parts.join(' &middot; ') + '</span>';
    var rankCell = '<span class="cfb-rank-badge ' + (p.overallRank === 1 ? 'top' : '') + '">' + (p.overallRank || '&ndash;') + '</span>';
    var podCell = multiPod ? '<td>' + (p.podName || '') + '</td>' : '';
    return '<tr><td><span class="cfb-name-cell">' + podChip(p.podName) + p.name + '</span></td>' +
      '<td>' + rankCell + '</td>' + podCell +
      '<td class="cfb-num">' + fmtSigned(p.week) + '</td>' +
      '<td class="cfb-num">' + matchCell + '</td></tr>';
  }).join('');

  var bySeasonMatch = DATA.seasonRows.slice().sort(function (a, b) {
    var am = a.matchPts == null ? -Infinity : a.matchPts;
    var bm = b.matchPts == null ? -Infinity : b.matchPts;
    return bm - am;
  });
  document.getElementById('cfb-season-body').innerHTML = bySeasonMatch.map(function (r) {
    var recordTxt = r.pvp.t ? (r.pvp.w + '-' + r.pvp.l + '-' + r.pvp.t) : (r.pvp.w + '-' + r.pvp.l);
    return '<tr><td><span class="cfb-name-cell">' + podChip(r.podName) + r.name + '</span></td>' +
      '<td class="cfb-num">' + (r.matchPts == null ? '&ndash;' : r.matchPts) + '</td>' +
      '<td class="cfb-num">' + (r.totalPts == null ? '&ndash;' : r.totalPts) + '</td>' +
      '<td class="cfb-pvp-record">' + recordTxt + '</td></tr>';
  }).join('');

  // ---- Player detail cards ----
  document.getElementById('cfb-roster-section').hidden = false;
  var podFilterEl = document.getElementById('cfb-pod-filter');
  if (multiPod) {
    podNames.slice().sort().forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name; opt.textContent = name + ' only';
      podFilterEl.appendChild(opt);
    });
  } else {
    podFilterEl.hidden = true;
  }

  function slotRow(s) {
    if (s.isPass) {
      return '<tr><td class="cfb-slot-label">' + s.lbl + '</td><td colspan="3" class="cfb-pass-txt">Passed</td><td class="cfb-margin">+0</td></tr>';
    }
    var scoreTxt;
    if (s.status === 'final' || s.status === 'in_progress') {
      var scoreStr = s.picked + '&ndash;' + s.oppScore;
      if (s.cfbdGameId) scoreStr = '<a href="' + ESPN_URL + s.cfbdGameId + '" target="_blank" rel="noopener">' + scoreStr + '</a>';
      var statusTag = s.status === 'in_progress' ? ' <span class="cfb-game-status-txt live">&middot; Live</span>' : ' <span class="cfb-game-status-txt">&middot; Final</span>';
      scoreTxt = '<span class="cfb-score-txt">' + scoreStr + '</span>' + statusTag;
    } else {
      scoreTxt = '<span class="cfb-game-status-txt">Not started</span>';
    }
    var marginTxt = s.margin == null ? '&mdash;' : fmtSigned(s.margin);
    var marginCls = s.margin == null ? '' : (s.margin > 0 ? 'pos' : (s.margin < 0 ? 'neg' : ''));
    var oppTxt = (s.isHome ? 'vs ' : '@ ') + (s.opp || '');
    return '<tr><td class="cfb-slot-label">' + s.lbl + '</td><td><b>' + s.team + '</b></td><td>' + oppTxt + '</td>' +
      '<td>' + scoreTxt + '</td><td class="cfb-margin ' + marginCls + '">' + marginTxt + '</td></tr>';
  }

  function groupTable(label, slots) {
    if (!slots.length) return '';
    var sum = slots.reduce(function (s, x) { return s + (x.margin || 0); }, 0);
    var sumCls = sum > 0 ? 'pos' : (sum < 0 ? 'neg' : '');
    return '<p class="cfb-slot-group-label">' + label + '</p>' +
      '<table class="cfb-slots"><thead><tr><th>Slot</th><th>Team</th><th>Opponent</th><th>Score</th><th>Net</th></tr></thead>' +
      '<tbody>' + slots.map(slotRow).join('') +
      '<tr class="cfb-subtotal-row"><td colspan="4">Subtotal</td><td class="cfb-margin ' + sumCls + '">' + fmtSigned(sum) + '</td></tr>' +
      '</tbody></table>';
  }

  var cardsById = {};
  players.forEach(function (p) {
    var p4 = p.slots.filter(function (s) { return s.grp === 'p4'; });
    var g6 = p.slots.filter(function (s) { return s.grp === 'g6'; });
    var wc = p.slots.filter(function (s) { return s.grp === 'wildcard'; });
    var c = p.counts;
    var statusTxt = c.in_progress > 0 ? (c.in_progress + ' live') : (c.pending > 0 ? (c.pending + ' pending') : 'All final');
    var statusCls = c.in_progress > 0 ? 'live' : 'done';

    var el = document.createElement('details');
    el.className = 'cfb-player-card';
    el.dataset.name = p.name;
    el.dataset.pod = p.podName || '';
    el.innerHTML =
      '<summary class="cfb-card-summary">' +
      '<span class="cfb-rank-badge ' + (p.overallRank === 1 ? 'top' : '') + '">' + (p.overallRank || '&ndash;') + '</span>' +
      '<div class="cfb-name-block">' + podChip(p.podName) + '<span class="cfb-name">' + p.name + '</span></div>' +
      '<span class="cfb-spacer"></span>' +
      '<span class="cfb-stat-chip">Week<b>' + fmtSigned(p.week) + '</b></span>' +
      (p.matchPts != null ? '<span class="cfb-stat-chip">Match Pts<b>' + p.matchPts + '</b></span>' : '') +
      '<span class="cfb-stat-chip">Season<b>' + fmtSigned(p.season) + '</b></span>' +
      '<span class="cfb-status-pill ' + statusCls + '">' + statusTxt + '</span>' +
      '</summary>' +
      '<div class="cfb-card-body">' + groupTable('P4', p4) + groupTable('G6', g6) + groupTable('Wildcard', wc) + '</div>';
    cardsById[p.playerId] = el;
  });

  var rosterEl = document.getElementById('cfb-roster');
  var sortByEl = document.getElementById('cfb-sort-by');
  var searchEl = document.getElementById('cfb-search');

  function applyToolbar() {
    var podVal = podFilterEl.value;
    var sortVal = sortByEl.value;
    var q = searchEl.value.trim().toLowerCase();

    var list = players.filter(function (p) {
      return (podVal === 'all' || p.podName === podVal) && (q === '' || p.name.toLowerCase().indexOf(q) !== -1);
    });

    var sorters = {
      rank: function (a, b) { return (a.overallRank || 999) - (b.overallRank || 999); },
      week: function (a, b) { return b.week - a.week; },
      match: function (a, b) {
        // Explicit null checks, not `||` -- a real 0 match-point score
        // (a last-place finish) is a legitimate value, not "missing".
        var am = a.matchPts == null ? -Infinity : a.matchPts;
        var bm = b.matchPts == null ? -Infinity : b.matchPts;
        return bm - am;
      },
      name: function (a, b) { return a.name.localeCompare(b.name); },
    };
    list.sort(sorters[sortVal]);

    rosterEl.innerHTML = '';
    list.forEach(function (p) { rosterEl.appendChild(cardsById[p.playerId]); });
  }

  podFilterEl.addEventListener('change', applyToolbar);
  sortByEl.addEventListener('change', applyToolbar);
  searchEl.addEventListener('input', applyToolbar);
  document.getElementById('cfb-expand-all').addEventListener('click', function () {
    rosterEl.querySelectorAll('details').forEach(function (d) { d.open = true; });
  });
  document.getElementById('cfb-collapse-all').addEventListener('click', function () {
    rosterEl.querySelectorAll('details').forEach(function (d) { d.open = false; });
  });

  applyToolbar();
})();
</script>
<!-- /wp:html -->"""


def _render_status_page(payload: dict) -> str:
    """Inject the JSON payload into _STATUS_PAGE_TEMPLATE. `</` is escaped
    so a team/player name can never prematurely close the <script> tag."""
    data_json = json.dumps(payload).replace('</', '<\\/')
    return _STATUS_PAGE_TEMPLATE.replace('__CFB_STATUS_DATA__', data_json)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def render_fragment(season_id: int, week: int, live_scores: dict) -> str:
    """
    Build the WordPress-postable (and locally-embeddable) fragment for a
    week's live scoring content: pod comparison, PVP matchups, this-week
    and season standings, and collapsible per-player cards with a
    filter/sort/search toolbar (T20, redesigned 2026-07-06). A JSON data
    payload + a shared JS renderer (_STATUS_PAGE_TEMPLATE), not
    Python-built HTML strings -- see the module comment above
    _slot_payload() for why. Wrapped in a Gutenberg Custom HTML block
    (<!-- wp:html -->) so WordPress doesn't mangle the raw <script>/
    <style> if the page is ever reopened in the block editor -- verified
    live that this survives intact, script execution included.

    Match points/standings are recomputed live off whatever's in
    live_scores right now, including in-progress games (Zach, 2026-07-05:
    understood as tentative until games finish, same as the raw score
    totals already are -- not withheld until finalize_week()).

    live_scores: same format as fetch_live_scores() — {cfbd_game_id: {...}}
    Pass an empty dict at week start or when no live data is available yet.
    """
    summaries = _build_summaries(season_id, week, live_scores)
    player_totals = {s['player_id']: {'player_name': s['name'], 'total': s['week']} for s in summaries}

    try:
        scoring_context_id = get_scoring_context(season_id)['id']
    except ValueError:
        scoring_context_id = None

    matchups = _matchups_with_live_scores(season_id, week, scoring_context_id, player_totals) if scoring_context_id else []
    pvp_records = _pvp_records_through_week(season_id, week, scoring_context_id) if scoring_context_id else {}
    leaderboard = build_season_leaderboard(season_id)

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    payload = _build_status_payload(week, timestamp, summaries, matchups, pvp_records, leaderboard)
    return _render_status_page(payload)


_RECAP_PAGE_TEMPLATE = """<!-- wp:html -->
<style>
  #cfb-recap { --paper: #f2efe4; --paper-raised: #fdfcf8; --ink: #1c2119; --ink-dim: #62695f;
    --line: #dbd5c3; --line-strong: #c4bda6; --accent: #b8501f; --accent-ink: #fff8f0;
    --accent-tint: #f4e0cf; --pod-orange: #b8501f; --pod-white: #48607a; --positive: #2c7a4b;
    --negative: #ae3a2c; --live: #b57d13; --live-tint: #f3e3bf; --focus: #2f6690;
    --shadow: 0 1px 2px rgba(30,24,10,.06), 0 6px 20px rgba(30,24,10,.07);
    --masthead-bg: #171310; --masthead-ink: #f2efe4;
    background: var(--paper); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
    line-height: 1.45; }
  /* Fixed light palette on purpose -- matches the live status page's
     2026-07-06 decision: the site around this fragment is always
     light, so following the visitor's dark-mode setting here would
     read as a jarring, out-of-place contrast rather than a courtesy. */
  #cfb-recap * { box-sizing: border-box; }
  #cfb-recap a { color: var(--focus); }
  #cfb-recap :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 3px; }
  #cfb-recap .cfb-wrap { max-width: 980px; margin: 0 auto; }
  #cfb-recap .cfb-masthead { background: var(--masthead-bg); color: var(--masthead-ink);
    padding: 18px 20px; border-radius: 8px; margin-bottom: 20px;
    display: flex; align-items: baseline; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
  #cfb-recap .cfb-masthead h1 { margin: 0; font-size: 1.4rem; font-weight: 800; letter-spacing: -0.01em;
    color: var(--masthead-ink); text-wrap: balance; }
  #cfb-recap .cfb-masthead .cfb-meta { font-size: 0.8rem; color: var(--masthead-ink); opacity: 0.65; white-space: nowrap; }
  #cfb-recap .cfb-section-label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.13em;
    font-weight: 700; color: var(--ink-dim); margin: 28px 0 10px; }
  #cfb-recap .cfb-section-label:first-of-type { margin-top: 0; }
  #cfb-recap .cfb-summary-block { background: var(--paper-raised); border: 1px solid var(--line);
    border-radius: 10px; padding: 4px 20px 16px; margin-bottom: 8px; box-shadow: var(--shadow); }
  #cfb-recap .cfb-summary-block h2 { font-size: 1.05rem; margin: 16px 0 6px; }
  #cfb-recap .cfb-summary-block p:empty::before { content: "No notes written for this week yet."; color: var(--ink-dim); font-style: italic; }

  #cfb-recap .cfb-pod-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  @media (max-width: 640px) { #cfb-recap .cfb-pod-row { grid-template-columns: 1fr; } }
  #cfb-recap .cfb-pod-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 10px;
    padding: 14px 16px; box-shadow: var(--shadow); position: relative; overflow: hidden; }
  #cfb-recap .cfb-pod-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
  #cfb-recap .cfb-pod-card.is-orange::before { background: var(--pod-orange); }
  #cfb-recap .cfb-pod-card.is-white::before { background: var(--pod-white); }
  #cfb-recap .cfb-pod-card h3 { margin: 0 0 8px; font-size: 0.92rem; }
  #cfb-recap .cfb-pod-card.is-orange h3 { color: var(--pod-orange); }
  #cfb-recap .cfb-pod-card.is-white h3 { color: var(--pod-white); }
  #cfb-recap .cfb-pod-winner-tag { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.06em;
    font-weight: 700; background: var(--positive); color: #fff; padding: 2px 7px; border-radius: 20px; margin-left: 6px; }
  #cfb-recap .cfb-pod-points { font-weight: 800; font-size: 1.35rem; font-variant-numeric: tabular-nums; margin: 2px 0 4px; }
  #cfb-recap .cfb-podvspod-title { margin: 28px 0 10px; font-size: 0.92rem; }
  #cfb-recap .cfb-eyebrow { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.13em; font-weight: 700; color: var(--ink-dim); }
  #cfb-recap .cfb-podvspod-title b { color: var(--ink); }

  #cfb-recap .cfb-pod-chip { width: 9px; height: 9px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  #cfb-recap .cfb-pod-chip.orange { background: var(--pod-orange); }
  #cfb-recap .cfb-pod-chip.white { background: var(--pod-white); }
  #cfb-recap .cfb-name-cell { display: inline-flex; align-items: center; gap: 7px; font-weight: 700; }
  #cfb-recap .cfb-name-cell.is-scrappy { font-style: italic; color: var(--ink-dim); font-weight: 600; }
  #cfb-recap .cfb-synthetic-tag { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--ink-dim); border: 1px solid var(--line-strong); border-radius: 20px; padding: 1px 6px; margin-left: 6px; }

  #cfb-recap .cfb-table-scroll { overflow-x: auto; margin-bottom: 8px; }
  #cfb-recap table.cfb-standings { width: 100%; border-collapse: collapse; font-size: 0.86rem; min-width: 560px; }
  #cfb-recap table.cfb-standings th { text-align: left; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--ink-dim); font-weight: 700; padding: 6px 10px; border-bottom: 2px solid var(--line-strong); white-space: nowrap; }
  #cfb-recap table.cfb-standings td { padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  #cfb-recap table.cfb-standings tbody tr:hover { background: var(--paper); }
  #cfb-recap table.cfb-standings tbody tr.is-scrappy { background: var(--paper); }
  #cfb-recap td.cfb-num { font-variant-numeric: tabular-nums; text-align: right; }
  #cfb-recap .cfb-pts-total { font-weight: 800; font-size: 0.98rem; }
  #cfb-recap .cfb-pts-break { display: block; font-size: 0.68rem; color: var(--ink-dim); font-weight: 500; white-space: nowrap; }
  #cfb-recap .cfb-rank-badge { display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px;
    border-radius: 50%; font-weight: 800; font-size: 0.78rem; background: var(--paper); border: 1px solid var(--line-strong);
    font-variant-numeric: tabular-nums; }
  #cfb-recap .cfb-rank-badge.top { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
  #cfb-recap .cfb-pvp-pill { font-size: 0.68rem; font-weight: 700; padding: 2px 8px; border-radius: 20px; }
  #cfb-recap .cfb-pvp-pill.w { background: var(--positive); color: #fff; }
  #cfb-recap .cfb-pvp-pill.l { background: var(--negative); color: #fff; }
  #cfb-recap .cfb-pvp-pill.t { background: var(--line-strong); color: var(--ink); }
  #cfb-recap .cfb-dash { color: var(--ink-dim); }
  #cfb-recap td.cfb-matchup-name.won { font-weight: 800; }
  #cfb-recap td.cfb-matchup-name.lost { color: var(--ink-dim); }
  #cfb-recap td.cfb-matchup-pts.won { font-weight: 800; color: var(--positive); }
  #cfb-recap td.cfb-matchup-pts.lost { color: var(--ink-dim); }
  #cfb-recap .cfb-pvp-record { font-variant-numeric: tabular-nums; }
  #cfb-recap .cfb-pick-record { font-size: 0.85rem; color: var(--ink-dim); margin: 0 0 10px; font-variant-numeric: tabular-nums; }
  #cfb-recap .cfb-pick-record b { color: var(--ink); font-weight: 700; }

  #cfb-recap .cfb-toolbar { position: sticky; top: 0; z-index: 5; background: var(--paper); display: flex; flex-wrap: wrap;
    gap: 10px; align-items: center; padding: 10px 0; margin-bottom: 14px; border-bottom: 1px solid var(--line); }
  #cfb-recap select, #cfb-recap input[type="search"] { font: inherit; font-size: 0.84rem; color: var(--ink);
    background: var(--paper-raised); border: 1px solid var(--line-strong); border-radius: 7px; padding: 7px 10px; }
  #cfb-recap input[type="search"] { min-width: 170px; }
  #cfb-recap .cfb-toolbar-spacer { flex: 1; }
  #cfb-recap button.cfb-tbtn { font: inherit; font-size: 0.8rem; font-weight: 700; color: var(--ink);
    background: var(--paper-raised); border: 1px solid var(--line-strong); border-radius: 7px; padding: 7px 12px; cursor: pointer; }
  #cfb-recap button.cfb-tbtn:hover { border-color: var(--accent); color: var(--accent); }

  #cfb-recap .cfb-roster { display: flex; flex-direction: column; gap: 10px; }
  #cfb-recap details.cfb-player-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: var(--shadow); overflow: hidden; }
  #cfb-recap details.cfb-player-card[open] summary { border-bottom: 1px solid var(--line); }
  #cfb-recap summary.cfb-card-summary { list-style: none; cursor: pointer; padding: 12px 16px; display: flex;
    align-items: center; gap: 12px; flex-wrap: wrap; }
  #cfb-recap summary.cfb-card-summary::-webkit-details-marker { display: none; }
  #cfb-recap summary.cfb-card-summary::before { content: "\\25b8"; color: var(--ink-dim); font-size: 0.75rem; width: 0.9em;
    flex-shrink: 0; transition: transform .15s ease; }
  #cfb-recap details[open] summary.cfb-card-summary::before { transform: rotate(90deg); }
  #cfb-recap .cfb-name-block { display: flex; align-items: center; gap: 8px; min-width: 150px; }
  #cfb-recap .cfb-card-summary .cfb-name { font-weight: 800; font-size: 0.98rem; }
  #cfb-recap .cfb-spacer { flex: 1; }
  #cfb-recap .cfb-stat-chip { font-size: 0.76rem; color: var(--ink-dim); display: flex; flex-direction: column;
    align-items: flex-end; min-width: 62px; }
  #cfb-recap .cfb-stat-chip b { font-variant-numeric: tabular-nums; font-size: 0.94rem; color: var(--ink); }
  #cfb-recap .cfb-card-body { padding: 4px 16px 16px; }
  #cfb-recap .cfb-subsection-label { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;
    color: var(--ink-dim); margin: 16px 0 4px; }
  #cfb-recap table.cfb-slots { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  #cfb-recap table.cfb-slots td, #cfb-recap table.cfb-slots th { padding: 5px 8px; }
  #cfb-recap table.cfb-slots th { text-align: left; font-size: 0.65rem; color: var(--ink-dim); text-transform: uppercase;
    letter-spacing: 0.06em; border-bottom: 1px solid var(--line); }
  #cfb-recap table.cfb-slots td { border-bottom: 1px solid var(--line); }
  #cfb-recap table.cfb-slots tbody tr:last-child td { border-bottom: none; }
  #cfb-recap .cfb-slot-label { color: var(--ink-dim); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; width: 58px; }
  #cfb-recap td.cfb-margin { font-weight: 800; font-variant-numeric: tabular-nums; text-align: right; width: 48px; }
  #cfb-recap td.cfb-margin.pos { color: var(--positive); }
  #cfb-recap td.cfb-margin.neg { color: var(--negative); }
  #cfb-recap .cfb-pass-txt { color: var(--ink-dim); font-style: italic; }
  #cfb-recap .cfb-subtotal-row td { font-weight: 700; border-top: 1px solid var(--line-strong); border-bottom: none !important; }
  #cfb-recap .cfb-empty-note { color: var(--ink-dim); font-size: 0.82rem; font-style: italic; padding: 4px 0; }

  #cfb-recap tr.cfb-optimal-row.played td.cfb-optimal-flag { color: var(--positive); }
  #cfb-recap tr.cfb-optimal-row.missed td.cfb-optimal-flag { color: var(--negative); }
  #cfb-recap td.cfb-optimal-flag { font-weight: 800; width: 22px; text-align: center; }
  #cfb-recap tr.cfb-optimal-row.missed { background: color-mix(in srgb, var(--negative) 6%, transparent); }
  #cfb-recap tr.cfb-optimal-row.played { background: color-mix(in srgb, var(--positive) 6%, transparent); }
</style>

<div id="cfb-recap">
  <div class="cfb-wrap">
    <div class="cfb-masthead">
      <h1 id="cfb-recap-title">CFB Fantasy &mdash; Week Recap</h1>
      <div class="cfb-meta">Finalized <span id="cfb-recap-updated"></span></div>
    </div>

    <div class="cfb-summary-block">
      __CFB_SUMMARY_BLOCK__
    </div>

    <div id="cfb-podvspod-section" hidden>
      <p class="cfb-podvspod-title"><span class="cfb-eyebrow">Pod vs. Pod</span> &mdash; <span id="cfb-podvspod-record"></span></p>
      <div class="cfb-pod-row" id="cfb-podvspod-row"></div>
    </div>

    <p class="cfb-section-label">Overall Results</p>
    <div class="cfb-table-scroll">
      <table class="cfb-standings" id="cfb-overall-table"><thead></thead><tbody id="cfb-overall-body"></tbody></table>
    </div>

    <div id="cfb-pvpmatchups-section" hidden>
      <p class="cfb-section-label">This Week's Matchups</p>
      <div class="cfb-table-scroll">
        <table class="cfb-standings">
          <thead><tr><th>Player</th><th class="cfb-num">Pts</th><th></th><th class="cfb-num">Pts</th><th>Player</th></tr></thead>
          <tbody id="cfb-pvpmatchups-body"></tbody>
        </table>
      </div>
    </div>

    <div id="cfb-leaderboard-section" hidden>
      <p class="cfb-section-label">Season Leaderboard</p>
      <div class="cfb-table-scroll">
        <table class="cfb-standings">
          <thead><tr><th>Rk</th><th>Player</th><th class="cfb-num">Match&nbsp;Pts</th><th class="cfb-num">Total&nbsp;Pts</th><th>PVP&nbsp;Record</th></tr></thead>
          <tbody id="cfb-leaderboard-body"></tbody>
        </table>
      </div>
    </div>

    <p class="cfb-section-label">Max &mdash; Best Possible Lineup, Any FBS Team</p>
    <div id="cfb-max-lineup"></div>

    <p class="cfb-section-label">Top 10 Owned, Unplayed Winners (League-Wide)</p>
    <div class="cfb-table-scroll">
      <table class="cfb-standings">
        <thead><tr><th>Team</th><th>Conf</th><th class="cfb-num">Margin</th><th>Owner</th><th>Pod</th></tr></thead>
        <tbody id="cfb-unplayed-winners-body"></tbody>
      </table>
    </div>

    <p class="cfb-section-label">Top 10 Free Agents by Pod</p>
    <div class="cfb-pod-row" id="cfb-free-agents-row"></div>

    <p class="cfb-section-label">Player Detail</p>
    <div class="cfb-toolbar">
      <select id="cfb-pod-filter" aria-label="Filter by pod"><option value="all">All pods</option></select>
      <select id="cfb-sort-by" aria-label="Sort players">
        <option value="week">Sort: Week net (high&rarr;low)</option>
        <option value="name">Sort: Name (A&rarr;Z)</option>
      </select>
      <input type="search" id="cfb-search" placeholder="Find a player&hellip;" aria-label="Search players">
      <div class="cfb-toolbar-spacer"></div>
      <button class="cfb-tbtn" id="cfb-expand-all">Expand all</button>
      <button class="cfb-tbtn" id="cfb-collapse-all">Collapse all</button>
    </div>
    <div class="cfb-roster" id="cfb-roster"></div>
  </div>
</div>

<script>
(function () {
  var DATA = __CFB_RECAP_DATA__;

  function fmtSigned(n) { return (n > 0 ? '+' : '') + n; }
  function isOrange(podName) { return /orange/i.test(podName || ''); }
  function podChip(podName) {
    if (!podName) return '';
    var cls = isOrange(podName) ? 'orange' : 'white';
    return '<span class="cfb-pod-chip ' + cls + '" title="' + podName + '"></span>';
  }
  function pvpPill(result, opponent) {
    // Just the pill here -- the opponent's name and score are shown in
    // their own dedicated PVP Matchups table instead (Zach, 2026-07-06),
    // not crammed inline into this column too.
    if (!result) return '<span class="cfb-dash">&mdash;</span>';
    var label = result === 'W' ? 'W' : (result === 'L' ? 'L' : 'T');
    var title = opponent ? (label + ' vs ' + opponent) : label;
    return '<span class="cfb-pvp-pill ' + label.toLowerCase() + '" title="' + title + '">' + label + '</span>';
  }

  document.getElementById('cfb-recap-title').textContent = 'CFB Fantasy \\u2014 Week ' + DATA.week + ' Recap';
  document.getElementById('cfb-recap-updated').textContent = DATA.updatedAt;

  // ---- Pod vs. Pod summary (absent entirely for a single-pod season) ----
  if (DATA.podVsPod) {
    document.getElementById('cfb-podvspod-section').hidden = false;
    var pvp = DATA.podVsPod;
    var maxPoints = Math.max.apply(null, pvp.thisWeek.map(function (t) { return t.points; }));
    var winnersThisWeek = pvp.thisWeek.filter(function (t) { return t.points === maxPoints; });

    // One-line series record, shown once in the section header rather
    // than repeated under each pod card.
    var bySeriesWins = pvp.series.slice().sort(function (a, b) { return b.wins - a.wins; });
    var leader = bySeriesWins[0], other = bySeriesWins[1];
    var recordTxt = leader.wins + '-' + (other ? other.wins : 0) + '-' + pvp.ties;
    var recordHtml = (other && leader.wins === other.wins)
      ? 'Series tied ' + recordTxt
      : '<b>' + leader.podName + '</b> leads ' + recordTxt;
    document.getElementById('cfb-podvspod-record').innerHTML = recordHtml;

    // Orange consistently on the right (Zach, 2026-07-06) -- sorting by
    // isOrange (false < true) puts the non-orange pod first/left, orange
    // last/right, deterministically every week rather than however
    // build_pod_vs_pod_summary() happened to order its dict.
    var orderedPods = pvp.thisWeek.slice().sort(function (a, b) { return isOrange(a.podName) - isOrange(b.podName); });
    document.getElementById('cfb-podvspod-row').innerHTML = orderedPods.map(function (tw) {
      var cls = isOrange(tw.podName) ? 'is-orange' : 'is-white';
      var isWinner = winnersThisWeek.length === 1 && winnersThisWeek[0].podName === tw.podName;
      return '<div class="cfb-pod-card ' + cls + '">' +
        '<h3>' + tw.podName + (isWinner ? ' <span class="cfb-pod-winner-tag">Won the week</span>' : '') + '</h3>' +
        '<div class="cfb-pod-points">' + fmtSigned(tw.points) + '</div>' +
        '</div>';
    }).join('');
  }

  // ---- This Week's Matchups (small 4-row pairing table; Zach, 2026-07-06) ----
  if (DATA.pvpMatchups && DATA.pvpMatchups.length) {
    document.getElementById('cfb-pvpmatchups-section').hidden = false;
    document.getElementById('cfb-pvpmatchups-body').innerHTML = DATA.pvpMatchups.map(function (m) {
      // Orange consistently on the right, same convention as the pod
      // totals above -- whichever side (a/b) is orange goes right,
      // regardless of which side the DB happened to store as "a".
      var aSide = { name: m.aName, pod: m.aPodName, points: m.aPoints, result: m.aResult };
      var bSide = { name: m.bName, pod: m.bPodName, points: m.bPoints, result: m.bResult };
      var left = isOrange(aSide.pod) ? bSide : aSide;
      var right = isOrange(aSide.pod) ? aSide : bSide;
      var leftCls = left.result === 'W' ? 'won' : (left.result === 'L' ? 'lost' : '');
      var rightCls = right.result === 'W' ? 'won' : (right.result === 'L' ? 'lost' : '');
      return '<tr>' +
        '<td class="cfb-matchup-name ' + leftCls + '">' + podChip(left.pod) + left.name + '</td>' +
        '<td class="cfb-num cfb-matchup-pts ' + leftCls + '">' + fmtSigned(left.points) + '</td>' +
        '<td class="cfb-dash">vs</td>' +
        '<td class="cfb-num cfb-matchup-pts ' + rightCls + '">' + fmtSigned(right.points) + '</td>' +
        '<td class="cfb-matchup-name ' + rightCls + '">' + podChip(right.pod) + right.name + '</td>' +
        '</tr>';
    }).join('');
  }

  // ---- Overall Results (players + Scrappy, interleaved by total points) ----
  var multiPod = new Set(DATA.overallResults.map(function (r) { return r.podName; }).filter(Boolean)).size > 1;
  var theadCells = multiPod
    ? '<th>Player</th><th>Pod</th><th>PVP</th><th class="cfb-num">Match&nbsp;Pts</th><th class="cfb-num">Total&nbsp;Pts</th>'
    : '<th>Player</th><th>PVP</th><th class="cfb-num">Match&nbsp;Pts</th><th class="cfb-num">Total&nbsp;Pts</th>';
  document.querySelector('#cfb-overall-table thead').innerHTML = '<tr>' + theadCells + '</tr>';

  document.getElementById('cfb-overall-body').innerHTML = DATA.overallResults.map(function (r, i) {
    var isScrappy = r.kind === 'scrappy';
    var nameCell = '<span class="cfb-name-cell' + (isScrappy ? ' is-scrappy' : '') + '">' +
      podChip(r.podName) + r.name + (isScrappy ? '<span class="cfb-synthetic-tag">synthetic</span>' : '') + '</span>';
    var podCell = multiPod ? '<td>' + (r.podName || '') + '</td>' : '';
    var parts = [];
    if (r.basePts != null) parts.push(r.basePts + ' base');
    if (r.podBonus) parts.push('+1 pod');
    if (r.vsPodBonus) parts.push('+1 v-pod');
    if (r.overallBonus) parts.push('+1 top');
    var matchCell = r.matchPts == null
      ? '<span class="cfb-dash">&mdash;</span>'
      : '<span class="cfb-pts-total">' + r.matchPts + '</span><span class="cfb-pts-break">' + parts.join(' &middot; ') + '</span>';
    return '<tr class="' + (isScrappy ? 'is-scrappy' : '') + '">' +
      '<td>' + nameCell + '</td>' + podCell +
      '<td>' + pvpPill(r.pvpResult, r.pvpOpponent) + '</td>' +
      '<td class="cfb-num">' + matchCell + '</td>' +
      '<td class="cfb-num">' + fmtSigned(r.totalPoints) + '</td></tr>';
  }).join('');

  // ---- Season Leaderboard ----
  if (DATA.seasonLeaderboard.length) {
    document.getElementById('cfb-leaderboard-section').hidden = false;
    document.getElementById('cfb-leaderboard-body').innerHTML = DATA.seasonLeaderboard.map(function (r) {
      var recordTxt = r.pvp.t ? (r.pvp.w + '-' + r.pvp.l + '-' + r.pvp.t) : (r.pvp.w + '-' + r.pvp.l);
      return '<tr><td><span class="cfb-rank-badge ' + (r.rank === 1 ? 'top' : '') + '">' + r.rank + '</span></td>' +
        '<td>' + r.name + '</td>' +
        '<td class="cfb-num">' + r.matchPts + '</td><td class="cfb-num">' + r.totalPts + '</td>' +
        '<td class="cfb-pvp-record">' + recordTxt + '</td></tr>';
    }).join('');
  }

  // ---- Shared: render a grouped P4/G6/Wildcard picks table (Max lineup + optimal comparisons) ----
  function groupPicksTable(label, picks, showPlayedFlag) {
    if (!picks.length) return '';
    var sum = picks.reduce(function (s, p) { return s + (p.margin || 0); }, 0);
    var sumCls = sum > 0 ? 'pos' : (sum < 0 ? 'neg' : '');
    var flagHeader = showPlayedFlag ? '<th></th>' : '';
    var rows = picks.map(function (p) {
      var marginCls = p.margin > 0 ? 'pos' : (p.margin < 0 ? 'neg' : '');
      var flagCell = '';
      var rowCls = '';
      if (showPlayedFlag) {
        flagCell = '<td class="cfb-optimal-flag">' + (p.played ? '\\u2713' : '\\u2717') + '</td>';
        rowCls = ' cfb-optimal-row ' + (p.played ? 'played' : 'missed');
      }
      return '<tr class="' + rowCls + '">' + flagCell +
        '<td class="cfb-slot-label">' + p.category + '</td><td><b>' + p.teamName + '</b></td>' +
        '<td class="cfb-margin ' + marginCls + '">' + fmtSigned(p.margin) + '</td></tr>';
    }).join('');
    var colspan = showPlayedFlag ? 3 : 2;
    return '<p class="cfb-subsection-label">' + label + '</p>' +
      '<table class="cfb-slots"><thead><tr>' + flagHeader + '<th>Slot</th><th>Team</th><th>Net</th></tr></thead>' +
      '<tbody>' + rows +
      '<tr class="cfb-subtotal-row"><td colspan="' + colspan + '">Subtotal</td><td class="cfb-margin ' + sumCls + '">' + fmtSigned(sum) + '</td></tr>' +
      '</tbody></table>';
  }

  function groupedByTier(picks) {
    // Grouped by the 'grp' field the Python side already resolved from
    // the real conference_slot_tiers mapping (mirrors the scorecard
    // slots' own 'grp' field) -- never guessed from conference-name
    // patterns here, so it can't go stale if conferences change.
    return {
      p4: picks.filter(function (p) { return p.grp === 'p4'; }),
      g6: picks.filter(function (p) { return p.grp === 'g6'; }),
      wc: picks.filter(function (p) { return p.grp === 'wildcard'; }),
    };
  }

  // ---- Max lineup ----
  var maxGroups = groupedByTier(DATA.maxLineup.picks);
  document.getElementById('cfb-max-lineup').innerHTML =
    '<p class="cfb-empty-note" style="margin-bottom:10px;">Total: <b>' + fmtSigned(DATA.maxLineup.total) + '</b> &middot; no roster constraint, every FBS team eligible.</p>' +
    groupPicksTable('P4', maxGroups.p4, false) + groupPicksTable('G6', maxGroups.g6, false) + groupPicksTable('Wildcard', maxGroups.wc, false);

  // ---- Top 10 unplayed winners (league-wide) ----
  document.getElementById('cfb-unplayed-winners-body').innerHTML = DATA.topUnplayedWinners.length
    ? DATA.topUnplayedWinners.map(function (t) {
        return '<tr><td><b>' + t.teamName + '</b></td><td>' + t.conference + '</td>' +
          '<td class="cfb-num">' + fmtSigned(t.margin) + '</td><td>' + t.playerName + '</td>' +
          '<td>' + podChip(t.podName) + (t.podName || '') + '</td></tr>';
      }).join('')
    : '<tr><td colspan="5" class="cfb-empty-note">Nothing left on the bench this week.</td></tr>';

  // ---- Top 10 free agents by pod ----
  var podNamesFA = Object.keys(DATA.freeAgentsByPod);
  document.getElementById('cfb-free-agents-row').innerHTML = podNamesFA.map(function (podName) {
    var cls = /orange/i.test(podName) ? 'is-orange' : 'is-white';
    var rows = DATA.freeAgentsByPod[podName];
    var body = rows.length
      ? rows.map(function (t) {
          return '<tr><td>' + t.teamName + ' <span class="cfb-dash">(' + t.conference + ')</span></td>' +
            '<td class="cfb-num">' + fmtSigned(t.margin) + '</td></tr>';
        }).join('')
      : '<tr><td colspan="2" class="cfb-empty-note">No positive-margin free agents this week.</td></tr>';
    return '<div class="cfb-pod-card ' + cls + '"><h3>' + podName + '</h3>' +
      '<table class="cfb-slots"><tbody>' + body + '</tbody></table></div>';
  }).join('');

  // ---- Player detail cards ----
  var podFilterEl = document.getElementById('cfb-pod-filter');
  var podNamesPlayers = Array.from(new Set(DATA.players.map(function (p) { return p.podName; }).filter(Boolean))).sort();
  if (podNamesPlayers.length > 1) {
    podNamesPlayers.forEach(function (name) {
      var opt = document.createElement('option');
      opt.value = name; opt.textContent = name + ' only';
      podFilterEl.appendChild(opt);
    });
  } else {
    podFilterEl.hidden = true;
  }

  function slotRow(s) {
    if (s.isPass) {
      return '<tr><td class="cfb-slot-label">' + s.lbl + '</td><td colspan="3" class="cfb-pass-txt">Passed</td><td class="cfb-margin">+0</td></tr>';
    }
    var scoreStr = (s.picked != null && s.oppScore != null) ? (s.picked + '&ndash;' + s.oppScore) : '&mdash;';
    if (s.cfbdGameId) scoreStr = '<a href="https://www.espn.com/college-football/game/_/gameId/' + s.cfbdGameId + '" target="_blank" rel="noopener">' + scoreStr + '</a>';
    var marginTxt = s.margin == null ? '&mdash;' : fmtSigned(s.margin);
    var marginCls = s.margin == null ? '' : (s.margin > 0 ? 'pos' : (s.margin < 0 ? 'neg' : ''));
    var oppTxt = (s.isHome ? 'vs ' : '@ ') + (s.opp || '');
    return '<tr><td class="cfb-slot-label">' + s.lbl + '</td><td><b>' + s.team + '</b></td><td>' + oppTxt + '</td>' +
      '<td>' + scoreStr + '</td><td class="cfb-margin ' + marginCls + '">' + marginTxt + '</td></tr>';
  }

  function scorecardTable(label, slots) {
    if (!slots.length) return '';
    var sum = slots.reduce(function (s, x) { return s + (x.margin || 0); }, 0);
    var sumCls = sum > 0 ? 'pos' : (sum < 0 ? 'neg' : '');
    return '<p class="cfb-subsection-label">' + label + '</p>' +
      '<table class="cfb-slots"><thead><tr><th>Slot</th><th>Team</th><th>Opponent</th><th>Score</th><th>Net</th></tr></thead>' +
      '<tbody>' + slots.map(slotRow).join('') +
      '<tr class="cfb-subtotal-row"><td colspan="4">Subtotal</td><td class="cfb-margin ' + sumCls + '">' + fmtSigned(sum) + '</td></tr>' +
      '</tbody></table>';
  }

  function pickRecordStr(rec) {
    var wl = rec.w + '-' + rec.l + (rec.t ? '-' + rec.t : '');
    var passTxt = rec.pass ? (' (' + rec.pass + ' pass' + (rec.pass > 1 ? 'es' : '') + ')') : '';
    return wl + passTxt;
  }

  function unplayedGamesList(games) {
    if (!games.length) return '<p class="cfb-empty-note">Every roster team with a game this week was in the lineup.</p>';
    return '<table class="cfb-slots"><tbody>' + games.map(function (g) {
      var marginCls = g.margin > 0 ? 'pos' : (g.margin < 0 ? 'neg' : '');
      return '<tr><td><b>' + g.teamName + '</b> <span class="cfb-dash">(' + g.conference + ')</span></td>' +
        '<td class="cfb-margin ' + marginCls + '">' + fmtSigned(g.margin) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }

  var cardsById = {};
  DATA.players.forEach(function (p) {
    var slotsByGroup = {
      p4: p.slots.filter(function (s) { return s.grp === 'p4'; }),
      g6: p.slots.filter(function (s) { return s.grp === 'g6'; }),
      wc: p.slots.filter(function (s) { return s.grp === 'wildcard'; }),
    };
    var optimalGroups = groupedByTier(p.optimalPicks);

    var el = document.createElement('details');
    el.className = 'cfb-player-card';
    el.dataset.name = p.name;
    el.dataset.pod = p.podName || '';
    el.innerHTML =
      '<summary class="cfb-card-summary">' +
      '<div class="cfb-name-block">' + podChip(p.podName) + '<span class="cfb-name">' + p.name + '</span></div>' +
      '<span class="cfb-spacer"></span>' +
      '<span class="cfb-stat-chip">Week<b>' + fmtSigned(p.weekTotal) + '</b></span>' +
      '<span class="cfb-stat-chip">Season<b>' + fmtSigned(p.seasonTotal) + '</b></span>' +
      '<span class="cfb-stat-chip">Optimal<b>' + fmtSigned(p.optimalTotal) + '</b></span>' +
      '</summary>' +
      '<div class="cfb-card-body">' +
      '<p class="cfb-pick-record">Pick Record &mdash; This Week: <b>' + pickRecordStr(p.pickRecordWeek) + '</b>' +
      ' &nbsp;&middot;&nbsp; Season: <b>' + pickRecordStr(p.pickRecordSeason) + '</b></p>' +
      '<p class="cfb-section-label" style="margin-top:6px;">Final Scorecard</p>' +
      scorecardTable('P4', slotsByGroup.p4) + scorecardTable('G6', slotsByGroup.g6) + scorecardTable('Wildcard', slotsByGroup.wc) +
      '<p class="cfb-section-label">Unplayed Roster Games</p>' +
      unplayedGamesList(p.unplayedGames) +
      '<p class="cfb-section-label">Optimal Lineup <span class="cfb-dash" style="text-transform:none;letter-spacing:normal;font-weight:500;">&mdash; &#10003; played it, &#10007; missed it</span></p>' +
      groupPicksTable('P4', optimalGroups.p4, true) + groupPicksTable('G6', optimalGroups.g6, true) + groupPicksTable('Wildcard', optimalGroups.wc, true) +
      '</div>';
    cardsById[p.playerId] = el;
  });

  var rosterEl = document.getElementById('cfb-roster');
  var sortByEl = document.getElementById('cfb-sort-by');
  var searchEl = document.getElementById('cfb-search');

  function applyToolbar() {
    var podVal = podFilterEl.value;
    var sortVal = sortByEl.value;
    var q = searchEl.value.trim().toLowerCase();

    var list = DATA.players.filter(function (p) {
      return (podVal === 'all' || p.podName === podVal) && (q === '' || p.name.toLowerCase().indexOf(q) !== -1);
    });

    var sorters = {
      week: function (a, b) { return b.weekTotal - a.weekTotal; },
      name: function (a, b) { return a.name.localeCompare(b.name); },
    };
    list.sort(sorters[sortVal]);

    rosterEl.innerHTML = '';
    list.forEach(function (p) { rosterEl.appendChild(cardsById[p.playerId]); });
  }

  podFilterEl.addEventListener('change', applyToolbar);
  sortByEl.addEventListener('change', applyToolbar);
  searchEl.addEventListener('input', applyToolbar);
  document.getElementById('cfb-expand-all').addEventListener('click', function () {
    rosterEl.querySelectorAll('details').forEach(function (d) { d.open = true; });
  });
  document.getElementById('cfb-collapse-all').addEventListener('click', function () {
    rosterEl.querySelectorAll('details').forEach(function (d) { d.open = false; });
  });

  applyToolbar();
})();
</script>
<!-- /wp:html -->"""


def _render_recap_page(payload: dict) -> str:
    """
    Inject the JSON payload (and the default empty summary block) into
    _RECAP_PAGE_TEMPLATE. Two separate placeholders, replaced in this
    order (data first): __CFB_RECAP_DATA__ is real JSON and could in
    principle contain the literal substring "__CFB_SUMMARY_BLOCK__" if
    a team/player name were ever that perverse, so the summary
    placeholder -- which is not attacker-controlled, always exactly
    _DEFAULT_SUMMARY_BLOCK at this stage -- is substituted second to
    avoid that (admittedly unlikely) collision. `</` is escaped in the
    JSON so a name can never prematurely close the <script> tag.
    """
    data_json = json.dumps(payload).replace('</', '<\\/')
    html = _RECAP_PAGE_TEMPLATE.replace('__CFB_RECAP_DATA__', data_json)
    html = html.replace('__CFB_SUMMARY_BLOCK__', _DEFAULT_SUMMARY_BLOCK)
    return html


def render_recap_fragment(
    season_id: int,
    week: int,
    standings: list[dict],
    leaderboard,
    live_scores: dict,
) -> str:
    """
    Build the WordPress-postable fragment for a week's recap
    (redesigned 2026-07-06 -- Zach's full spec): the overall results
    table (PVP/pod-vs-pod/standings/total points, with Scrappy's two
    pod results interleaved by total points), the season leaderboard,
    every player's final scorecard + their unplayed-roster-games list +
    their optimal-lineup comparison (which picks they actually played),
    the league-wide "Max" lineup, the top 10 owned-but-unplayed winners,
    and the top 10 free agents per pod. Same JSON-payload + shared-JS-
    renderer architecture as render_fragment() (T20), and the same
    Gutenberg Custom HTML wrapping -- see that function's docstring for
    why. Includes an empty, marker-bounded "Commissioner's Notes"
    section near the top for Zach to type a summary directly into via
    the WordPress editor; see merge_recap_summary() for how that
    survives a --finalize-only re-push of the same week without this
    function needing to know anything about WordPress.

    standings: from lib.metagame.compute_weekly_standings() — pass the
        exact list finalize_week() already computed, no need to re-query.
    leaderboard: from lib.metagame.build_season_leaderboard() — a
        DataFrame, possibly empty (no rows) if this is week 1.
    live_scores: authoritative final scores keyed by cfbd_game_id (the
        same dict finalize_week() fetches via _fetch_final_scores).
        Passed through so cards show FINAL status/scores even though
        games.status in the DB itself may still read 'scheduled' — see
        TODO.md's "Game finalization script" gap; this sidesteps it
        rather than depending on it.
    """
    payload = _build_recap_payload(season_id, week, standings, leaderboard, live_scores)
    return _render_recap_page(payload)


def _wrap_doc(title: str, fragment: str, auto_refresh: bool = False) -> str:
    """Wrap a fragment in a minimal local-file HTML document shell."""
    refresh_meta = '\n  <meta http-equiv="refresh" content="300">' if auto_refresh else ''
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">{refresh_meta}
  <title>{title}</title>
</head>
<body>
{fragment}
</body>
</html>"""


def update_week_page(season_id: int, week: int, live_scores: dict) -> Path:
    """
    Build and write the week's HTML score page to output/web/week_N_live.html.

    live_scores: same format as fetch_live_scores() — {cfbd_game_id: {...}}
    Pass an empty dict at week start (generate_week_page) or when no API data
    is available yet.
    """
    fragment = render_fragment(season_id, week, live_scores)
    html = _wrap_doc(f'CFB Fantasy &mdash; Week {week} Live Scores', fragment, auto_refresh=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f'week_{week}_live.html'
    path.write_text(html, encoding='utf-8')
    return path


def generate_week_page(season_id: int, week: int) -> Path:
    """
    Create the initial HTML page at week start with all slots showing pending.
    Equivalent to update_week_page with no live data.
    """
    return update_week_page(season_id, week, live_scores={})


def write_final_page(
    season_id: int,
    week: int,
    standings: list[dict],
    leaderboard,
    live_scores: dict,
) -> Path:
    """Write the week's finalized recap to output/web/week_N_final.html (T13)."""
    fragment = render_recap_fragment(season_id, week, standings, leaderboard, live_scores)
    html = _wrap_doc(f'CFB Fantasy &mdash; Week {week} Recap', fragment)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f'week_{week}_final.html'
    path.write_text(html, encoding='utf-8')
    return path
