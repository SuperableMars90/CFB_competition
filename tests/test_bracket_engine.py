"""
Unit tests for lib.bracket_engine -- pure Python, no DB, no Streamlit.

Uses lib.playoffs's real EIGHT_TEAM_BRACKET/FOUR_TEAM_BRACKET specs as
fixtures throughout, since those are exactly what this engine needs to
handle correctly.
"""

import pytest

from lib.bracket_engine import (
    Participant,
    GameSpec,
    GameResult,
    seed,
    winner_of,
    loser_of,
    resolve_participant,
    playable_games,
    compute_placements,
    expected_placements,
)
from lib.playoffs import EIGHT_TEAM_BRACKET, FOUR_TEAM_BRACKET

# Seeds 1-8 map directly to player_ids 1-8 for readability in these tests.
SEED_TO_PLAYER = {i: i for i in range(1, 9)}


class TestParticipant:
    def test_seed_only_is_valid(self):
        p = seed(3)
        assert p.seed == 3
        assert p.from_game is None

    def test_winner_of_and_loser_of(self):
        w = winner_of(2)
        assert w.from_game == 2 and w.from_result == 'winner'
        l = loser_of(2)
        assert l.from_game == 2 and l.from_result == 'loser'

    def test_both_seed_and_source_raises(self):
        with pytest.raises(ValueError):
            Participant(seed=1, from_game=2, from_result='winner')

    def test_neither_seed_nor_source_raises(self):
        with pytest.raises(ValueError):
            Participant()

    def test_invalid_from_result_raises(self):
        with pytest.raises(ValueError):
            Participant(from_game=1, from_result='tie')


class TestResolveParticipant:
    def test_seed_based_resolves_immediately(self):
        assert resolve_participant(seed(4), SEED_TO_PLAYER, {}) == 4

    def test_game_based_unresolved_before_source_decided(self):
        assert resolve_participant(winner_of(1), SEED_TO_PLAYER, {}) is None

    def test_game_based_resolves_after_source_decided(self):
        results = {1: GameResult(winner_id=4, loser_id=5)}
        assert resolve_participant(winner_of(1), SEED_TO_PLAYER, results) == 4
        assert resolve_participant(loser_of(1), SEED_TO_PLAYER, results) == 5


class TestPlayableGames:
    def test_only_round1_games_playable_initially(self):
        playable = playable_games(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, {})
        assert {g.game_number for g in playable} == {1, 2, 3}

    def test_game6_playable_independent_of_game3(self):
        # G1 and G2 decided, G3 (same nominal round) still open --
        # G6 (loser G1 vs loser G2) should surface anyway, since the
        # engine is dependency-driven, not round-ordered.
        results = {
            1: GameResult(winner_id=4, loser_id=5),
            2: GameResult(winner_id=3, loser_id=6),
        }
        playable = playable_games(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, results)
        numbers = {g.game_number for g in playable}
        assert numbers == {3, 4, 5, 6}
        assert 7 not in numbers and 8 not in numbers

    def test_decided_game_never_reappears(self):
        results = {1: GameResult(winner_id=4, loser_id=5)}
        playable = playable_games(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert 1 not in {g.game_number for g in playable}


class TestExpectedPlacements:
    def test_eight_team(self):
        assert expected_placements(EIGHT_TEAM_BRACKET) == set(range(1, 9))

    def test_four_team(self):
        assert expected_placements(FOUR_TEAM_BRACKET) == {1, 2, 3}


class TestComputePlacementsEightTeam:
    def test_no_upsets_collapses_to_seed_order(self):
        results = {
            1: GameResult(4, 5), 2: GameResult(3, 6), 3: GameResult(7, 8),
            4: GameResult(1, 4), 5: GameResult(2, 3), 6: GameResult(5, 6),
            7: GameResult(1, 2), 8: GameResult(3, 4),
        }
        placements = compute_placements(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert placements == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8}

    def test_with_deliberate_upsets(self):
        # G1: seed 5 upsets seed 4. G2/G3: no upsets.
        # G4: seed1 beats winner(G1)=seed5. G5: seed2 beats winner(G2)=seed3.
        # G6: loser(G1)=seed4 beats loser(G2)=seed6 -> places {5:4, 6:6}.
        # G7: seed1 beats seed2 -> places {1:1, 2:2}.
        # G8: loser(G4)=seed5 vs loser(G5)=seed3 -> seed3 wins -> places {3:3, 4:5}.
        results = {
            1: GameResult(winner_id=5, loser_id=4),
            2: GameResult(winner_id=3, loser_id=6),
            3: GameResult(winner_id=7, loser_id=8),
            4: GameResult(winner_id=1, loser_id=5),
            5: GameResult(winner_id=2, loser_id=3),
            6: GameResult(winner_id=4, loser_id=6),
            7: GameResult(winner_id=1, loser_id=2),
            8: GameResult(winner_id=3, loser_id=5),
        }
        placements = compute_placements(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert placements == {1: 1, 2: 2, 3: 3, 4: 5, 5: 4, 6: 6, 7: 7, 8: 8}

    def test_partial_bracket_only_decided_placements_appear(self):
        results = {
            1: GameResult(4, 5), 2: GameResult(3, 6), 3: GameResult(7, 8),
        }
        placements = compute_placements(EIGHT_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert placements == {7: 7, 8: 8}


class TestComputePlacementsFourTeam:
    def test_full_walk_no_upsets(self):
        results = {
            1: GameResult(winner_id=2, loser_id=3),
            2: GameResult(winner_id=1, loser_id=2),
        }
        placements = compute_placements(FOUR_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert placements == {1: 1, 2: 2, 3: 3}

    def test_upset_in_semifinal(self):
        results = {
            1: GameResult(winner_id=3, loser_id=2),  # seed 3 upsets seed 2
            2: GameResult(winner_id=1, loser_id=3),
        }
        placements = compute_placements(FOUR_TEAM_BRACKET, SEED_TO_PLAYER, results)
        assert placements == {1: 1, 2: 3, 3: 2}
