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
  - build_unplayed_games()       -- the "Unplayed Games" block: three
                                     league-wide top-10 lists (redundant /
                                     wasted / unowned), each row pod-tagged
  - build_pod_play_breakdown()   -- the "Games by Pod & Ownership" block
                                     (two-pod seasons): every started team
                                     split by other-pod play/ownership, plus
                                     head-to-head games where both teams
                                     were started
  - compare_players_by_teams()   -- "Pick Similarity" subsection 1:
                                     cross-pod player pairs compared by the
                                     teams they picked, ignoring slot --
                                     count of matching teams + summed
                                     |margin| of the shared teams
  - compare_players_by_slot_points() -- "Pick Similarity" subsection 2:
                                     every player pair compared slot for
                                     slot on the points each slot produced
                                     (conference slots by conference, flex
                                     slots ranked best-to-best)
  - lib.performance.compute_max_optimal() -- the Max lineup (reused as-is,
    no wrapper needed here)
"""

from __future__ import annotations

from typing import Optional

from lib.db import (
    get_active_roster_teams,
    get_available_teams,
    get_owned_team_ids,
    get_team_week_results,
    get_week_team_games,
)
from lib.optimal_lineup import (
    G6_FLEX_CATEGORY,
    P4_FLEX_CATEGORY,
    WILDCARD_CATEGORY,
    OptimalLineupResult,
    TeamWeekResult,
    _eligible_categories,
)
from lib.performance import compute_player_optimal, compute_scrappy_optimal

# The 16-slot lineup shape (10 conference + 3 P4 flex + 2 G6 flex + 1 wild
# card) is a fixed game rule, not pod-format config -- see CLAUDE.md.
# These mirror optimize_lineup()'s same-named parameter defaults.
_P4_FLEX_SLOTS = 3
_G6_FLEX_SLOTS = 2
_WILDCARD_SLOTS = 1


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


def _margin(team: TeamWeekResult) -> int:
    """Margin as a plain int -- a bye (None) counts as 0, i.e. an
    occupied-but-scoreless slot, never a help."""
    return team.margin or 0


def _slot_capacities(conference_slot_tiers: dict[str, str]) -> dict[str, int]:
    """{category: number of slots}. One per conference, plus the fixed
    flex / wild-card counts."""
    caps = {abbr: 1 for abbr in conference_slot_tiers}
    caps[P4_FLEX_CATEGORY] = _P4_FLEX_SLOTS
    caps[G6_FLEX_CATEGORY] = _G6_FLEX_SLOTS
    caps[WILDCARD_CATEGORY] = _WILDCARD_SLOTS
    return caps


def greedy_arrange_started_picks(
    started: list[TeamWeekResult],
    conference_slot_tiers: dict[str, str],
) -> dict[str, list[TeamWeekResult]]:
    """
    Arrange one player's actually-started teams into the 16-slot
    structure to maximise total, without the min-cost-flow machinery of
    lib.optimal_lineup: each conference slot takes that player's best
    still-unplaced team native to the conference (falling back to an
    unplaced independent of the slot's tier), then P4 flex, G6 flex and
    the wild card take the best unplaced tier-eligible teams left.

    A deliberately "lazy" normalisation (Zach, 2026-09): once every
    conference slot holds the best eligible team for that conference,
    swapping a bench team into a flex or wild-card slot can't open up a
    better placement in some other conference, so a single pairwise
    comparison against this arrangement is enough to tell whether a
    benched team could have helped -- no cascading re-solve needed.

    Unlike optimize_lineup(), non-positive and bye picks are kept in
    place: the arrangement has to reflect every slot the player really
    used (a losing pick still occupied its slot), so a bench team gets
    measured against what was actually there, not an idealised blank.

    Independent teams eligible for several conference slots are placed
    first-fit after natives; a rare over-subscription just leaves the odd
    team unplaced (dropped from the result) -- acceptable here.
    """
    remaining = sorted(started, key=_margin, reverse=True)
    arranged: dict[str, list[TeamWeekResult]] = {}

    def take(pred) -> Optional[TeamWeekResult]:
        for i, team in enumerate(remaining):
            if pred(team):
                return remaining.pop(i)
        return None

    for abbr, tier in conference_slot_tiers.items():
        pick = take(lambda t, a=abbr: t.conference_abbreviation == a)
        if pick is None:
            pick = take(
                lambda t, tr=tier: t.conference_abbreviation not in conference_slot_tiers
                and t.tier == tr
            )
        if pick is not None:
            arranged[abbr] = [pick]

    for category, count, tiers in (
        (P4_FLEX_CATEGORY, _P4_FLEX_SLOTS, ('P4',)),
        (G6_FLEX_CATEGORY, _G6_FLEX_SLOTS, ('G6',)),
        (WILDCARD_CATEGORY, _WILDCARD_SLOTS, ('P4', 'G6')),
    ):
        for _ in range(count):
            pick = take(lambda t, tt=tiers: t.tier in tt)
            if pick is not None:
                arranged.setdefault(category, []).append(pick)

    return arranged


def classify_bench_team(
    team: TeamWeekResult,
    arranged: dict[str, list[TeamWeekResult]],
    capacities: dict[str, int],
    conference_slot_tiers: dict[str, str],
) -> Optional[dict]:
    """
    Decide whether one benched team's points were 'wasted' (some slot it
    was eligible for held a weaker team, so swapping it in would have
    raised the total) or 'redundant' (every eligible slot already held a
    team scoring at least as much -- the points had nowhere to go),
    measured against `arranged` (greedy_arrange_started_picks() output).

    Returns one of:
      {'kind': 'wasted', 'gain': int, 'replaced_category': str,
       'replaced_team': str | None}   -- best swap strictly positive;
       replaced_team is None when the beaten slot was an outright pass.
      {'kind': 'redundant', 'score': int}   -- best swap strictly
       negative AND the team actually scored (margin > 0).
      None   -- an exact wash (best swap == 0), or a team whose best swap
       is negative but that didn't score: nothing to show.
    """
    margin = _margin(team)
    best_gain: Optional[int] = None
    best_category: Optional[str] = None
    best_replaced: Optional[str] = None

    for category in _eligible_categories(team, conference_slot_tiers):
        occupants = arranged.get(category, [])
        if len(occupants) < capacities.get(category, 0):
            gain, replaced = margin, None            # a passed / unfilled slot
        else:
            weakest = min(occupants, key=_margin)
            gain, replaced = margin - _margin(weakest), weakest.name
        if best_gain is None or gain > best_gain:
            best_gain, best_category, best_replaced = gain, category, replaced

    if best_gain is None:
        return None
    if best_gain > 0:
        return {
            'kind': 'wasted',
            'gain': best_gain,
            'replaced_category': best_category,
            'replaced_team': best_replaced,
        }
    if best_gain < 0 and margin > 0:
        return {'kind': 'redundant', 'score': margin}
    return None


def _build_unowned_scores(
    season_id: int,
    week: int,
    pods: list[dict],
    pod_names: dict[int, str],
    limit: int,
) -> list[dict]:
    """
    Top `limit` teams with no roster owner this week, by net margin,
    deduplicated to one row per team. `pod` is 'both' when the team is a
    free agent in every pod, otherwise the pod name(s) it's available in
    -- free agency is pod-scoped (pods are severed rosters), so a team
    can be owned in one pod and a free agent in another.
    """
    unowned_in: dict[int, set[int]] = {}
    for pod in pods:
        for team in get_available_teams(season_id, pod['id']):
            unowned_in.setdefault(team['team_id'], set()).add(pod['id'])
    if not unowned_in:
        return []

    pod_count = len(pods)
    rows = []
    for r in get_team_week_results(season_id, week, list(unowned_in)):
        if r['margin'] is None:
            continue
        pod_ids = unowned_in[r['team_id']]
        if pod_count > 1 and len(pod_ids) == pod_count:
            pod_label = 'both' if pod_count == 2 else 'all'
        else:
            pod_label = ', '.join(sorted(pod_names.get(p, '?') for p in pod_ids))
        rows.append({
            'team_id': r['team_id'],
            'team_name': r['name'],
            'conference': r['conference_abbreviation'],
            'margin': r['margin'],
            'pod': pod_label,
        })
    rows.sort(key=lambda r: r['margin'], reverse=True)
    return rows[:limit]


def build_unplayed_games(
    season_id: int,
    week: int,
    player_ids: list[int],
    player_names: dict[int, str],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
    lineup_team_ids_by_player: dict[int, set[int]],
    pods: list[dict],
    conference_slot_tiers: dict[str, str],
    limit: int = 10,
) -> dict:
    """
    The recap's "Unplayed Games" block: three league-wide top-`limit`
    lists, each row tagged with the pod it applies to.

      redundant -- owned, had a game, left on the bench, scored points,
        but every lineup slot it was eligible for already held a team
        scoring at least as much (measured against the owner's own
        started teams, greedily re-arranged). Ranked by the team's own
        margin.
      wasted -- same population, but some eligible slot held a weaker
        team; ranked by how many points swapping it in would have added
        (this team's margin minus the team it would have replaced, or its
        full margin against a slot the owner passed).
      unowned -- highest-scoring teams with no roster owner, one row per
        team; pod is a single name or 'both'.

    A team benched by two owners in different pods produces two
    redundant/wasted rows (the points-left value is owner-specific), but
    only one unowned row.
    """
    capacities = _slot_capacities(conference_slot_tiers)
    redundant: list[dict] = []
    wasted: list[dict] = []

    for pid in player_ids:
        started = lineup_team_ids_by_player.get(pid, set())
        roster_ids = [t['team_id'] for t in get_active_roster_teams(pid, season_id)]
        bench_ids = [tid for tid in roster_ids if tid not in started]
        if not bench_ids or not started:
            continue

        arranged = greedy_arrange_started_picks(
            [TeamWeekResult(**r) for r in get_team_week_results(season_id, week, list(started))],
            conference_slot_tiers,
        )
        tag = {
            'player_id': pid,
            'player_name': player_names.get(pid),
            'pod_name': pod_names.get(pod_of_player.get(pid)),
        }
        for r in get_team_week_results(season_id, week, bench_ids):
            team = TeamWeekResult(**r)
            if team.margin is None:
                continue
            verdict = classify_bench_team(team, arranged, capacities, conference_slot_tiers)
            if verdict is None:
                continue
            base = {
                'team_id': team.team_id,
                'team_name': team.name,
                'conference': team.conference_abbreviation,
                **tag,
            }
            if verdict['kind'] == 'wasted':
                wasted.append({
                    **base,
                    'gain': verdict['gain'],
                    'replaced_category': verdict['replaced_category'],
                    'replaced_team': verdict['replaced_team'],
                })
            else:
                redundant.append({**base, 'score': verdict['score']})

    redundant.sort(key=lambda r: r['score'], reverse=True)
    wasted.sort(key=lambda r: r['gain'], reverse=True)

    return {
        'redundant': redundant[:limit],
        'wasted': wasted[:limit],
        'unowned': _build_unowned_scores(season_id, week, pods, pod_names, limit),
    }


def _played_by_pod(
    played_by_player: dict[int, set[int]],
    pod_of_player: dict[int, int],
) -> dict[int, dict[int, list[int]]]:
    """{pod_id: {team_id: [player_id, ...]}} -- who in each pod started
    each team this week. Players with no pod assignment are skipped."""
    out: dict[int, dict[int, list[int]]] = {}
    for pid, team_ids in played_by_player.items():
        pod_id = pod_of_player.get(pid)
        if pod_id is None:
            continue
        bucket = out.setdefault(pod_id, {})
        for tid in team_ids:
            bucket.setdefault(tid, []).append(pid)
    return out


def classify_pod_play(
    played_by_pod: dict[int, dict[int, list[int]]],
    owned_by_pod: dict[int, set[int]],
    pod_a_id: int,
    pod_b_id: int,
) -> dict[str, list[dict]]:
    """
    Split every team started in either pod this week into five buckets
    (a two-pod comparison). Each entry is
    {'team_id', 'players_by_pod': {pod_id: [player_id, ...]}}, with
    players_by_pod restricted to the pod(s) relevant to the bucket.

      'both'              -- started in both pods
      'owned_both_only_a' -- owned in both pods, started only in pod A
      'owned_both_only_b' -- owned in both pods, started only in pod B
      'only_a_unowned_b'  -- started in pod A, no active owner in pod B
      'only_b_unowned_a'  -- started in pod B, no active owner in pod A

    "started implies owned in the starting pod" is assumed; a team
    dropped after it was started can make an 'owned in both' label
    slightly generous -- acceptable for a retrospective snapshot.
    """
    a_played = played_by_pod.get(pod_a_id, {})
    b_played = played_by_pod.get(pod_b_id, {})
    b_owned = owned_by_pod.get(pod_b_id, set())
    a_owned = owned_by_pod.get(pod_a_id, set())

    buckets: dict[str, list[dict]] = {
        k: [] for k in
        ('both', 'owned_both_only_a', 'owned_both_only_b', 'only_a_unowned_b', 'only_b_unowned_a')
    }
    for tid in sorted(set(a_played) | set(b_played)):
        in_a, in_b = tid in a_played, tid in b_played
        if in_a and in_b:
            buckets['both'].append({
                'team_id': tid,
                'players_by_pod': {pod_a_id: a_played[tid], pod_b_id: b_played[tid]},
            })
        elif in_a:
            key = 'owned_both_only_a' if tid in b_owned else 'only_a_unowned_b'
            buckets[key].append({'team_id': tid, 'players_by_pod': {pod_a_id: a_played[tid]}})
        else:
            key = 'owned_both_only_b' if tid in a_owned else 'only_b_unowned_a'
            buckets[key].append({'team_id': tid, 'players_by_pod': {pod_b_id: b_played[tid]}})
    return buckets


def find_played_matchups(
    played_by_pod: dict[int, dict[int, list[int]]],
    team_games: dict[int, dict],
) -> list[dict]:
    """
    Real games this week where BOTH teams were started by some player
    (either pod). One row per game_id:
    {'game_id', 'team_id', 'opponent_id', 'margin' (team_id's side),
     'players_by_pod', 'opp_players_by_pod'}. The caller decides how to
    orient winner/loser for display.
    """
    players_of: dict[int, dict[int, list[int]]] = {}
    for pod_id, teams in played_by_pod.items():
        for tid, pids in teams.items():
            players_of.setdefault(tid, {})[pod_id] = pids

    played = set(players_of)
    seen: set[int] = set()
    rows = []
    for tid in sorted(played):
        game = team_games.get(tid)
        if not game or game['opponent_id'] not in played or game['game_id'] in seen:
            continue
        seen.add(game['game_id'])
        rows.append({
            'game_id': game['game_id'],
            'team_id': tid,
            'opponent_id': game['opponent_id'],
            'margin': game['margin'],
            'players_by_pod': players_of[tid],
            'opp_players_by_pod': players_of[game['opponent_id']],
        })
    return rows


def build_pod_play_breakdown(
    season_id: int,
    week: int,
    played_by_player: dict[int, set[int]],
    pod_of_player: dict[int, int],
    player_names: dict[int, str],
    pods: list[dict],
) -> Optional[dict]:
    """
    The recap's "Games by Pod & Ownership" block -- a two-pod comparison,
    so None unless the season has exactly two pods. Every team started in
    either pod this week, split by whether the other pod also started /
    owns it (classify_pod_play), plus a head-to-head list of real games
    where both teams were started (find_played_matchups). Every row
    carries the starting player(s), grouped by pod name.
    """
    if len(pods) != 2:
        return None

    pod_a, pod_b = pods[0], pods[1]
    pod_name = {p['id']: p['name'] for p in pods}

    played_by_pod = _played_by_pod(played_by_player, pod_of_player)
    all_team_ids = sorted({tid for teams in played_by_pod.values() for tid in teams})
    if not all_team_ids:
        return None

    meta = {r['team_id']: r for r in get_team_week_results(season_id, week, all_team_ids)}
    team_games = get_week_team_games(season_id, week, all_team_ids)
    owned_by_pod = {p['id']: get_owned_team_ids(season_id, p['id']) for p in pods}

    def names(players_by_pod: dict[int, list[int]]) -> list[dict]:
        return [
            {'podName': pod_name[pod_id], 'playerName': player_names.get(pid)}
            for pod_id, pids in players_by_pod.items()
            for pid in pids
        ]

    def team_row(entry: dict) -> dict:
        m = meta.get(entry['team_id'], {})
        return {
            'teamName': m.get('name'),
            'conference': m.get('conference_abbreviation'),
            'margin': m.get('margin'),
            'players': names(entry['players_by_pod']),
        }

    buckets = classify_pod_play(played_by_pod, owned_by_pod, pod_a['id'], pod_b['id'])
    a_name, b_name = pod_a['name'], pod_b['name']
    categories = [
        {'label': 'Started in both pods', 'rows': [team_row(e) for e in buckets['both']]},
        {'label': f'Owned in both pods, started only in {a_name}',
         'rows': [team_row(e) for e in buckets['owned_both_only_a']]},
        {'label': f'Owned in both pods, started only in {b_name}',
         'rows': [team_row(e) for e in buckets['owned_both_only_b']]},
        {'label': f'Started in {a_name}, not owned in {b_name}',
         'rows': [team_row(e) for e in buckets['only_a_unowned_b']]},
        {'label': f'Started in {b_name}, not owned in {a_name}',
         'rows': [team_row(e) for e in buckets['only_b_unowned_a']]},
    ]
    for category in categories:
        category['rows'].sort(
            key=lambda r: (r['margin'] is None, -(r['margin'] or 0), r['teamName'] or '')
        )

    matchups = []
    for mm in find_played_matchups(played_by_pod, team_games):
        tid, opp, margin = mm['team_id'], mm['opponent_id'], mm['margin']
        t_players, o_players = mm['players_by_pod'], mm['opp_players_by_pod']
        if margin is not None and margin < 0:                # orient winner first
            tid, opp, margin = opp, tid, -margin
            t_players, o_players = o_players, t_players
        t_meta, o_meta = meta.get(tid, {}), meta.get(opp, {})
        matchups.append({
            'gameId': mm['game_id'],
            'teamName': t_meta.get('name'),
            'teamConference': t_meta.get('conference_abbreviation'),
            'teamMargin': margin,
            'teamPlayers': names(t_players),
            'oppName': o_meta.get('name'),
            'oppConference': o_meta.get('conference_abbreviation'),
            'oppPlayers': names(o_players),
        })
    matchups.sort(key=lambda r: (r['teamMargin'] is None, -(r['teamMargin'] or 0), r['teamName'] or ''))

    return {'categories': categories, 'matchups': matchups}


def build_scrappy_by_pod(season_id: int, week: int, pods: list[dict]) -> dict[str, OptimalLineupResult]:
    """{pod_name: OptimalLineupResult} for every pod this season."""
    return {pod['name']: compute_scrappy_optimal(season_id, week, pod['id']) for pod in pods}


# ------------------------------------------------------------------
# Pick Similarity -- how alike were two players' picks this week
# ------------------------------------------------------------------

# The flex / wild-card slot types have no per-slot identity to pair on
# (a player's three P4-flex slots are interchangeable), so they bucket
# by type; conference slots bucket by their conference abbreviation.
_FLEX_SLOT_KEYS = {'p4_flex': 'P4 Flex', 'g6_flex': 'G6 Flex', 'wildcard': 'Wild Card'}


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def compare_players_by_teams(
    lineup_team_ids_by_player: dict[int, set[int]],
    team_margin: dict[int, Optional[int]],
    team_meta: dict[int, dict],
    player_names: dict[int, str],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
) -> list[dict]:
    """
    Cross-pod player pairs compared by the teams they picked this week,
    ignoring which slot each team filled. Same-pod players can never
    share a team (pods are severed rosters), so only cross-pod pairs are
    compared -- the result is [] for a single-pod season.

    Two independent similarity measures per pair (Zach, 2026-09):
      matchingTeams  -- plain count of teams both players picked (0-16).
      sharedImpact   -- sum of |margin| over those shared teams. A team
                        that lost by 20 counts the same as one that won
                        by 20: either way, that game moved both players'
                        weeks by the same amount, so it's a shared swing.
    sharedTeams lists the shared teams (name, conference, signed margin),
    ordered by |margin| desc. Rows come back sorted by matchingTeams
    desc then sharedImpact desc; the renderer re-sorts for its second
    table. A shared team whose game has no score yet (margin None) counts
    as 0 impact but still counts toward matchingTeams.
    """
    pods_present = {pod_of_player.get(pid) for pid in lineup_team_ids_by_player}
    pods_present.discard(None)
    if len(pods_present) < 2:
        return []

    def impact(tid: int) -> int:
        v = team_margin.get(tid)
        return abs(v) if v is not None else 0

    rows = []
    pids = sorted(lineup_team_ids_by_player)
    for idx, a in enumerate(pids):
        for b in pids[idx + 1:]:
            pod_a, pod_b = pod_of_player.get(a), pod_of_player.get(b)
            if pod_a is None or pod_b is None or pod_a == pod_b:
                continue
            shared = lineup_team_ids_by_player[a] & lineup_team_ids_by_player[b]
            shared_teams = sorted(
                (
                    {
                        'teamId': tid,
                        'teamName': team_meta.get(tid, {}).get('name'),
                        'conference': team_meta.get(tid, {}).get('conference'),
                        'margin': team_margin.get(tid),
                    }
                    for tid in shared
                ),
                key=lambda r: abs(r['margin']) if r['margin'] is not None else 0,
                reverse=True,
            )
            rows.append({
                'aPlayerId': a, 'aName': player_names.get(a), 'aPodName': pod_names.get(pod_a),
                'bPlayerId': b, 'bName': player_names.get(b), 'bPodName': pod_names.get(pod_b),
                'matchingTeams': len(shared),
                'sharedImpact': sum(impact(tid) for tid in shared),
                'sharedTeams': shared_teams,
            })
    rows.sort(key=lambda r: (r['matchingTeams'], r['sharedImpact']), reverse=True)
    return rows


def _slot_margins_by_key(slots: list[dict]) -> dict[str, list[int]]:
    """
    {slot_key: [margin, ...]} for one player's slots. A conference slot's
    key is its conference abbreviation (its `label`); the flex / wild-card
    slots bucket by type. Byes and passes count as 0, matching
    compute_player_totals().
    """
    out: dict[str, list[int]] = {}
    for s in slots:
        slot_type = s['slot_type']
        key = s['label'] if slot_type == 'conference' else _FLEX_SLOT_KEYS.get(slot_type, slot_type)
        out.setdefault(key, []).append(s['margin'] or 0)
    return out


def compare_players_by_slot_points(
    slots_by_player: dict[int, list[dict]],
    player_names: dict[int, str],
    pod_of_player: dict[int, int],
    pod_names: dict[int, str],
) -> list[dict]:
    """
    Every player pair (both pods), compared slot for slot on the points
    each slot produced -- not on team identity. A team in a player's SEC
    slot is only ever compared against the other player's SEC slot, never
    against the same team sitting in their flex (Zach, 2026-09). The
    three P4-flex slots (and the two G6-flex slots) have no natural
    identity, so within each of those buckets both players' picks are
    ranked by margin and compared rank-for-rank -- best flex vs best flex.

    Per slot the distance is |marginA - marginB|; summed over every slot
    that gives slotPointDistance (lower = more alike). Sign agreement is
    tracked separately as sameDirectionSlots: slots where both picks won,
    both lost, or both landed on exactly zero -- so "we both ate a
    blowout in the same spot" reads as similar, not just "we both scored".
    Rows come back sorted by slotPointDistance asc, then
    sameDirectionSlots desc.
    """
    rows = []
    pids = sorted(slots_by_player)
    for idx, a in enumerate(pids):
        for b in pids[idx + 1:]:
            a_keys = _slot_margins_by_key(slots_by_player[a])
            b_keys = _slot_margins_by_key(slots_by_player[b])
            distance = same_dir = compared = 0
            for key in set(a_keys) | set(b_keys):
                a_list = sorted(a_keys.get(key, []), reverse=True)
                b_list = sorted(b_keys.get(key, []), reverse=True)
                for margin_a, margin_b in zip(a_list, b_list):
                    distance += abs(margin_a - margin_b)
                    if _sign(margin_a) == _sign(margin_b):
                        same_dir += 1
                    compared += 1
            pod_a, pod_b = pod_of_player.get(a), pod_of_player.get(b)
            rows.append({
                'aPlayerId': a, 'aName': player_names.get(a), 'aPodName': pod_names.get(pod_a),
                'bPlayerId': b, 'bName': player_names.get(b), 'bPodName': pod_names.get(pod_b),
                'slotPointDistance': distance,
                'sameDirectionSlots': same_dir,
                'slotsCompared': compared,
            })
    rows.sort(key=lambda r: (r['slotPointDistance'], -r['sameDirectionSlots']))
    return rows
