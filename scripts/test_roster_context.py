"""
Integration test: pull a real player's roster with this week's game context.

Run from project root:
    python -m scripts.test_roster_context [player_id] [season_id] [week]

Defaults to player_id=1, season_id=1, week=1 if no args given.
"""

import sys

from lib.db import get_roster_with_context


def main() -> int:
    # Parse args with sensible defaults for the current test DB state
    player_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    season_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    week = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    print(f"Fetching roster for player_id={player_id}, "
          f"season_id={season_id}, week={week}\n")

    try:
        roster = get_roster_with_context(player_id, season_id, week)
    except Exception as e:
        print(f"  ✗ Query failed: {type(e).__name__}: {e}")
        return 1

    if not roster:
        print("  ⚠ Empty roster. Possibilities:")
        print("     - Player has no roster entries for this season")
        print("     - is_active filter is excluding all rows")
        print("     - Wrong player_id or season_id")
        return 1

    print(f"  ✓ Retrieved {len(roster)} teams\n")

    # Summary stats
    n_on_bye = sum(1 for t in roster if t.is_on_bye)
    n_neutral = sum(1 for t in roster if t.location and t.location.value == "neutral")
    by_tier: dict[str, int] = {}
    for t in roster:
        by_tier[t.tier] = by_tier.get(t.tier, 0) + 1

    print(f"  On bye this week  : {n_on_bye}")
    print(f"  Neutral-site games: {n_neutral}")
    print(f"  By tier           : {dict(sorted(by_tier.items()))}")
    print()

    # Per-team detail
    print(f"  {'Conf':<6} {'Tier':<12} {'Team':<30} Display Label")
    print(f"  {'-'*6} {'-'*12} {'-'*30} {'-'*50}")
    for t in roster:
        print(f"  {t.conference_abbreviation:<6} "
              f"{t.tier:<12} "
              f"{t.name:<30} "
              f"{t.display_label}")

    return 0


if __name__ == "__main__":
    sys.exit(main())