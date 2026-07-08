"""
lib/draft.py
------------
Pure snake-draft position math, plus the filter/sort logic shared by
the live draft page's team-selection and roster-display sections. No
DB, no Streamlit.

Used at commissioner setup time to generate the entire draft's pick
schedule up front (see generate_full_draft_order) -- once written to
draft_order, "whose turn is it" is just "the lowest pick_number row
still having team_selected IS NULL," read directly from the DB. Nothing
here is called repeatedly during live picking except is_double_pick,
which only compares two already-known rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class DraftSlot:
    pick_number: int
    round_number: int
    pick_in_round: int
    player_id: int


def round_order(base_order: list[int], round_number: int) -> list[int]:
    """base_order for odd rounds, reversed for even rounds (1-based)."""
    if round_number % 2 == 1:
        return list(base_order)
    return list(reversed(base_order))


def slot_for_pick(base_order: list[int], pick_number: int) -> DraftSlot:
    """Pure position math: which round/pick_in_round/player a given overall pick_number maps to."""
    n = len(base_order)
    round_number = ((pick_number - 1) // n) + 1
    pick_in_round = ((pick_number - 1) % n) + 1
    player_id = round_order(base_order, round_number)[pick_in_round - 1]
    return DraftSlot(
        pick_number=pick_number, round_number=round_number,
        pick_in_round=pick_in_round, player_id=player_id,
    )


def total_picks(num_players: int, picks_per_player: int) -> int:
    return num_players * picks_per_player


def generate_full_draft_order(base_order: list[int], picks_per_player: int) -> list[DraftSlot]:
    """
    slot_for_pick() for every pick_number from 1 to total_picks(...) --
    the entire pre-populated schedule lib.db.set_draft_order() bulk-inserts
    at commissioner setup time.
    """
    total = total_picks(len(base_order), picks_per_player)
    return [slot_for_pick(base_order, p) for p in range(1, total + 1)]


def is_double_pick(first: DraftSlot, second: Optional[DraftSlot]) -> bool:
    """
    True iff `second` exists and shares `first`'s player_id -- the only
    way this can happen is a round boundary, by construction of a snake
    order. Used by the live page against the two lowest-pick_number open
    (team_selected IS NULL) rows it just read from the DB, not against
    any recomputed count.
    """
    return second is not None and second.player_id == first.player_id


def ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11/12/13 -> '11th'/'12th'/'13th', 21 -> '21st'."""
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


class SortBy(str, Enum):
    NAME = "name"
    CONFERENCE = "conference"
    TIER = "tier"


class TierFilter(str, Enum):
    ALL = "All"
    P4 = "P4"
    G6 = "G6"
    CONFERENCE = "conference"  # "specific conference" mode; actual abbreviation passed separately


def filter_and_sort_teams(
    teams: list[dict], *, tier: TierFilter = TierFilter.ALL,
    conference: Optional[str] = None, sort_by: SortBy = SortBy.NAME,
) -> list[dict]:
    """
    Pure filter/sort over team dicts ({team_id, name, conference_abbreviation, tier}) --
    shared by the pick area's available-teams pool and the rosters-so-far
    section. Raises ValueError if tier=CONFERENCE and conference is None.
    """
    if tier == TierFilter.CONFERENCE and conference is None:
        raise ValueError("conference is required when tier=TierFilter.CONFERENCE")

    filtered = teams
    if tier == TierFilter.P4:
        filtered = [t for t in filtered if t['tier'] == 'P4']
    elif tier == TierFilter.G6:
        filtered = [t for t in filtered if t['tier'] == 'G6']
    elif tier == TierFilter.CONFERENCE:
        filtered = [t for t in filtered if t['conference_abbreviation'] == conference]

    key_fns = {
        SortBy.NAME: lambda t: t['name'],
        SortBy.CONFERENCE: lambda t: (t['conference_abbreviation'], t['name']),
        SortBy.TIER: lambda t: (t['tier'], t['name']),
    }
    return sorted(filtered, key=key_fns[sort_by])
