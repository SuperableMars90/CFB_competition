"""
lib/pvp_luck.py
---------------
Pure-Python "how lucky was the PVP schedule?" analysis. No DB access, no
Streamlit -- callers fetch pairings/totals and pass them in.

The PVP schedule is a fixed set of weekly rounds (perfect matchings of the
players) laid out in some order across the season. Each player's weekly
`total_points` is what it is; only *who they were paired against* that week
depends on the order. So the null model is: keep every player's weekly
totals, take the season's full set of rounds, and lay them out in every
possible order. A player's PVP wins through week W under one such order is
the sum, over weeks 1..W, of "did my total beat my opponent's total in
whichever round landed in that week".

This module computes each player's *exact* win distribution over all
orderings (no sampling) with a small DP over which rounds have been used,
then compares actual wins to it.

Win/tie rule matches lib.metagame.compute_pvp_resolutions: higher weekly
total wins; exactly equal is a tie. A tie counts as half a win here, so
wins are tracked internally in half-win units ("halves": win=2, tie=1,
loss=0) to keep every quantity an integer.
"""

from __future__ import annotations

from math import perm, sqrt

Pair = tuple[int, int]
Round = list[Pair]


def score_round(pairs: Round, totals: dict[int, float]) -> dict[int, int]:
    """
    Halves earned by each player in `pairs` given one week's `totals`
    ({player_id: total_points}): 2 for a win, 1 for a tie, 0 for a loss.
    A player who isn't in `pairs` (bye) isn't in the result.
    """
    out: dict[int, int] = {}
    for a, b in pairs:
        ta, tb = totals[a], totals[b]
        if ta == tb:
            out[a] = out[b] = 1
        elif ta > tb:
            out[a], out[b] = 2, 0
        else:
            out[a], out[b] = 0, 2
    return out


def actual_halves(
    pairings_by_week: dict[int, Round],
    totals_by_week: dict[int, dict[int, float]],
) -> dict[int, int]:
    """
    Each player's actual PVP halves over the weeks in `totals_by_week`,
    using the pairings that were actually played those weeks. Every player
    in any week's totals appears in the result (0 if they never played).
    """
    halves = {p: 0 for totals in totals_by_week.values() for p in totals}
    for week, totals in totals_by_week.items():
        for p, h in score_round(pairings_by_week.get(week, []), totals).items():
            halves[p] += h
    return halves


def win_distributions(
    rounds: list[Round],
    totals_by_week: dict[int, dict[int, float]],
) -> dict[int, list[float]]:
    """
    Exact distribution of each player's PVP halves over every ordering of
    the season's `rounds` into the weeks in `totals_by_week`.

    `rounds` is the season's whole pool of rounds (played and unplayed --
    a look-back at week 2 still permutes all 10 rounds, of which 2 land in
    weeks 1-2). `totals_by_week` holds only the weeks being evaluated; its
    length W must be <= len(rounds). An ordering is an ordered choice of W
    distinct rounds, uniformly likely: len(rounds)!/(len(rounds)-W)! of
    them (duplicate-looking rounds are still distinct slots).

    Returns {player_id: probs}, where probs[h] = P(player earned exactly h
    halves), h in 0..2W. A player with `wins` wins and `ties` ties has
    h = 2*wins + ties.
    """
    weeks = sorted(totals_by_week)
    n_weeks, n_rounds = len(weeks), len(rounds)
    if n_weeks > n_rounds:
        raise ValueError(f"{n_weeks} weeks but only {n_rounds} rounds to fill them")

    players = sorted({p for totals in totals_by_week.values() for p in totals})
    # gain[w][r][p] = halves p earns if round r is played in week weeks[w]
    gain = [
        [score_round(rnd, totals_by_week[week]) for rnd in rounds]
        for week in weeks
    ]

    n_orderings = perm(n_rounds, n_weeks)
    result: dict[int, list[float]] = {}
    for p in players:
        # counts[mask][h]: orderings of the weeks so far using exactly the
        # rounds in `mask`, in which p has earned h halves.
        counts: dict[int, list[int]] = {0: [1] + [0] * (2 * n_weeks)}
        for w in range(n_weeks):
            nxt: dict[int, list[int]] = {}
            for mask, row in counts.items():
                for r in range(n_rounds):
                    bit = 1 << r
                    if mask & bit:
                        continue
                    g = gain[w][r].get(p, 0)
                    dest = nxt.setdefault(mask | bit, [0] * (2 * n_weeks + 1))
                    for h, c in enumerate(row):
                        if c:
                            dest[h + g] += c
            counts = nxt
        totals_by_h = [sum(col) for col in zip(*counts.values())]
        result[p] = [c / n_orderings for c in totals_by_h]
    return result


def summarize_luck(
    distributions: dict[int, list[float]],
    actual: dict[int, int],
) -> dict[int, dict]:
    """
    Compare each player's actual halves to their distribution. Per player:
      actual_wins / expected_wins / luck (actual - expected, in wins),
      sd_wins, z (luck / sd; None if sd is 0),
      p_at_most  = P(random order gives <= actual wins)  -- "this unlucky or worse"
      p_at_least = P(random order gives >= actual wins)  -- "this lucky or better"
      p_exact    = P(exactly the actual record's win total)
      pct_mid    = mid-rank percentile, p_at_most - p_exact/2 (0.5 = typical)
    All win figures are in wins (ties = 0.5), not halves.
    """
    out: dict[int, dict] = {}
    for p, probs in distributions.items():
        mean_h = sum(h * pr for h, pr in enumerate(probs))
        var_h = sum(pr * (h - mean_h) ** 2 for h, pr in enumerate(probs))
        a = actual[p]
        p_le = sum(probs[: a + 1])
        p_ge = sum(probs[a:])
        sd = sqrt(var_h) / 2
        out[p] = {
            "actual_wins": a / 2,
            "expected_wins": mean_h / 2,
            "luck": (a - mean_h) / 2,
            "sd_wins": sd,
            "z": ((a - mean_h) / 2 / sd) if sd > 1e-12 else None,
            "p_at_most": p_le,
            "p_at_least": p_ge,
            "p_exact": probs[a],
            "pct_mid": p_le - probs[a] / 2,
        }
    return out
