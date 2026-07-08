"""
Drop/add request validation for the CFB Game.

Pure Python — no database access, no Streamlit, no I/O of any kind. Mirrors
lib.game_rules's separation of concerns: this module only judges whether a
requested drop/add is structurally legal (roster ownership, no double-owning
a team). It does not decide business questions that aren't settled yet
(weekly request limits, lock windows) — see TODO.md.
"""

from typing import Optional

from lib.game_rules import ValidationResult


def validate_dropadd_request(
    dropped_team_id: Optional[int],
    added_team_id: Optional[int],
    roster_team_ids: set[int],
    owned_team_ids: set[int],
) -> ValidationResult:
    """
    Validate one drop/add request before it's written to dropadd_requests.

    Args:
        dropped_team_id: team the player wants to drop, or None for an
            add-only request.
        added_team_id: team the player wants to add, or None for a
            drop-only request.
        roster_team_ids: team_ids currently active on the requesting
            player's own roster.
        owned_team_ids: team_ids currently active on ANY player's roster
            this season (including the requester's own) — an added team
            must not already be in this set.
    """
    result = ValidationResult()

    if dropped_team_id is None and added_team_id is None:
        result.add_error("Specify a team to drop, add, or both.")
        return result

    if dropped_team_id is not None and dropped_team_id not in roster_team_ids:
        result.add_error("The team to drop is not on your active roster.")

    if added_team_id is not None:
        if added_team_id in roster_team_ids:
            result.add_error("The team to add is already on your roster.")
        elif added_team_id in owned_team_ids:
            result.add_error(
                "The team to add is already owned by another player this season."
            )

    if (
        dropped_team_id is not None
        and added_team_id is not None
        and dropped_team_id == added_team_id
    ):
        result.add_error("Cannot drop and add the same team.")

    return result
