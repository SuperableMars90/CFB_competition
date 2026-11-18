"""
lib/scenarios.py
----------------
"What if" PVP standings for playoff-odds projections: split a season's
pairings into decided records and still-open games, fill the open games
with hypothetical winners, rank the result, and map it to playoff seeds.

Pure Python -- no DB access, no Streamlit. Ranking and seeding reuse
lib.seeding's tiebreakers and lib.playoffs' seed assignment, so a
projection can never disagree with how the league actually seeds. The
one difference is mid-season ties: where compute_seeding() raises
UnresolvedTieError, rank_standings() leaves the group tied (same rank,
tied=True) and lets the caller decide what to do with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.playoffs import (
    _assign_seeds_two_pod, assign_seeds_4_team, bracket_format_for_league_shape,
)
from lib.seeding import MatchupRecord, _classes_desc, record_and_margin, win_pct


@dataclass(frozen=True)
class OpenMatchup:
    pairing_id: int
    week: int
    player_a_id: int
    player_b_id: int


def split_pairings(
    rows: list[dict],
) -> tuple[dict[int, list[MatchupRecord]], list[OpenMatchup]]:
    """
    rows: matchup_pairings-shaped dicts {id, week, player_a_id, player_b_id,
    winner_id, is_tie, a_points, b_points}. A row is decided when it has a
    winner_id or is_tie; everything else is returned as an OpenMatchup.
    Every player in rows gets a records entry, even with no decided games.
    """
    records: dict[int, list[MatchupRecord]] = {}
    remaining: list[OpenMatchup] = []
    for r in rows:
        a, b = r['player_a_id'], r['player_b_id']
        records.setdefault(a, [])
        records.setdefault(b, [])
        if r['winner_id'] is None and not r['is_tie']:
            remaining.append(OpenMatchup(r['id'], r['week'], a, b))
            continue
        margin = r['a_points'] - r['b_points']
        if r['is_tie']:
            ra, rb = 'T', 'T'
        elif r['winner_id'] == a:
            ra, rb = 'W', 'L'
        else:
            ra, rb = 'L', 'W'
        records[a].append(MatchupRecord(r['week'], b, ra, margin))
        records[b].append(MatchupRecord(r['week'], a, rb, -margin))
    return records, remaining


def apply_picks(
    records: dict[int, list[MatchupRecord]],
    remaining: list[OpenMatchup],
    picks: dict[int, int],
) -> dict[int, list[MatchupRecord]]:
    """
    New records with each picked open game added: picks is {pairing_id:
    winner player_id}. Hypothetical games count as a 0-point margin, so
    they move records but not net-margin tiebreakers. Open games missing
    from picks stay unplayed. `records` itself is not modified.
    """
    out = {pid: list(recs) for pid, recs in records.items()}
    for m in remaining:
        winner = picks.get(m.pairing_id)
        if winner is None:
            continue
        loser = m.player_b_id if winner == m.player_a_id else m.player_a_id
        out.setdefault(winner, []).append(MatchupRecord(m.week, loser, 'W', 0))
        out.setdefault(loser, []).append(MatchupRecord(m.week, winner, 'L', 0))
    return out


def _resolve_tiers(
    group: set[int],
    all_records: dict[int, list[MatchupRecord]],
    manual_tiebreaks: dict[frozenset, list[int]],
) -> list[set[int]]:
    """
    lib.seeding.resolve_tiebreak_group(), except a group that criteria 1-3
    and the manual overrides can't separate comes back as one tier instead
    of raising. Returns tiers best -> worst.
    """
    if len(group) == 1:
        return [group]

    for criterion in (1, 2, 3):
        if criterion == 1:      # record within the tied group
            metric = {pid: win_pct(*record_and_margin(all_records[pid], group - {pid})[:3])
                      for pid in group}
        elif criterion == 2:    # net margin within the tied group
            metric = {pid: record_and_margin(all_records[pid], group - {pid})[3] for pid in group}
        else:                   # criterion 3: full-season net margin, unscoped
            metric = {pid: record_and_margin(all_records[pid], None)[3] for pid in group}

        classes = _classes_desc(group, metric)
        if len(classes) > 1:
            tiers: list[set[int]] = []
            for cls in classes:
                tiers.extend(_resolve_tiers(cls, all_records, manual_tiebreaks))
            return tiers

    order = manual_tiebreaks.get(frozenset(group))
    if order is not None and set(order) == group:
        return [{pid} for pid in order]
    return [group]


def rank_standings(
    all_records: dict[int, list[MatchupRecord]],
    manual_tiebreaks: dict[frozenset, list[int]],
) -> list[dict]:
    """
    [{rank, tied, player_id, wins, losses, ties, win_pct, net_margin}, ...]
    best first. Ranked by win % and then lib.seeding's tiebreakers. Players
    still tied after all of them share a rank (competition style: 1, 2, 2,
    4) with tied=True; within a tie they're listed by player_id.
    """
    overall = {pid: record_and_margin(recs, None) for pid, recs in all_records.items()}
    pct = {pid: win_pct(*overall[pid][:3]) for pid in overall}

    out: list[dict] = []
    for cls in _classes_desc(set(overall), pct):
        for tier in _resolve_tiers(cls, all_records, manual_tiebreaks):
            rank = len(out) + 1
            for pid in sorted(tier):
                w, l, t, margin = overall[pid]
                out.append({
                    'rank': rank, 'tied': len(tier) > 1, 'player_id': pid,
                    'wins': w, 'losses': l, 'ties': t,
                    'win_pct': float(pct[pid]), 'net_margin': margin,
                })
    return out


def projected_playoff_seeds(
    standings: list[dict],
    pod_of_player: dict[int, int],
) -> dict[int, int]:
    """
    {player_id: bracket seed} if the regular season ended with these
    standings. standings must be fully ordered best first (rank_standings()
    rows with ties already broken by the caller); only player_id order is
    used. Seeds come from lib.playoffs, picked by league shape.
    """
    seeding = [{'seed': i + 1, 'player_id': r['player_id']} for i, r in enumerate(standings)]
    n_pods = len({pod_of_player[r['player_id']] for r in standings})
    fmt = bracket_format_for_league_shape(n_pods, len(standings))
    if fmt == 'four_team':
        seed_to_player = assign_seeds_4_team(seeding)
    else:
        seed_to_player = _assign_seeds_two_pod(seeding, pod_of_player)
    return {pid: s for s, pid in seed_to_player.items()}
