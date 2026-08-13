"""
lib/playoffs.py
----------------
Playoff formats built on top of lib.bracket_engine: the declarative
4-team, 6-team, and 8-team bracket specs (4-team/8-team transcribed from
8_team_playoff_matchup_table.tsv / 8_team_playoff_placement_table.tsv;
6-team from playoff_games.csv / placement.csv, added 2026-08 for the
2-pods-of-3 format -- see transition_plan.md), seed assignment from
regular-season standings, and the playoff-game tiebreak rule.

Format selection (bracket_format_for_league_shape) is keyed on
(pod count, total player count), not hardcoded to one pod arrangement --
a season can go back to 2 pods of 4 (eight_team) after a season of 2
pods of 3 (six_team) with no code change, only a different season-init
config. Adding support for a genuinely new pod/player-count combination
still requires a new confirmed bracket design (see the CSVs above),
not a guessed generalization.

Week-number agnostic by design -- real calendar week numbers for each
round are a separate, later concern (season length varies year to
year), not handled here.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional

from lib.bracket_engine import (
    GameSpec, GameResult, seed, winner_of, loser_of, compute_placements, expected_placements,
)
from lib.seeding import MatchupRecord

# ------------------------------------------------------------------
# Bracket specs
# ------------------------------------------------------------------

FOUR_TEAM_BRACKET: list[GameSpec] = [
    GameSpec(game_number=1, round=1, side_a=seed(2), side_b=seed(3), loser_place=3),
    GameSpec(game_number=2, round=2, side_a=seed(1), side_b=winner_of(1),
             winner_place=1, loser_place=2),
]
FOUR_TEAM_NON_QUALIFYING_SEED = 4  # placed 4th automatically, no game played

EIGHT_TEAM_BRACKET: list[GameSpec] = [
    GameSpec(1, 1, seed(4), seed(5)),
    GameSpec(2, 1, seed(3), seed(6)),
    GameSpec(3, 1, seed(7), seed(8), winner_place=7, loser_place=8),
    GameSpec(4, 2, seed(1), winner_of(1)),
    GameSpec(5, 2, seed(2), winner_of(2)),
    GameSpec(6, 2, loser_of(1), loser_of(2), winner_place=5, loser_place=6),
    GameSpec(7, 3, winner_of(4), winner_of(5), winner_place=1, loser_place=2),
    GameSpec(8, 3, loser_of(4), loser_of(5), winner_place=3, loser_place=4),
]

# Same shape as EIGHT_TEAM_BRACKET with its "7-seed vs 8-seed" placement
# game (and seeds 7/8 themselves) removed -- every other game, round, and
# placement maps 1:1. Game numbers here match playoff_games.csv's POG#
# directly for traceability back to the confirmed design.
SIX_TEAM_BRACKET: list[GameSpec] = [
    GameSpec(1, 1, seed(3), seed(6)),                                          # POG1
    GameSpec(2, 1, seed(4), seed(5)),                                          # POG2
    GameSpec(3, 2, loser_of(1), loser_of(2), winner_place=5, loser_place=6),   # POG3
    GameSpec(4, 2, seed(1), winner_of(2)),                                     # POG4
    GameSpec(5, 2, seed(2), winner_of(1)),                                     # POG5
    GameSpec(6, 3, loser_of(4), loser_of(5), winner_place=3, loser_place=4),   # POG6
    GameSpec(7, 3, winner_of(4), winner_of(5), winner_place=1, loser_place=2), # POG7
]


# ------------------------------------------------------------------
# Seed assignment
# ------------------------------------------------------------------

def assign_seeds_4_team(seeding: list[dict]) -> dict[int, int]:
    """
    seeding: lib.seeding.compute_seeding() output, must be exactly 4
    players. Bracket seed == compute_seeding()'s own 'seed' field --
    no pod logic, there's only one pod.

    Returns {bracket_seed: player_id}.
    """
    if len(seeding) != 4:
        raise ValueError(f"4-team format requires exactly 4 players, got {len(seeding)}")
    return {row['seed']: row['player_id'] for row in seeding}


def _assign_seeds_two_pod(seeding: list[dict], pod_of_player: dict[int, int]) -> dict[int, int]:
    """
    Shared algorithm behind assign_seeds_6_team/assign_seeds_8_team --
    size-agnostic, works for any total player count as long as exactly 2
    pods are present. seeding: lib.seeding.compute_seeding() output
    across both pods combined (overall-rank order). pod_of_player:
    {player_id: pod_id}.

    Seed 1 = seeding[0] (overall #1), whichever pod.
    Seed 2 = first remaining player (in overall-rank order) whose pod
      differs from seed 1's -- NOT necessarily overall #2.
    Seeds 3-N = everyone else remaining, in original overall-rank order.

    This is algebraically equivalent to "each pod's own winner gets
    seed 1 or 2 (better overall pod first), everyone else keeps their
    relative overall order for seeds 3-N" -- a pod's own top finisher is
    always the first member of that pod encountered walking the full
    overall order, so "first remaining player from the other pod" and
    "that pod's own winner" are always the same player.

    Returns {bracket_seed (1-N): player_id}.
    """
    remaining = list(seeding)
    seed1_row = remaining.pop(0)
    seed1_pod = pod_of_player[seed1_row['player_id']]

    seed2_idx = next(
        (i for i, row in enumerate(remaining)
         if pod_of_player[row['player_id']] != seed1_pod),
        None,
    )
    if seed2_idx is None:
        raise ValueError("no remaining player from a different pod than seed 1 -- not really 2 pods?")
    seed2_row = remaining.pop(seed2_idx)

    ordered = [seed1_row, seed2_row] + remaining
    return {i + 1: row['player_id'] for i, row in enumerate(ordered)}


def assign_seeds_6_team(seeding: list[dict], pod_of_player: dict[int, int]) -> dict[int, int]:
    """seeding must be exactly 6 players (2 pods of 3) -- see _assign_seeds_two_pod."""
    if len(seeding) != 6:
        raise ValueError(f"6-team format requires exactly 6 players, got {len(seeding)}")
    return _assign_seeds_two_pod(seeding, pod_of_player)


def assign_seeds_8_team(seeding: list[dict], pod_of_player: dict[int, int]) -> dict[int, int]:
    """seeding must be exactly 8 players (2 pods of 4) -- see _assign_seeds_two_pod."""
    if len(seeding) != 8:
        raise ValueError(f"8-team format requires exactly 8 players, got {len(seeding)}")
    return _assign_seeds_two_pod(seeding, pod_of_player)


def compute_4_team_placements(
    seed_to_player: dict[int, int], results: dict[int, GameResult],
) -> dict[int, int]:
    """
    compute_placements(FOUR_TEAM_BRACKET, ...) (places 1-3) plus the
    seed-4 non-qualifier's automatic 4th place.
    """
    placements = compute_placements(FOUR_TEAM_BRACKET, seed_to_player, results)
    placements[4] = seed_to_player[FOUR_TEAM_NON_QUALIFYING_SEED]
    return placements


# ------------------------------------------------------------------
# Format dispatch -- shared by scripts/generate_pvp_schedule.py,
# scripts/seed_playoffs.py, and scripts/advance_playoffs.py so the
# "which (pod count, player count) maps to which bracket format" and
# "is the bracket fully decided yet" logic isn't duplicated three times.
# ------------------------------------------------------------------

BRACKET_SPECS: dict[str, list[GameSpec]] = {
    'four_team': FOUR_TEAM_BRACKET,
    'six_team': SIX_TEAM_BRACKET,
    'eight_team': EIGHT_TEAM_BRACKET,
}
PLAYOFF_ROUND_COUNT: dict[str, int] = {'four_team': 2, 'six_team': 3, 'eight_team': 3}
EXHIBITION_GAME_COUNT: dict[str, int] = {'four_team': 1, 'six_team': 1, 'eight_team': 2}

# (n_pods, n_players) -> bracket format, for every combination with a
# confirmed design. A season can move between any of these (e.g. back to
# 2 pods of 4 after a season of 2 pods of 3) purely via season-init
# config -- no code change. A genuinely new combination needs a new
# confirmed bracket design added here, not a guessed generalization.
_LEAGUE_SHAPE_TO_FORMAT: dict[tuple[int, int], str] = {
    (1, 4): 'four_team',
    (2, 6): 'six_team',
    (2, 8): 'eight_team',
}


def bracket_format_for_league_shape(n_pods: int, n_players: int) -> str:
    """Bracket format for a given (pod count, total player count); raises
    ValueError if that combination has no confirmed design yet."""
    try:
        return _LEAGUE_SHAPE_TO_FORMAT[(n_pods, n_players)]
    except KeyError:
        raise ValueError(
            f"no confirmed playoff bracket format for {n_pods} pod(s) / {n_players} total players"
        )


def compute_final_placements(
    bracket_format: str, seed_to_player: dict[int, int], results: dict[int, GameResult],
) -> Optional[dict[int, int]]:
    """
    Full 1-N placement dict once every placement is decided, else None.
    Unlike compute_placements()/compute_4_team_placements(), which return
    whatever's decided so far, this is a clean "is the bracket over yet"
    check -- useful since eight_team's places 5-8 (six_team's 5-6) are
    known well before the championship finishes.
    """
    if bracket_format == 'four_team':
        placements = compute_4_team_placements(seed_to_player, results)
        expected = expected_placements(FOUR_TEAM_BRACKET) | {4}
    else:
        spec = BRACKET_SPECS[bracket_format]
        placements = compute_placements(spec, seed_to_player, results)
        expected = expected_placements(spec)
    return placements if set(placements) == expected else None


# ------------------------------------------------------------------
# Playoff-game tiebreak
# ------------------------------------------------------------------

def pick_record_score(scored_picks: list[dict]) -> Fraction:
    """
    1 pt per pick with margin > 0 (win); 0.5 per pass (is_pass) OR a
    non-pass pick with margin == 0 (an actual tied underlying CFB game --
    rare; scored the same as a pass); 0 per margin < 0 (loss).

    Raises ValueError on a non-pass pick with margin still None (week
    not fully final yet) -- only meant to be called once a playoff
    week has fully concluded.
    """
    total = Fraction(0)
    for p in scored_picks:
        if p['is_pass']:
            total += Fraction(1, 2)
        elif p['margin'] is None:
            raise ValueError("pick_record_score requires a fully final week (a pick has margin=None)")
        elif p['margin'] > 0:
            total += 1
        elif p['margin'] == 0:
            total += Fraction(1, 2)
        # margin < 0 -> +0, no-op
    return total


class UnresolvedPlayoffTieError(Exception):
    """
    Raised when total points AND pick-record both tie for a single
    elimination game. Unlike lib.seeding.UnresolvedTieError (which
    carries a whole tied *group* for a ranking problem), this always
    carries exactly the two players in this one game -- a 1v1
    winner-take-all problem has no larger tied group. Mirrors
    UnresolvedTieError's role: a future interactive commissioner script
    catches this and prompts for a manual winner.
    """
    def __init__(self, player_a_id: int, player_b_id: int):
        self.player_a_id = player_a_id
        self.player_b_id = player_b_id
        super().__init__(f"Unresolved playoff-game tie between {player_a_id} and {player_b_id}")


def resolve_playoff_game(
    player_totals: dict[int, dict], player_a_id: int, player_b_id: int,
) -> GameResult:
    """
    Decide a single playoff game's winner.
    1. Higher `total` wins (decided_by='total_points').
    2. Tied totals -> higher pick_record_score() wins (decided_by='pick_record').
    3. Still tied -> raises UnresolvedPlayoffTieError.
    """
    a, b = player_totals[player_a_id], player_totals[player_b_id]
    if a['total'] != b['total']:
        if a['total'] > b['total']:
            return GameResult(player_a_id, player_b_id, decided_by='total_points')
        return GameResult(player_b_id, player_a_id, decided_by='total_points')

    a_rec = pick_record_score(a['slots'])
    b_rec = pick_record_score(b['slots'])
    if a_rec != b_rec:
        if a_rec > b_rec:
            return GameResult(player_a_id, player_b_id, decided_by='pick_record')
        return GameResult(player_b_id, player_a_id, decided_by='pick_record')

    raise UnresolvedPlayoffTieError(player_a_id, player_b_id)


# ------------------------------------------------------------------
# Exhibition matchups (final playoff week, already-eliminated players)
# ------------------------------------------------------------------

def _meeting_count(all_records: dict[int, list[MatchupRecord]], a: int, b: int) -> int:
    """How many times a and b have played each other this season."""
    return sum(1 for r in all_records[a] if r.opponent_id == b)


def _weeks_since_last_meeting(
    all_records: dict[int, list[MatchupRecord]], a: int, b: int, current_week: int,
) -> int:
    """current_week minus the most recent week a and b played each other."""
    weeks = [r.week for r in all_records[a] if r.opponent_id == b]
    if not weeks:
        raise ValueError(f"players {a} and {b} have never played each other")
    return current_week - max(weeks)


def _head_to_head_win_diff(all_records: dict[int, list[MatchupRecord]], a: int, b: int) -> int:
    """abs(a's wins over b - b's wins over a) -- 0 if their record is even."""
    a_wins = sum(1 for r in all_records[a] if r.opponent_id == b and r.result == 'W')
    b_wins = sum(1 for r in all_records[b] if r.opponent_id == a and r.result == 'W')
    return abs(a_wins - b_wins)


def find_exhibition_pairing(
    idle_players: list[int],
    all_records: dict[int, list[MatchupRecord]],
    pod_of_player: dict[int, int],
    current_week: int,
) -> list[tuple[int, int]]:
    """
    Pairs up players who are already fully eliminated with no game
    scheduled for the final playoff week, so nobody sits idle.

    Exactly 2 idle players (4-team and 6-team formats -- six_team's
    already-decided 5th/6th finishers): returns the single trivial pair --
    nothing to optimize.

    Exactly 4 idle players (8-team format): evaluates all 3 ways to
    split them into 2 pairs and picks the one that, in order --
      1. minimizes total combined head-to-head meetings this season
      2. (tiebreak) maximizes combined weeks-since-last-meeting
         (prefers the staler matchup)
      3. (tiebreak) prefers more cross-pod pairs
      4. (tiebreak) minimizes summed |wins_a - wins_b| across both pairs
         (prefers a more evenly-split head-to-head history)
      5. (tiebreak) arbitrary but deterministic: lowest sorted-player-id
         ordering

    Any other count of idle players raises ValueError -- only formats
    with a confirmed 2- or 4-idle-player final week are supported.
    """
    if len(idle_players) == 2:
        a, b = idle_players
        return [(a, b)]

    if len(idle_players) != 4:
        raise ValueError(
            f"find_exhibition_pairing only supports 2 or 4 idle players, got {len(idle_players)}"
        )

    w, x, y, z = idle_players
    candidates = [
        [(w, x), (y, z)],
        [(w, y), (x, z)],
        [(w, z), (x, y)],
    ]

    def score(candidate: list[tuple[int, int]]):
        total_meetings = sum(_meeting_count(all_records, a, b) for a, b in candidate)
        weeks_since = sum(
            _weeks_since_last_meeting(all_records, a, b, current_week) for a, b in candidate
        )
        cross_pod_count = sum(1 for a, b in candidate if pod_of_player[a] != pod_of_player[b])
        variance = sum(_head_to_head_win_diff(all_records, a, b) for a, b in candidate)
        canonical = tuple(sorted(tuple(sorted(pair)) for pair in candidate))
        return (total_meetings, -weeks_since, -cross_pod_count, variance, canonical)

    return min(candidates, key=score)
