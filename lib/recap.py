"""
lib/recap.py
-------------
Data assembly for the post-week recap page (the sibling of T20's live
status page, for the finalized/final view). Mixes DB access with light
computation, matching lib/records.py's and lib/metagame.py's existing
convention -- the actual optimization logic lives in lib.optimal_lineup,
this module is glue.

Everything here assumes finalize_week() has already run for this week:
weekly_results and matchup_pairings.winner_id/is_tie are both resolved,
so nothing here needs T20's "live/in-progress" degradation handling.

Sections (kept in separate functions/payload keys deliberately, so a new
section can be added later without touching the existing ones):
  - build_overall_results()      -- standings + PVP + pod-vs-pod + Scrappy
  - build_pod_vs_pod_summary()   -- this week's pod totals + season series record
  - build_player_recap()         -- one player's scorecard-adjacent data:
                                     unplayed roster games + optimal-lineup
                                     comparison (both, per player)
  - build_top_unplayed_winners() -- league-wide, across all pods
  - build_top_free_agents()      -- per pod
  - lib.performance.compute_max_optimal() -- the Max lineup (reused as-is,
    no wrapper needed here)
"""

from __future__ import annotations

from typing import Optional

from lib.db import (
    get_active_roster_teams,
    get_available_teams,
    get_team_week_results,
)
from lib.optimal_lineup import OptimalLineupResult
from lib.performance import compute_player_optimal, compute_scrappy_optimal


def _resolve_matchup(m: dict) -> tuple:
    """(result_a, result_b), each 'W'/'L'/'T'/None, from one
    get_matchups_for_week() row. None/None if winner_id/is_tie somehow
    isn't resolved yet (shouldn't happen once finalize_week() has run,
    but not assumed blindly). Shared by _matchup_lookup() (per-player
    view, for build_overall_results()) and build_pvp_matchups_table()
    (per-matchup view) so the two can never disagree."""
    if m['is_tie']:
        return 'T', 'T'
    if m['winner_id'] == m['player_a_id']:
        return 'W', 'L'
    if m['winner_id'] == m['player_b_id']:
        return 'L', 'W'
    return None, None


def _matchup_lookup(matchups: list[dict]) -> dict[int, dict]:
    """{player_id: {opponent_id, opponent_name, result, matchup_type}} from
    get_matchups_for_week()'s rows."""
    lookup: dict[int, dict] = {}
    for m in matchups:
        result_a, result_b = _resolve_matchup(m)
        lookup[m['player_a_id']] = {
            'opponent_id': m['player_b_id'], 'opponent_name': m['player_b_name'],
            'result': result_a, 'matchup_type': m['matchup_type'],
        }
        lookup[m['player_b_id']] = {
            'opponent_id': m['player_a_id'], 'opponent_name': m['player_a_name'],
            'result': result_b, 'matchup_type': m['matchup_type'],
        }
    return lookup


def build_pvp_matchups_table(
    matchups: list[dict],
    standings: list[dict],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
) -> list[dict]:
    """
    One row per PVP matchup this week -- both sides' name, pod, this
    week's total points, and result. The dedicated pairing view (Zach,
    2026-07-06): distinct from build_overall_results()'s per-player
    pvp_result/pvp_opponent fields, which stay in the Overall Results
    table as a quick per-player glance; this is the fuller side-by-side
    view shown as its own small table.
    """
    totals_by_player = {s['player_id']: s['total_points'] for s in standings}
    rows = []
    for m in matchups:
        result_a, result_b = _resolve_matchup(m)
        rows.append({
            'matchupType': m['matchup_type'],
            'aPlayerId': m['player_a_id'], 'aName': m['player_a_name'],
            'aPodName': pod_names.get(pod_of_player.get(m['player_a_id'])),
            'aPoints': totals_by_player.get(m['player_a_id']), 'aResult': result_a,
            'bPlayerId': m['player_b_id'], 'bName': m['player_b_name'],
            'bPodName': pod_names.get(pod_of_player.get(m['player_b_id'])),
            'bPoints': totals_by_player.get(m['player_b_id']), 'bResult': result_b,
        })
    return rows


