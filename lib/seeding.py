"""
lib/seeding.py
--------------
Playoff seeding: ranks players by PVP regular-season record, resolving
ties via a 4-criteria procedure (record vs. tied group -> net margin vs.
tied group -> overall net margin -> manual commissioner override).

**End-of-regular-season only.** This is for final seeding once the PVP
regular season has fully concluded (all weeks through
seasons.pvp_regular_season_end_week played) -- not for a mid-season
"current standings" view. Mid-season, not every player has necessarily
played every other player yet, and ties there should simply display as
ties, with no forced resolution. Callers (e.g. scripts/break_tie.py)
should confirm the regular season has actually concluded before relying
on compute_seeding()'s output as final.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from lib.db import get_connection


@dataclass(frozen=True)
class MatchupRecord:
    week: int
    opponent_id: int
    result: str        # 'W' | 'L' | 'T'
    margin: int         # this player's total_points - opponent's, that week


class UnresolvedTieError(Exception):
    """
    Raised when criteria 1-3 (and no manual override) fail to fully
    separate a tied group. Callers (scripts/break_tie.py) should catch
    this, prompt for a manual decision, persist it via
    save_manual_tiebreak(), and retry.
    """
    def __init__(self, group: set[int]):
        self.group = group
        super().__init__(f"Unresolved tie among player_ids {sorted(group)}")


# ------------------------------------------------------------------
# DB reads / writes
# ------------------------------------------------------------------

def get_pvp_regular_season_records(
    season_id: int, scoring_context_id: int, cutoff_week: int,
) -> dict[int, list[MatchupRecord]]:
    """
    Read matchup_pairings joined to weekly_results (both sides) for
    week <= cutoff_week, and build {player_id: [MatchupRecord, ...]}
    from each player's own perspective. A matchup not yet scored (no
    weekly_results row for that week) is naturally excluded via the
    inner join, not returned as a placeholder.
    """
    query = """
        SELECT mp.week, mp.player_a_id, mp.player_b_id, mp.winner_id, mp.is_tie,
               wa.total_points AS a_points, wb.total_points AS b_points
        FROM matchup_pairings mp
        JOIN weekly_results wa
            ON wa.player_id = mp.player_a_id AND wa.season_id = mp.season_id
           AND wa.scoring_context_id = mp.scoring_context_id AND wa.week = mp.week
        JOIN weekly_results wb
            ON wb.player_id = mp.player_b_id AND wb.season_id = mp.season_id
           AND wb.scoring_context_id = mp.scoring_context_id AND wb.week = mp.week
        WHERE mp.season_id = %s AND mp.scoring_context_id = %s AND mp.week <= %s
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, scoring_context_id, cutoff_week))
        rows = cur.fetchall()
        cur.close()

    records: dict[int, list[MatchupRecord]] = defaultdict(list)
    for row in rows:
        margin = row['a_points'] - row['b_points']
        if row['is_tie']:
            ra, rb = 'T', 'T'
        elif row['winner_id'] == row['player_a_id']:
            ra, rb = 'W', 'L'
        else:
            ra, rb = 'L', 'W'
        records[row['player_a_id']].append(
            MatchupRecord(row['week'], row['player_b_id'], ra, margin)
        )
        records[row['player_b_id']].append(
            MatchupRecord(row['week'], row['player_a_id'], rb, -margin)
        )
    return dict(records)


def get_manual_tiebreaks(season_id: int, scoring_context_id: int) -> dict[frozenset, list[int]]:
    """{frozenset(tied_player_ids): [resolved order, best to worst]}"""
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT tied_player_ids, resolved_order FROM manual_tiebreaks "
            "WHERE season_id = %s AND scoring_context_id = %s",
            (season_id, scoring_context_id),
        )
        rows = cur.fetchall()
        cur.close()
    return {
        frozenset(int(x) for x in r['tied_player_ids'].split(',')):
            [int(x) for x in r['resolved_order'].split(',')]
        for r in rows
    }


