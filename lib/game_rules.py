"""
Game rules and lineup validation for the CFB Game.

Pure Python — no database access, no Streamlit, no I/O of any kind.
All inputs are plain data; outputs are ValidationResult objects.

Lineup structure (16 total slots, 2026 format):
    - 10 conference slots: one slot per lineup conference (4 P4 + 6 G6),
      filled by a team whose conference abbreviation matches the slot
    - 3 P4 flex slots: any team from a P4-tier conference
    - 2 G6 flex slots: any team from a G6-tier conference
    - 1 wildcard slot: any team from a P4- or G6-tier conference

Passing:
    Every slot may be PASSED instead of filled. A pass is a Pick with
    team=None. It is always structurally valid for any slot, scores an
    automatic zero, and cannot produce negative points. Passing is a
    normal strategic choice (e.g. your only eligible team faces a likely
    blowout loss) and is also how a player resolves a conference slot
    they own no eligible team for. Scoring — not validation — is what
    turns a passed slot into a zero; this module only permits it.

Independent teams:
    Independents are modeled as bucket conferences (IndP4 / IndG6) that
    are NOT lineup slots. Their teams carry a P4 or G6 tier.

    Settled rule (2026-07-04, firm — do not revert): a P4 independent is
    eligible for EVERY P4 conference slot, every P4 flex slot, and the
    wildcard slot. A G6 independent is eligible for EVERY G6 conference
    slot, every G6 flex slot, and the wildcard slot — exactly as if it
    belonged to all of that tier's conferences simultaneously. This is
    broader than a plain tier match on flex/wildcard alone: it also
    covers the 4/6 named conference slots (ACC/B12/B1G/SEC and
    AAC/CUSA/MAC/MWC/PAC/SBC), which a non-independent team can only
    fill by matching that exact conference.

FCS teams carry an FCS tier, so they match no flex/wildcard eligibility
and no P4/G6 conference slot — they are ineligible everywhere.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from lib.game_context import TeamGameContext


# -----------------------------------------------------------------
# Slot types
# -----------------------------------------------------------------

class SlotType(str, Enum):
    CONFERENCE = "conference"
    P4_FLEX = "p4_flex"
    G6_FLEX = "g6_flex"
    WILDCARD = "wildcard"


# -----------------------------------------------------------------
# Lineup structure constants (2026 format)
# -----------------------------------------------------------------

REQUIRED_P4_FLEX_SLOTS = 3
REQUIRED_G6_FLEX_SLOTS = 2
REQUIRED_WILDCARD_SLOTS = 1
# 10 conference slots + 3 P4 flex + 2 G6 flex + 1 wildcard
TOTAL_LINEUP_SIZE = 16

# Tiers eligible for each flex/wildcard slot type
_FLEX_ELIGIBLE_TIERS: dict[SlotType, frozenset[str]] = {
    SlotType.P4_FLEX: frozenset({"P4"}),
    SlotType.G6_FLEX: frozenset({"G6"}),
    SlotType.WILDCARD: frozenset({"P4", "G6"}),
}


def build_slot_manifest(
    conference_slot_tiers: dict[str, str],
) -> tuple[list[tuple[SlotType, str, str]], list[str], list[str]]:
    """
    Ordered list of (slot_type, slot_identifier, label) for all 16 lineup
    slots, plus the P4 and G6 conference abbreviation lists used to group
    them. Shared by any page that renders or edits a lineup (submission,
    history) so slot order/grouping can't drift between them.

    conference_slot_tiers maps conference abbreviation -> "P4"/"G6", as
    returned by lib.db.get_conference_slot_tiers.
    """
    p4 = sorted(a for a, t in conference_slot_tiers.items() if t == "P4")
    g6 = sorted(a for a, t in conference_slot_tiers.items() if t == "G6")
    manifest: list[tuple[SlotType, str, str]] = []
    for abbr in p4 + g6:
        manifest.append((SlotType.CONFERENCE, abbr, abbr))
    for i in range(1, REQUIRED_P4_FLEX_SLOTS + 1):
        manifest.append((SlotType.P4_FLEX, str(i), f"P4 Flex {i}"))
    for i in range(1, REQUIRED_G6_FLEX_SLOTS + 1):
        manifest.append((SlotType.G6_FLEX, str(i), f"G6 Flex {i}"))
    manifest.append((SlotType.WILDCARD, "1", "Wildcard"))
    return manifest, p4, g6


# -----------------------------------------------------------------
# Pick: a single slot resolution (a team, or a pass)
# -----------------------------------------------------------------

@dataclass(frozen=True)
class Pick:
    """
    One slot in a lineup, resolved either to a team or to a pass.

    team is None for a passed slot. A passed slot still occupies its
    slot_type and slot_identifier; it simply has no team.

    game_id is None for a regular current-week pick. When set, scoring
    resolves against that specific game (e.g. a banked week 0 game).
    Two picks for the same team are legal when their game_ids differ.
    """
    slot_type: SlotType
    slot_identifier: str   # conference abbreviation, or "1"/"2"/"3" for flex/wildcard
    team: Optional[TeamGameContext]
    game_id: Optional[int] = None

    @property
    def is_pass(self) -> bool:
        return self.team is None


# -----------------------------------------------------------------
# Validation result
# -----------------------------------------------------------------

@dataclass
class ValidationResult:
    """
    Outcome of a validation check.

    is_valid is True iff there are no errors. Warnings are informational
    and do not invalidate the result.
    """
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def merge(self, other: "ValidationResult") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


# -----------------------------------------------------------------
# Per-pick validation
# -----------------------------------------------------------------

def validate_pick(
    pick: Pick,
    roster_team_ids: set[int],
    conference_slot_tiers: dict[str, str],
) -> ValidationResult:
    """
    Validate one pick.

    A pass (team is None) is always valid. Otherwise the team must be on
    the player's roster and eligible for the slot:
        - conference slot: team's conference abbreviation == slot
          identifier, OR the team is an independent (its conference isn't
          one of this season's real lineup-slot conferences) whose tier
          matches this slot's tier — see module docstring's "Independent
          teams" section (settled 2026-07-04): an independent is eligible
          for EVERY conference slot of its own tier, not just flex/wildcard.
        - flex/wildcard:   team's tier is eligible for the slot type

    conference_slot_tiers: {abbreviation: tier} for this season's real
    lineup-slot conferences (from get_conference_slot_tiers) — needed
    both to know each slot's tier for the independent check above, and
    (at the lineup level, not here) whether a conference slot is legitimate.
    """
    result = ValidationResult()

    if pick.is_pass:
        return result

    team = pick.team

    # Roster ownership
    if team.team_id not in roster_team_ids:
        result.add_error(f"{team.name} is not on the player's roster.")

    # Slot eligibility
    if pick.slot_type == SlotType.CONFERENCE:
        slot_tier = conference_slot_tiers.get(pick.slot_identifier)
        is_exact_conference = team.conference_abbreviation == pick.slot_identifier
        is_matching_independent = (
            team.conference_abbreviation not in conference_slot_tiers
            and team.tier == slot_tier
        )
        if not (is_exact_conference or is_matching_independent):
            result.add_error(
                f"{team.name} ({team.conference_abbreviation}) "
                f"cannot fill the {pick.slot_identifier} conference slot."
            )
    else:
        eligible_tiers = _FLEX_ELIGIBLE_TIERS.get(pick.slot_type, frozenset())
        if team.tier not in eligible_tiers:
            slot_label = pick.slot_type.value.replace("_", " ")
            result.add_error(
                f"{team.name} (tier: {team.tier}) "
                f"cannot fill a {slot_label} slot."
            )

    return result


# -----------------------------------------------------------------
# Full-lineup validation
# -----------------------------------------------------------------

def validate_lineup(
    picks: Iterable[Pick],
    roster_team_ids: set[int],
    conference_slot_tiers: dict[str, str],
) -> ValidationResult:
    """
    Validate a full lineup.

    Args:
        picks: All 16 slot resolutions in the proposed lineup. Each is a
            team or a pass (team=None).
        roster_team_ids: Team IDs the player owns.
        conference_slot_tiers: {abbreviation: tier} for the conferences
            that are lineup slots this season (from get_conference_slot_tiers).
            Drives which conference slots must be present.

    A passed slot is structurally valid but still must be present: every
    conference slot and every flex/wildcard slot must appear exactly once,
    whether filled or passed.
    """
    result = ValidationResult()
    picks = list(picks)

    # Per-pick eligibility (passes auto-pass)
    for pick in picks:
        result.merge(validate_pick(pick, roster_team_ids, conference_slot_tiers))

    # No (team, game) pair used more than once. The same team may appear twice
    # if one pick targets their current-week game and another targets a banked
    # week 0 game (different game_ids), but identical (team_id, game_id) pairs
    # are duplicates regardless of slot.
    seen: dict[tuple, list[Pick]] = {}
    for p in picks:
        if p.is_pass:
            continue
        key = (p.team.team_id, p.game_id)
        seen.setdefault(key, []).append(p)
    for (_tid, _gid), dupes in seen.items():
        if len(dupes) > 1:
            result.add_error(
                f"{dupes[0].team.name} is used in {len(dupes)} slots for the same game; "
                f"each team-game combination may be used at most once."
            )

    # Conference slots present must exactly match the season's slots
    required_conferences = set(conference_slot_tiers.keys())
    conference_slots_present = {
        p.slot_identifier for p in picks
        if p.slot_type == SlotType.CONFERENCE
    }
    missing = required_conferences - conference_slots_present
    if missing:
        result.add_error(
            f"Missing conference slot(s): {', '.join(sorted(missing))}"
        )
    unexpected = conference_slots_present - required_conferences
    if unexpected:
        result.add_error(
            f"Unexpected conference slot(s): {', '.join(sorted(unexpected))}"
        )

    # Correct number of flex/wildcard slots (passed slots still count)
    expected_counts = {
        SlotType.P4_FLEX: REQUIRED_P4_FLEX_SLOTS,
        SlotType.G6_FLEX: REQUIRED_G6_FLEX_SLOTS,
        SlotType.WILDCARD: REQUIRED_WILDCARD_SLOTS,
    }
    actual_counts = {st: 0 for st in expected_counts}
    for p in picks:
        if p.slot_type in actual_counts:
            actual_counts[p.slot_type] += 1
    for slot_type, expected in expected_counts.items():
        actual = actual_counts[slot_type]
        if actual != expected:
            label = slot_type.value.replace("_", " ")
            result.add_error(
                f"Expected {expected} {label} slot(s), got {actual}."
            )

    # Total lineup size
    if len(picks) != TOTAL_LINEUP_SIZE:
        result.add_error(
            f"Lineup must have exactly {TOTAL_LINEUP_SIZE} slots, "
            f"got {len(picks)}."
        )

    # Informational: surface passes for the confirmation step.
    # (Remove if you'd rather compute this in the UI.)
    passed = [p for p in picks if p.is_pass]
    if passed:
        labels = sorted(
            p.slot_identifier if p.slot_type == SlotType.CONFERENCE
            else p.slot_type.value.replace("_", " ")
            for p in passed
        )
        result.add_warning(
            f"{len(passed)} slot(s) passed (each scores 0): {', '.join(labels)}"
        )

    return result