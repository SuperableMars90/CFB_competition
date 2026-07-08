"""
lib/bracket_engine.py
----------------------
Generic, format-agnostic single-elimination bracket engine. No DB
access, no Streamlit, no knowledge of players/pods/standings -- only
games, participants, and placements. lib/playoffs.py plugs real seed
assignments and results into this.

A bracket is just a list of GameSpec: each side of a game is either a
fixed seed, or the winner/loser of an earlier game (by game_number).
Some games directly decide a final placement for their winner and/or
loser (winner_place/loser_place); others just feed into a later game.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Participant:
    """
    One side of a game: either a fixed bracket seed, or the winner/loser
    of an earlier game. Exactly one of (seed) or (from_game +
    from_result) is set -- use the seed()/winner_of()/loser_of() helpers
    rather than constructing this directly.
    """
    seed: Optional[int] = None
    from_game: Optional[int] = None
    from_result: Optional[str] = None   # 'winner' | 'loser'

    def __post_init__(self):
        has_seed = self.seed is not None
        has_source = self.from_game is not None and self.from_result is not None
        if has_seed == has_source:
            raise ValueError("Participant must set exactly one of seed, or from_game+from_result")
        if self.from_result is not None and self.from_result not in ('winner', 'loser'):
            raise ValueError(f"from_result must be 'winner' or 'loser', got {self.from_result!r}")


def seed(n: int) -> Participant:
    return Participant(seed=n)


def winner_of(game_number: int) -> Participant:
    return Participant(from_game=game_number, from_result='winner')


def loser_of(game_number: int) -> Participant:
    return Participant(from_game=game_number, from_result='loser')


@dataclass(frozen=True)
class GameSpec:
    game_number: int
    round: int                          # label only -- engine logic never depends on it
    side_a: Participant
    side_b: Participant
    winner_place: Optional[int] = None  # final placement the winner earns, if this game decides one
    loser_place: Optional[int] = None   # same, for the loser


@dataclass(frozen=True)
class GameResult:
    winner_id: int
    loser_id: int
    # Audit/display only -- 'total_points' | 'pick_record' | 'manual' | 'result'.
    # The engine itself never reads this field.
    decided_by: str = 'result'


def resolve_participant(
    participant: Participant,
    seed_to_player: dict[int, int],
    results: dict[int, GameResult],
) -> Optional[int]:
    """player_id if resolvable now, else None (its source game isn't decided yet)."""
    if participant.seed is not None:
        return seed_to_player[participant.seed]

    result = results.get(participant.from_game)
    if result is None:
        return None
    return result.winner_id if participant.from_result == 'winner' else result.loser_id


def playable_games(
    spec: list[GameSpec],
    seed_to_player: dict[int, int],
    results: dict[int, GameResult],
) -> list[GameSpec]:
    """
    Games not yet in `results` whose both sides already resolve to a
    player_id. Purely dependency-driven, not round-ordered -- a game can
    become playable before another game nominally in the same round.
    """
    playable = []
    for game in spec:
        if game.game_number in results:
            continue
        a = resolve_participant(game.side_a, seed_to_player, results)
        b = resolve_participant(game.side_b, seed_to_player, results)
        if a is not None and b is not None:
            playable.append(game)
    return playable


def compute_placements(
    spec: list[GameSpec],
    seed_to_player: dict[int, int],
    results: dict[int, GameResult],
) -> dict[int, int]:
    """
    place -> player_id, for every placement whose deciding game is
    already in `results`. Partial dict if the bracket isn't fully
    played yet.
    """
    placements: dict[int, int] = {}
    for game in spec:
        result = results.get(game.game_number)
        if result is None:
            continue
        if game.winner_place is not None:
            placements[game.winner_place] = result.winner_id
        if game.loser_place is not None:
            placements[game.loser_place] = result.loser_id
    return placements


def expected_placements(spec: list[GameSpec]) -> set[int]:
    """
    Every placement number this spec can ultimately decide (union of all
    winner_place/loser_place across all games). Doesn't include a
    placement a format assigns outside of any game (e.g. the 4-team
    format's non-qualifying seed) -- that's a lib/playoffs.py concern.
    """
    places = set()
    for game in spec:
        if game.winner_place is not None:
            places.add(game.winner_place)
        if game.loser_place is not None:
            places.add(game.loser_place)
    return places
