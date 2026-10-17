"""
lib/projection.py
-----------------
Pure-Python projected-score math and live-game display helpers for the live
weekly status page. No DB access, no Streamlit, no I/O -- callers fetch
scoreboard/line data and pass it in.

Conventions
-----------
* CFBD's `spread` is home-team-relative: NEGATIVE means the home team is
  favored (South Carolina -3 at home = home favored by 3). That raw number
  is what `game_lines.home_spread` stores.
* Everything user-facing uses "expected margin" instead: the points a team
  is expected to win by, so a favorite is POSITIVE (Tennessee favored by 10
  shows as "Tennessee (+10)") and an underdog is negative. `expected_margin()`
  is the one place that flips the sign.

Projection formula
------------------
    projected margin = current margin + expected margin * (minutes left / 60)

The favorite is "due" to outscore the underdog by (expected margin / 60)
points per minute for whatever time is left. Before kickoff that is the full
expected margin; once the game is final it is just the actual margin.
Overtime counts as zero minutes remaining.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

QUARTER_MINUTES = 15.0
QUARTERS = 4
REGULATION_MINUTES = QUARTER_MINUTES * QUARTERS


def average_spread(lines: list[dict]) -> Optional[float]:
    """
    Mean of the non-null `spread` values across providers' lines for one
    game (CFBD `/lines` `lines` list), rounded to 2 places. None if no
    provider has a spread.
    """
    values = [l["spread"] for l in lines if l.get("spread") is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def expected_margin(home_spread: Optional[float], is_home: bool) -> Optional[float]:
    """
    A team's expected margin of victory from the home-relative CFBD spread:
    favorites positive, underdogs negative. None if there is no line.
    """
    if home_spread is None:
        return None
    value = -home_spread if is_home else home_spread
    return value + 0.0  # normalizes -0.0 to 0.0


def parse_clock_minutes(clock: Optional[str]) -> Optional[float]:
    """'MM:SS' game clock -> minutes as a float. None if missing/unparseable."""
    if not clock:
        return None
    try:
        minutes, seconds = clock.split(":")
        return int(minutes) + int(seconds) / 60
    except (ValueError, AttributeError):
        return None


def minutes_remaining(status: Optional[str], period: Optional[int], clock: Optional[str]) -> float:
    """
    Regulation minutes left in a game.

    final -> 0. Not started (or status unknown) -> 60. In progress ->
    full quarters left plus the clock in the current one. Overtime
    (period > 4) -> 0. A missing clock is read as halftime (0 left in the
    period) in period 2 and as the start of the period otherwise; an
    in-progress game with no period at all is treated as not yet started.
    """
    if status == "final":
        return 0.0
    if status != "in_progress":
        return REGULATION_MINUTES
    if period is None or period < 1:
        return REGULATION_MINUTES
    if period > QUARTERS:
        return 0.0
    clock_minutes = parse_clock_minutes(clock)
    if clock_minutes is None:
        clock_minutes = 0.0 if period == 2 else QUARTER_MINUTES
    clock_minutes = min(max(clock_minutes, 0.0), QUARTER_MINUTES)
    return (QUARTERS - period) * QUARTER_MINUTES + clock_minutes


def projected_margin(
    current_margin: Optional[float],
    team_expected_margin: Optional[float],
    minutes_left: float,
) -> float:
    """
    Projected final margin for one team. A missing current margin (game not
    started) counts as 0; a missing line contributes nothing, so the
    projection falls back to the current margin.
    """
    base = current_margin or 0
    if team_expected_margin is None:
        return float(base)
    return base + team_expected_margin * (minutes_left / REGULATION_MINUTES)


def game_started(status: Optional[str], start_date: Optional[str], now: datetime) -> bool:
    """
    True once a game has kicked off. Trusts the scoreboard status when there
    is one; otherwise falls back to comparing CFBD's ISO 8601 `startDate`
    (UTC, e.g. '2026-09-19T20:15:00.000Z') against `now` (tz-aware).
    """
    if status is not None:
        return status in ("in_progress", "final")
    if not start_date:
        return False
    try:
        kickoff = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
    except ValueError:
        return False
    return kickoff <= now


def format_game_clock(period: Optional[int], clock: Optional[str]) -> str:
    """Compact game-time label: 'Q3 4:12', 'Half', 'OT', '2OT', or 'Live'."""
    if period is None or period < 1:
        return "Live"
    if period > QUARTERS:
        n = period - QUARTERS
        return "OT" if n == 1 else f"{n}OT"
    minutes = parse_clock_minutes(clock)
    if period == 2 and (minutes is None or minutes == 0):
        return "Half"
    if minutes is None:
        return f"Q{period}"
    whole = int(minutes)
    seconds = round((minutes - whole) * 60)
    return f"Q{period} {whole}:{seconds:02d}"


def build_live_games(
    live_scores: dict[int, dict],
    lines: dict[int, dict],
    owners: Optional[dict[int, dict[str, list[str]]]] = None,
) -> list[dict]:
    """
    JSON-ready list of the games currently in progress, for the live-games
    sections of the status page.

    live_scores: {cfbd_game_id: {status, home_points, away_points, period,
        clock, start_date, division, home_name, away_name, spread}} as built
        by scripts/scoring_engine.py:fetch_live_scores().
    lines: {cfbd_game_id: {home_spread, home_team, away_team}} from
        lib.db.get_game_lines(). Preferred over the scoreboard's own spread
        (a single provider, not the stored average) and its longer team
        names ("Tennessee Volunteers" vs "Tennessee").
    owners: {cfbd_game_id: {"home": [player names], "away": [player names]}}
        -- the players with that side in this week's lineup, shown next to
        the team name.

    The spread is reported once, on the favorite ("favorite" name plus
    "favLine", the negative betting-style line, e.g. -7). A pick'em has
    favorite None and favLine 0; no line at all has both None.

    Sorted by kickoff then home team so rows keep a stable order between
    refreshes instead of reshuffling as the games progress.
    """
    rows = []
    for gid, game in live_scores.items():
        if game.get("status") != "in_progress":
            continue
        line = lines.get(gid)
        if line is not None:
            home_spread = float(line["home_spread"])
            home_name, away_name = line["home_team"], line["away_team"]
        else:
            home_spread = game.get("spread")
            home_name, away_name = game.get("home_name"), game.get("away_name")
        home_margin = expected_margin(home_spread, True)
        if home_margin is None or home_margin == 0:
            favorite, fav_line = None, home_margin
        elif home_margin > 0:
            favorite, fav_line = home_name, -home_margin
        else:
            favorite, fav_line = away_name, home_margin
        sides = (owners or {}).get(gid, {})
        rows.append({
            "id": gid,
            "division": game.get("division", "fbs"),
            "home": home_name,
            "away": away_name,
            "homePts": game.get("home_points"),
            "awayPts": game.get("away_points"),
            "time": format_game_clock(game.get("period"), game.get("clock")),
            "favorite": favorite,
            "favLine": fav_line,
            "homePlayers": list(sides.get("home", [])),
            "awayPlayers": list(sides.get("away", [])),
            "_start": game.get("start_date") or "",
        })
    rows.sort(key=lambda r: (r["_start"], r["home"] or ""))
    for r in rows:
        del r["_start"]
    return rows