def tally_pick_record(slots: list[dict]) -> dict:
    """
    {'w', 'l', 't', 'pass'} -- how many of these picked teams won, lost,
    tied, or were an outright pass. slots only need 'is_pass' and
    'margin' (positive == win) -- works equally for one week's
    already-built scorecard slots (scripts/html_scoring.py's
    _build_slot() shape) and season-to-date raw pick rows
    (lib.db.get_pick_results_by_player()'s shape), so this-week and
    season-total pick records are both derived from exactly one place.
    A slot with margin=None (game not yet final) isn't counted in any
    bucket -- shouldn't occur for an already-finalized week, but not
    assumed blindly.
    """
    w = l = t = p = 0
    for s in slots:
        if s['is_pass']:
            p += 1
        elif s['margin'] is None:
            continue
        elif s['margin'] > 0:
            w += 1
        elif s['margin'] < 0:
            l += 1
        else:
            t += 1
    return {'w': w, 'l': l, 't': t, 'pass': p}


def build_overall_results(
    standings: list[dict],
    matchups: list[dict],
    scrappy_by_pod: dict[str, OptimalLineupResult],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
) -> list[dict]:
    """
    One row per real player (from compute_weekly_standings()'s output)
    plus one row per pod's Scrappy, all sorted together by total points.
    Scrappy rows carry pod_name/total_points only -- match_pts/pvp/
    pod-vs-pod fields are None, since Scrappy doesn't participate in
    those systems (matches Zach's "leave that stuff off" instruction;
    the None fields are what let the renderer leave those cells blank
    rather than showing a misleading zero).
    """
    matchup_by_player = _matchup_lookup(matchups)

    rows = []
    for s in standings:
        pod_id = pod_of_player.get(s['player_id'])
        pvp = matchup_by_player.get(s['player_id'])
        rows.append({
            'kind': 'player',
            'player_id': s['player_id'],
            'name': s['player_name'],
            'pod_name': pod_names.get(pod_id),
            'total_points': s['total_points'],
            'pod_rank': s.get('pod_rank'),
            'overall_rank': s.get('overall_rank'),
            'match_pts': s.get('match_play_points'),
            'base_pts': s.get('base_points'),
            'pod_bonus': s.get('pod_bonus'),
            'vs_pod_bonus': s.get('pod_vs_pod_bonus'),
            'overall_bonus': s.get('overall_bonus'),
            'pvp_result': pvp['result'] if pvp else None,
            'pvp_opponent': pvp['opponent_name'] if pvp else None,
        })

    for pod_name, result in scrappy_by_pod.items():
        rows.append({
            'kind': 'scrappy',
            'player_id': None,
            'name': f'Scrappy ({pod_name})',
            'pod_name': pod_name,
            'total_points': result.total,
            'pod_rank': None, 'overall_rank': None,
            'match_pts': None, 'base_pts': None, 'pod_bonus': None,
            'vs_pod_bonus': None, 'overall_bonus': None,
            'pvp_result': None, 'pvp_opponent': None,
        })

    rows.sort(key=lambda r: r['total_points'], reverse=True)
    return rows


def build_pod_vs_pod_summary(
    standings: list[dict],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
    season_pod_results: list[dict],
) -> Optional[dict]:
    """
    None for a single-pod season (nothing to compare) -- otherwise:
    {'thisWeek': [{'podName', 'points'}, ...], 'series': [{'podName', 'wins'}, ...], 'ties': n}

    thisWeek's points are this week's combined total_points per pod,
    summed fresh from `standings` (the same data every other section of
    this week's recap already uses). series/ties are the season-to-date
    record, built from season_pod_results
    (lib.db.get_pod_vs_pod_bonus_by_week's rows) -- reads the
    already-stored pod_vs_pod_bonus per week rather than re-deriving a
    winner independently, so this can never disagree with the actual
    bonus points a player received. A week is only counted if both pods
    have a row for it (defensive -- shouldn't happen once a season is
    underway, but a genuinely absent pod for a week shouldn't be
    silently treated as a loss for it).
    """
    pods_present = set(pod_of_player.values())
    if len(pods_present) < 2:
        return None

    this_week_points: dict[int, int] = {}
    for s in standings:
        pod_id = pod_of_player.get(s['player_id'])
        if pod_id is not None:
            this_week_points[pod_id] = this_week_points.get(pod_id, 0) + s['total_points']

    by_week: dict[int, dict[int, int]] = {}
    for row in season_pod_results:
        by_week.setdefault(row['week'], {})[row['pod_id']] = row['won']

    wins: dict[int, int] = {pid: 0 for pid in pods_present}
    ties = 0
    for pod_wins in by_week.values():
        if len(pod_wins) < 2:
            continue
        winners = [pid for pid, won in pod_wins.items() if won]
        if len(winners) == 1:
            wins[winners[0]] = wins.get(winners[0], 0) + 1
        else:
            ties += 1

    return {
        'thisWeek': [{'podName': pod_names.get(pid), 'points': pts} for pid, pts in this_week_points.items()],
        'series': [{'podName': pod_names.get(pid), 'wins': w} for pid, w in wins.items()],
        'ties': ties,
    }