def save_manual_tiebreak(
    season_id: int, scoring_context_id: int, group: set[int],
    resolved_order: list[int], decided_by: int, notes: Optional[str] = None,
) -> None:
    tied_csv = ",".join(str(x) for x in sorted(group))
    order_csv = ",".join(str(x) for x in resolved_order)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO manual_tiebreaks
                (season_id, scoring_context_id, tied_player_ids, resolved_order, decided_by, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE resolved_order = VALUES(resolved_order),
                decided_by = VALUES(decided_by), notes = VALUES(notes), decided_at = NOW()
            """,
            (season_id, scoring_context_id, tied_csv, order_csv, decided_by, notes),
        )
        conn.commit()
        cur.close()


# ------------------------------------------------------------------
# Pure computation
# ------------------------------------------------------------------

def win_pct(wins: int, losses: int, ties: int) -> Fraction:
    """1 per win, 0.5 per tie, as an exact Fraction (avoids float
    equality pitfalls when comparing tied players)."""
    games = wins + losses + ties
    if games == 0:
        return Fraction(0)
    return Fraction(2 * wins + ties, 2 * games)


def record_and_margin(
    records: list[MatchupRecord], opponent_filter: Optional[set[int]] = None,
) -> tuple[int, int, int, int]:
    """(wins, losses, ties, net_margin), optionally scoped to only
    opponents in opponent_filter (criteria 1/2 scope to the tied group)."""
    rel = [r for r in records if opponent_filter is None or r.opponent_id in opponent_filter]
    wins = sum(1 for r in rel if r.result == 'W')
    losses = sum(1 for r in rel if r.result == 'L')
    ties = sum(1 for r in rel if r.result == 'T')
    margin = sum(r.margin for r in rel)
    return wins, losses, ties, margin


def _classes_desc(group: set[int], metric: dict) -> list[set[int]]:
    """Partition group by identical metric value; return classes sorted
    best -> worst (descending metric)."""
    by_value = defaultdict(set)
    for pid in group:
        by_value[metric[pid]].add(pid)
    return [by_value[v] for v in sorted(by_value, reverse=True)]


def resolve_tiebreak_group(
    group: set[int],
    all_records: dict[int, list[MatchupRecord]],
    manual_tiebreaks: dict[frozenset, list[int]],
) -> list[int]:
    """
    Returns player_ids from `group` in resolved best-to-worst order.
    Applies criteria 1 -> 2 -> 3 to the whole group at once; if a
    criterion splits the group into 2+ classes, recurses independently
    into each class (generalizing "outright winner + tied rest" and
    "tied top + clear bottom" to any number of classes of any size).
    Falls through to a manual_tiebreaks lookup, then raises
    UnresolvedTieError if still unresolved.
    """
    if len(group) == 1:
        return list(group)

    for criterion in (1, 2, 3):
        if criterion == 1:      # record within the tied group
            metric = {
                pid: win_pct(*record_and_margin(all_records[pid], group - {pid})[:3])
                for pid in group
            }
        elif criterion == 2:    # net margin within the tied group
            metric = {
                pid: record_and_margin(all_records[pid], group - {pid})[3]
                for pid in group
            }
        else:                   # criterion 3: full-season net margin, unscoped
            metric = {pid: record_and_margin(all_records[pid], None)[3] for pid in group}

        classes = _classes_desc(group, metric)
        if len(classes) > 1:
            resolved: list[int] = []
            for cls in classes:
                resolved.extend(resolve_tiebreak_group(cls, all_records, manual_tiebreaks))
            return resolved
        # single class -> this criterion didn't separate anyone; fall through

    # Criterion 4: manual commissioner override
    key = frozenset(group)
    if key in manual_tiebreaks:
        order = manual_tiebreaks[key]
        if set(order) != group:
            raise ValueError(
                f"stored manual tiebreak for {sorted(key)} doesn't match current group {sorted(group)}"
            )
        return order

    raise UnresolvedTieError(group)


def compute_seeding(
    all_records: dict[int, list[MatchupRecord]],
    manual_tiebreaks: dict[frozenset, list[int]],
) -> list[dict]:
    """
    all_records: full PVP-regular-season records, {player_id:
    [MatchupRecord, ...]}, for every player who played at least one game
    (see get_pvp_regular_season_records()).

    Returns [{seed, player_id, wins, losses, ties, win_pct, net_margin}, ...]
    in seed order (1 = best). Raises UnresolvedTieError if a group can't
    be fully resolved -- catch it, prompt for a manual tiebreak (see
    scripts/break_tie.py), save it, and retry.
    """
    players = set(all_records.keys())
    overall = {pid: record_and_margin(all_records[pid], None) for pid in players}
    overall_pct = {pid: win_pct(*overall[pid][:3]) for pid in players}

    seed_order: list[int] = []
    for cls in _classes_desc(players, overall_pct):
        seed_order.extend(resolve_tiebreak_group(cls, all_records, manual_tiebreaks))

    return [
        {
            'seed': i + 1, 'player_id': pid,
            'wins': overall[pid][0], 'losses': overall[pid][1], 'ties': overall[pid][2],
            'win_pct': float(overall_pct[pid]), 'net_margin': overall[pid][3],
        }
        for i, pid in enumerate(seed_order)
    ]
