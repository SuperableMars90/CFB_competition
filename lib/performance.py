"""
lib/performance.py
--------------------
Retroactive performance benchmarks for an already-completed week, built
on lib.optimal_lineup's optimizer. Three named comparisons (Zach,
2026-07-06), a dependency of the post-week summary/recap page:

  - compute_player_optimal() — a real player's own 25-team roster: how
    close did their actual submitted lineup get to the best they could
    have done with the teams they own?
  - compute_scrappy_optimal() — "Scrappy": a synthetic player limited to
    a pod's free-agent pool only (the scraps nobody drafted).
  - compute_max_optimal() — "Max": a synthetic player with access to
    every FBS team that week, no roster constraint at all.

Each returns lib.optimal_lineup.OptimalLineupResult (total + which team
filled which slot) — not a comparison against the player's actual score,
since the caller (the recap page) already has weekly_results.total_points
on hand for that; duplicating the fetch here would be redundant.
"""

from __future__ import annotations

from typing import Optional

from lib.db import (
    get_active_roster_teams,
    get_available_teams,
    get_conference_slot_tiers,
    get_team_week_results,
)
from lib.optimal_lineup import TeamWeekResult, OptimalLineupResult, optimize_lineup


def _optimize(season_id: int, week: int, team_ids, prefer_team_ids=None) -> OptimalLineupResult:
    """Shared plumbing: fetch this week's results for team_ids (None = every FBS team), then optimize."""
    rows = get_team_week_results(season_id, week, team_ids)
    teams = [TeamWeekResult(**row) for row in rows]
    conference_slot_tiers = get_conference_slot_tiers(season_id)
    return optimize_lineup(teams, conference_slot_tiers, prefer_team_ids=prefer_team_ids)


def compute_player_optimal(
    season_id: int,
    week: int,
    player_id: int,
    actual_team_ids: Optional[set[int]] = None,
) -> OptimalLineupResult:
    """
    Best possible lineup from this player's own active roster.

    actual_team_ids: the teams this player actually played this week
    (their real lineup_slots picks), if known. Optional and only used
    to break ties in the player's favor -- e.g. if three teams all net
    +7 and the player played one of them, the reported optimal lineup
    will use that same team rather than an arbitrary other tied option,
    so a genuinely-optimal pick doesn't get flagged as a miss just
    because the solver happened to pick a different equally-good team.
    Never changes the reported total -- see optimize_lineup()'s docstring.
    """
    roster = get_active_roster_teams(player_id, season_id)
    team_ids = [t['team_id'] for t in roster]
    return _optimize(season_id, week, team_ids, prefer_team_ids=actual_team_ids)


def compute_scrappy_optimal(season_id: int, week: int, pod_id: int) -> OptimalLineupResult:
    """
    "Scrappy": best possible lineup from this pod's free-agent pool
    only. Pod-scoped, not season-scoped, since pods are completely
    severed rosters — a team owned in another pod is still a legitimate
    "scrap" here if it's unowned within this pod.
    """
    free_agents = get_available_teams(season_id, pod_id)
    team_ids = [t['team_id'] for t in free_agents]
    return _optimize(season_id, week, team_ids)


def compute_max_optimal(season_id: int, week: int) -> OptimalLineupResult:
    """"Max": best possible lineup from every FBS team that week, no roster constraint at all."""
    return _optimize(season_id, week, None)