def build_player_recap(season_id: int, week: int, player_id: int, lineup_team_ids: set[int]) -> dict:
    """
    One player's unplayed-roster-games list and optimal-lineup
    comparison. Both need the same two inputs (their roster and their
    actual lineup), fetched once by the caller and passed in.
    """
    roster = get_active_roster_teams(player_id, season_id)
    roster_ids = [t['team_id'] for t in roster]
    unplayed_ids = [tid for tid in roster_ids if tid not in lineup_team_ids]

    unplayed_results = get_team_week_results(season_id, week, unplayed_ids)
    unplayed_results = [r for r in unplayed_results if r['margin'] is not None]
    unplayed_results.sort(key=lambda r: r['margin'], reverse=True)

    optimal = compute_player_optimal(season_id, week, player_id, actual_team_ids=lineup_team_ids)
    optimal_picks = [{
        'category': p.category,
        'team_id': p.team_id,
        'team_name': p.team_name,
        'margin': p.margin,
        'played': p.team_id in lineup_team_ids,
    } for p in optimal.picks]

    return {
        'unplayed_games': unplayed_results,
        'optimal_total': optimal.total,
        'optimal_picks': optimal_picks,
    }


def build_top_unplayed_winners(
    season_id: int,
    week: int,
    player_ids: list[int],
    player_names: dict[int, str],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
    lineup_team_ids_by_player: dict[int, set[int]],
    limit: int = 10,
) -> list[dict]:
    """
    League-wide (both pods): the highest-margin teams that were on some
    player's roster but not included in that player's actual lineup this
    week. A team owned by two different players in two different pods
    (pods are fully severed) can legitimately appear twice here, once
    per owner, if neither played it -- these are evaluated per
    (player, team) ownership, not per team_id alone.
    """
    candidates = []
    for pid in player_ids:
        roster = get_active_roster_teams(pid, season_id)
        roster_ids = [t['team_id'] for t in roster]
        played = lineup_team_ids_by_player.get(pid, set())
        unplayed_ids = [tid for tid in roster_ids if tid not in played]
        if not unplayed_ids:
            continue
        results = get_team_week_results(season_id, week, unplayed_ids)
        pod_id = pod_of_player.get(pid)
        for r in results:
            if r['margin'] is not None and r['margin'] > 0:
                candidates.append({
                    **r,
                    'player_id': pid,
                    'player_name': player_names.get(pid),
                    'pod_name': pod_names.get(pod_id),
                })

    candidates.sort(key=lambda r: r['margin'], reverse=True)
    return candidates[:limit]


def build_top_free_agents(season_id: int, week: int, pod_id: int, limit: int = 10) -> list[dict]:
    """Highest-margin free agents in this pod this week -- pod-scoped, since free agency is completely severed between pods."""
    free_agents = get_available_teams(season_id, pod_id)
    team_ids = [t['team_id'] for t in free_agents]
    results = get_team_week_results(season_id, week, team_ids)
    results = [r for r in results if r['margin'] is not None and r['margin'] > 0]
    results.sort(key=lambda r: r['margin'], reverse=True)
    return results[:limit]


def build_scrappy_by_pod(season_id: int, week: int, pods: list[dict]) -> dict[str, OptimalLineupResult]:
    """{pod_name: OptimalLineupResult} for every pod this season."""
    return {pod['name']: compute_scrappy_optimal(season_id, week, pod['id']) for pod in pods}
