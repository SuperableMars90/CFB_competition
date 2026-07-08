"""
Data shapes for game context within a given week.

This module is pure Python — no database access, no Streamlit dependencies.
It defines what data the lineup form consumes per team, independent of
where that data comes from.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GameLocation(str, Enum):
    """Where a team is playing this week relative to their opponent."""
    HOME = "home"
    AWAY = "away"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class TeamGameContext:
    """
    Everything the lineup form needs about one team for a given week.

    For teams on bye:
        - opponent_team_id, opponent_name, and location are all None
        - is_on_bye returns True
        - display_label returns "TeamName - BYE"
    """
    # Identity (always present)
    team_id: int
    name: str
    conference_id: int
    conference_abbreviation: str
    tier: str   # "P4", "G6", or "Independent" — matches conferences.tier enum

    # This week's game (None for bye weeks)
    opponent_team_id: Optional[int] = None
    opponent_name: Optional[str] = None
    location: Optional[GameLocation] = None
    game_id: Optional[int] = None

    # Reserved for future extension — populated by the data layer when ready.
    # Adding fields with defaults here is non-breaking; consumers that don't
    # read them simply ignore them.
    season_net_score_to_date: Optional[int] = None
    last_n_games_net_scores: Optional[tuple[int, ...]] = None
    opponent_record: Optional[tuple[int, int]] = None

    @property
    def is_on_bye(self) -> bool:
        """True if this team has no game this week."""
        return self.opponent_team_id is None

    @property
    def display_label(self) -> str:
        """
        Human-readable label for the lineup form.

        Examples:
            "Alabama - BYE"
            "Alabama vs Tennessee"
            "Alabama @ Tennessee"
            "Alabama vs Tennessee (N)"
        """
        if self.is_on_bye:
            return f"{self.name} - BYE"

        if self.location == GameLocation.HOME:
            return f"{self.name} vs {self.opponent_name}"
        elif self.location == GameLocation.AWAY:
            return f"{self.name} @ {self.opponent_name}"
        else:  # NEUTRAL
            return f"{self.name} vs {self.opponent_name} (N)"