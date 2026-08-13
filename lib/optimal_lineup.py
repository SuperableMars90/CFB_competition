"""
lib/optimal_lineup.py
-----------------------
Retroactive roster optimization: given a pool of teams and their already-
known net margins for a completed week, find the highest-scoring lineup
possible under the same 16-slot structure and eligibility rules as
lib.game_rules (10 conference slots + 3 P4 flex + 2 G6 flex + 1 wildcard,
independents broadened to every conference slot of their own tier).

Pure Python — no database access, no I/O. Callers fetch team/margin data
and pass it in. Reuses lib.game_rules's eligibility rule by construction
(_eligible_categories mirrors validate_pick's exact conditions) rather
than re-deriving it, so this can't silently drift from the real rules —
see that module's docstring for the settled independent-eligibility
decision this depends on.

Used for three retroactive comparisons against a real (or hypothetical)
team pool for a week that's already final:
  - a real player's own full roster (size is season config, see
    lib.db.get_draft_picks_per_player), to see how close their actual
    submitted lineup got to the best they could have done
  - "Scrappy": a pod's free-agent pool only
  - "Max": every FBS team that week, no roster constraint at all

Why this needs real optimization, not a greedy pick-the-best-team-per-
slot pass: an independent team is eligible for every conference slot of
its own tier at once, so it can genuinely compete for more than one
scarce conference slot simultaneously. Assigning it to the first
available slot a greedy pass finds can strand a same-conference-only
team that had nowhere else to go, when sending the independent to a
different slot it's equally eligible for would have let both score.
Worked example verified in tests/test_optimal_lineup.py
(test_exchange_case_beats_naive_greedy): an independent worth 100
eligible for both the ACC and B12 slots, an ACC-only team worth 90, and
a B12-only team worth 80, with no flex/wildcard cushion. Greedily
assigning the independent to ACC (first in iteration order) strands the
ACC-only team: total 180. The optimal answer sends the independent to
B12 instead, freeing ACC for the ACC-only team: total 190.

Solved via successive-shortest-augmenting-path min-cost flow (source ->
team nodes -> eligible-category nodes -> sink), which handles the
negative edge costs (-margin, since flow algorithms minimize cost and
we want to maximize margin) correctly via Bellman-Ford/SPFA on each
iteration. Only teams with a strictly positive margin are ever added to
the graph: a margin <= 0 team can never beat simply passing that slot
(which scores 0), so including it could only ever hurt or do nothing —
this is also why the algorithm never has to decide when to stop early:
every edge in the graph has strictly negative cost, so augmenting for
as long as any source-to-sink path exists is always correct.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

P4_FLEX_CATEGORY = 'P4_FLEX'
G6_FLEX_CATEGORY = 'G6_FLEX'
WILDCARD_CATEGORY = 'WILDCARD'


@dataclass(frozen=True)
class TeamWeekResult:
    """One team's identity + this week's net margin. margin is None for
    a bye (no game that week) — treated identically to a margin <= 0,
    i.e. never worth playing over a pass."""
    team_id: int
    name: str
    conference_abbreviation: str
    tier: str  # 'P4' | 'G6' | 'FCS' (or any tier absent from flex eligibility)
    margin: Optional[int]


@dataclass(frozen=True)
class OptimalPick:
    """One filled slot in the optimal lineup. Slots not listed here were
    (optimally) left passed -- no positive-margin team was available or
    every eligible team was better used elsewhere."""
    category: str  # a conference abbreviation, or P4_FLEX/G6_FLEX/WILDCARD
    team_id: int
    team_name: str
    margin: int


@dataclass(frozen=True)
class OptimalLineupResult:
    total: int
    picks: list[OptimalPick]


def _eligible_categories(team: TeamWeekResult, conference_slot_tiers: dict[str, str]) -> list[str]:
    """
    Every category this team could be assigned to. Mirrors
    lib.game_rules.validate_pick's eligibility conditions exactly:
      - conference slot: exact conference match, OR (team's conference
        isn't a real lineup-slot conference -- i.e. it's an independent
        bucket) and its tier matches that slot's tier, broadened to
        EVERY conference slot of that tier (not just one).
      - P4_FLEX / G6_FLEX: tier match.
      - WILDCARD: P4 or G6 tier.
    """
    categories = []

    if team.conference_abbreviation in conference_slot_tiers:
        categories.append(team.conference_abbreviation)
    else:
        categories.extend(
            abbr for abbr, tier in conference_slot_tiers.items() if tier == team.tier
        )

    if team.tier == 'P4':
        categories.append(P4_FLEX_CATEGORY)
    if team.tier == 'G6':
        categories.append(G6_FLEX_CATEGORY)
    if team.tier in ('P4', 'G6'):
        categories.append(WILDCARD_CATEGORY)

    return categories


class _MinCostFlow:
    """Minimal successive-shortest-augmenting-path min-cost flow, SPFA
    (queue-based Bellman-Ford) for the shortest-path step so negative
    edge costs are handled correctly. Small, self-contained -- this
    problem never has more than a few hundred nodes/edges."""

    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[list[int]]] = [[] for _ in range(n)]  # [to, cap, cost, rev_index]

    def add_edge(self, frm: int, to: int, cap: int, cost: int) -> None:
        self.graph[frm].append([to, cap, cost, len(self.graph[to])])
        self.graph[to].append([frm, 0, -cost, len(self.graph[frm]) - 1])

    def min_cost_max_flow(self, s: int, t: int) -> int:
        total_cost = 0
        while True:
            dist = [float('inf')] * self.n
            dist[s] = 0
            in_queue = [False] * self.n
            prev_node = [-1] * self.n
            prev_edge = [-1] * self.n
            queue = deque([s])
            in_queue[s] = True
            while queue:
                u = queue.popleft()
                in_queue[u] = False
                for i, (to, cap, cost, _rev) in enumerate(self.graph[u]):
                    if cap > 0 and dist[u] + cost < dist[to]:
                        dist[to] = dist[u] + cost
                        prev_node[to] = u
                        prev_edge[to] = i
                        if not in_queue[to]:
                            queue.append(to)
                            in_queue[to] = True

            if dist[t] == float('inf'):
                return total_cost

            path_flow = float('inf')
            v = t
            while v != s:
                u = prev_node[v]
                i = prev_edge[v]
                path_flow = min(path_flow, self.graph[u][i][1])
                v = u

            v = t
            while v != s:
                u = prev_node[v]
                i = prev_edge[v]
                self.graph[u][i][1] -= path_flow
                rev = self.graph[u][i][3]
                self.graph[v][rev][1] += path_flow
                total_cost += path_flow * self.graph[u][i][2]
                v = u


def optimize_lineup(
    teams: list[TeamWeekResult],
    conference_slot_tiers: dict[str, str],
    p4_flex_slots: int = 3,
    g6_flex_slots: int = 2,
    wildcard_slots: int = 1,
    prefer_team_ids: Optional[set[int]] = None,
) -> OptimalLineupResult:
    """
    The highest-scoring lineup this team pool can produce this week.

    conference_slot_tiers: {abbreviation: tier} for this season's real
    lineup-slot conferences (lib.db.get_conference_slot_tiers) -- also
    defines the 10 conference-slot categories (one each, capacity 1).

    prefer_team_ids: optional -- team_ids a real player actually played
    this week. Ties are common (three teams all net +7 are equally
    valid for a slot), and the *total* is identical no matter which tied
    team fills it -- but if we're about to show this result next to a
    player's actual lineup, arbitrarily picking a different tied team
    than the one they actually played would make a genuinely optimal
    pick look like a miss. When given, ties are broken in favor of
    whichever candidate the player actually used, via an integer
    tie-break bonus on a scaled-up cost space (real margins are scaled
    by MARGIN_SCALE, the bonus is a flat 1 in that scaled space) so it
    can only ever decide among truly equal-margin options, never
    override a real difference in margin -- so the returned `total` is
    unaffected; it's computed from the picks' real margins, not the
    flow's internal (bonus-inclusive, scaled) cost.

    Costs are kept as exact integers throughout rather than floats
    (e.g. a raw 0.01 bonus) deliberately -- a real bug caught while
    verifying this live: SPFA's shortest-path relaxation compares costs
    on every iteration, and floating-point rounding noise from mixing
    exact integer margins with a float bonus made it possible for the
    search to never settle (perpetually "improving" a distance by less
    than float precision can reliably represent) once enough preferred
    teams created enough tied/near-tied paths -- a real player's full
    roster (25 teams, at the time this was found) reliably triggered it.
    Integer costs throughout removes the possibility entirely regardless
    of roster size.
    """
    candidates = [t for t in teams if t.margin is not None and t.margin > 0]
    if not candidates:
        return OptimalLineupResult(total=0, picks=[])

    prefer_team_ids = prefer_team_ids or set()
    # Scale real margins up so a flat integer bonus of 1 (per preferred
    # edge) can break ties without ever overriding a real margin
    # difference: MARGIN_SCALE=100 comfortably exceeds TOTAL_LINEUP_SIZE
    # (16), so even every slot being a preferred tie-break (bonus 16 in
    # scaled space) can never outweigh the smallest possible real
    # difference of 1 whole margin point (scaled: 100).
    MARGIN_SCALE = 100
    TIE_BREAK_BONUS = 1

    category_capacity: dict[str, int] = {abbr: 1 for abbr in conference_slot_tiers}
    category_capacity[P4_FLEX_CATEGORY] = p4_flex_slots
    category_capacity[G6_FLEX_CATEGORY] = g6_flex_slots
    category_capacity[WILDCARD_CATEGORY] = wildcard_slots

    # Node layout: 0 = source, 1..len(candidates) = teams,
    # next len(categories) = categories, last = sink.
    category_names = list(category_capacity.keys())
    category_index = {name: i for i, name in enumerate(category_names)}

    source = 0
    team_base = 1
    category_base = team_base + len(candidates)
    sink = category_base + len(category_names)

    flow = _MinCostFlow(sink + 1)

    for i, team in enumerate(candidates):
        team_node = team_base + i
        flow.add_edge(source, team_node, 1, 0)
        bonus = TIE_BREAK_BONUS if team.team_id in prefer_team_ids else 0
        for cat in set(_eligible_categories(team, conference_slot_tiers)):
            if cat not in category_capacity:
                continue  # e.g. a tier with no matching flex category (shouldn't happen, defensive)
            cat_node = category_base + category_index[cat]
            flow.add_edge(team_node, cat_node, 1, -(team.margin * MARGIN_SCALE) - bonus)

    for cat, cap in category_capacity.items():
        flow.add_edge(category_base + category_index[cat], sink, cap, 0)

    flow.min_cost_max_flow(source, sink)

    picks = []
    for i, team in enumerate(candidates):
        team_node = team_base + i
        for to, cap, cost, _rev in flow.graph[team_node]:
            # Forward edges to categories start at cap=1; cap==0 means used.
            if category_base <= to < sink and cap == 0 and cost < 0:
                cat_name = category_names[to - category_base]
                picks.append(OptimalPick(
                    category=cat_name, team_id=team.team_id, team_name=team.name, margin=team.margin,
                ))
                break

    # Computed from the picks' real margins, not the flow's own cost
    # tally -- that includes the tie-break bonus above, which must never
    # leak into the reported total.
    return OptimalLineupResult(total=sum(p.margin for p in picks), picks=picks)
