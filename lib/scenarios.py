"""
lib/scenarios.py
----------------
"What if" standings for the rest of the PVP regular season: take every
matchup already played, add a hypothetical winner for any subset of the
still-unresolved ones, and rank the result the same way final playoff
seeding does (lib.seeding's criteria 1-4).

Hypothetical games carry no point margin -- there are no scores to make
one up from -- so they count as margin 0. Net-margin tiebreakers
(criteria 2/3) therefore reflect only games actually played. A group
that still can't be separated (no manual override on file for it) is
shown as tied rather than raising, since a scenario isn't final seeding.

One DB read (get_regular_season_state); everything else is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lib.db import get_connection
from lib.playoffs import (
    assign_seeds_4_team,
    assign_seeds_6_team,
    assign_seeds_8_team,
    bracket_format_for_league_shape,
)
from lib.seeding import (
    MatchupRecord,
    _classes_desc,
    record_and_margin,
    resolve_tiebreak_tiers,
    win_pct,
)


@dataclass(frozen=True)
class RemainingMatchup:
    pairing_id: int
    week: int
    player_a_id: int
    player_b_id: int


# ------------------------------------------------------------------
# DB reads
# ------------------------------------------------------------------

def get_pvp_regular_season_end_week(season_id: int) -> Optional[int]:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT pvp_regular_season_end_week FROM seasons WHERE id = %s", (season_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


def get_regular_season_state(
    season_id: int, scoring_context_id: int, cutoff_week: int,
) -> tuple[dict[int, list[MatchupRecord]], list[RemainingMatchup]]:
    """
    Every regular-season pairing through cutoff_week, split into played
    records and still-unresolved matchups -- see split_pairings().
    """
    query = """
        SELECT mp.id, mp.week, mp.player_a_id, mp.player_b_id, mp.winner_id, mp.is_tie,
               wa.total_points AS a_points, wb.total_points AS b_points
        FROM matchup_pairings mp
        LEFT JOIN weekly_results wa
            ON wa.player_id = mp.player_a_id AND wa.season_id = mp.season_id
           AND wa.scoring_context_id = mp.scoring_context_id AND wa.week = mp.week
        LEFT JOIN weekly_results wb
            ON wb.player_id = mp.player_b_id AND wb.season_id = mp.season_id
           AND wb.scoring_context_id = mp.scoring_context_id AND wb.week = mp.week
        WHERE mp.season_id = %s AND mp.scoring_context_id = %s AND mp.week <= %s
        ORDER BY mp.week, mp.id
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, scoring_context_id, cutoff_week))
        rows = cur.fetchall()
        cur.close()
    return split_pairings(rows)


# ------------------------------------------------------------------
# Pure computation
# ------------------------------------------------------------------

def split_pairings(
    rows: list[dict],
) -> tuple[dict[int, list[MatchupRecord]], list[RemainingMatchup]]:
    """
    rows: [{id, week, player_a_id, player_b_id, winner_id, is_tie,
    a_points, b_points}, ...]. A pairing is played once winner_id is set
    or is_tie is true (resolve_pvp_matchups() has run for it); anything
    else is remaining. Returns ({player_id: [MatchupRecord, ...]},
    [RemainingMatchup, ...]); every player in any row gets a records key,
    even with no games played yet.
    """
    records: dict[int, list[MatchupRecord]] = {}
    remaining: list[RemainingMatchup] = []
    for row in rows:
        a, b = row['player_a_id'], row['player_b_id']
        records.setdefault(a, [])
        records.setdefault(b, [])
        if row['winner_id'] is None and not row['is_tie']:
            remaining.append(RemainingMatchup(row['id'], row['week'], a, b))
            continue
        if row['a_points'] is not None and row['b_points'] is not None:
            margin = row['a_points'] - row['b_points']
        else:
            margin = 0
        if row['is_tie']:
            ra, rb = 'T', 'T'
        elif row['winner_id'] == a:
            ra, rb = 'W', 'L'
        else:
            ra, rb = 'L', 'W'
        records[a].append(MatchupRecord(row['week'], b, ra, margin))
        records[b].append(MatchupRecord(row['week'], a, rb, -margin))
    return records, remaining


def apply_picks(
    records: dict[int, list[MatchupRecord]],
    remaining: list[RemainingMatchup],
    picks: dict[int, int],
) -> dict[int, list[MatchupRecord]]:
    """
    New records with a margin-0 hypothetical result added for each
    remaining matchup that has a pick ({pairing_id: winner player_id}).
    Unpicked matchups are left out entirely. `records` isn't modified.
    """
    out = {pid: list(recs) for pid, recs in records.items()}
    for m in remaining:
        winner = picks.get(m.pairing_id)
        if winner is None:
            continue
        if winner not in (m.player_a_id, m.player_b_id):
            raise ValueError(f"pick {winner} isn't in pairing {m.pairing_id}")
        loser = m.player_b_id if winner == m.player_a_id else m.player_a_id
        out.setdefault(winner, []).append(MatchupRecord(m.week, loser, 'W', 0))
        out.setdefault(loser, []).append(MatchupRecord(m.week, winner, 'L', 0))
    return out


def rank_standings(
    all_records: dict[int, list[MatchupRecord]],
    manual_tiebreaks: dict[frozenset, list[int]],
) -> list[dict]:
    """
    Like lib.seeding.compute_seeding(), but a group the tiebreakers can't
    separate shares a rank instead of raising. Returns
    [{rank, tied, player_id, wins, losses, ties, win_pct, net_margin}, ...]
    best first; tied players share `rank` (the next tier skips ahead,
    e.g. 1, 2, 2, 4) and have tied=True.
    """
    players = set(all_records.keys())
    overall = {pid: record_and_margin(all_records[pid], None) for pid in players}
    overall_pct = {pid: win_pct(*overall[pid][:3]) for pid in players}

    tiers: list[set[int]] = []
    for cls in _classes_desc(players, overall_pct):
        tiers.extend(resolve_tiebreak_tiers(cls, all_records, manual_tiebreaks))

    rows: list[dict] = []
    for tier in tiers:
        rank = len(rows) + 1
        for pid in sorted(tier):
            w, l, t, margin = overall[pid]
            rows.append({
                'rank': rank, 'tied': len(tier) > 1, 'player_id': pid,
                'wins': w, 'losses': l, 'ties': t,
                'win_pct': float(overall_pct[pid]), 'net_margin': margin,
            })
    return rows


_SEED_ASSIGNERS = {
    'four_team': lambda seeding, _pods: assign_seeds_4_team(seeding),
    'six_team': assign_seeds_6_team,
    'eight_team': assign_seeds_8_team,
}


def projected_playoff_seeds(
    standings: list[dict], pod_of_player: dict[int, int],
) -> dict[int, int]:
    """
    {player_id: playoff seed} from rank_standings() output, using the
    same pod-aware seed assignment as scripts/seed_playoffs.py. Only
    meaningful once standings has no ties -- raises ValueError otherwise,
    or if the league shape has no confirmed bracket format.
    """
    if any(r['tied'] for r in standings):
        raise ValueError("standings still have unresolved ties")
    fmt = bracket_format_for_league_shape(len(set(pod_of_player.values())), len(pod_of_player))
    seeding = [{'seed': r['rank'], 'player_id': r['player_id']} for r in standings]
    seed_to_player = _SEED_ASSIGNERS[fmt](seeding, pod_of_player)
    return {pid: s for s, pid in seed_to_player.items()}
