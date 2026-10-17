"""
Point-in-time roster reconstruction ("time travel") — pure Python, no DB
access, no Streamlit, no I/O. Callers fetch rows and pass them in
(lib.db.get_rosters_as_of() is the DB-backed entry point).

Source of truth is the rosters table itself: every ownership stint is
its own row (an add always inserts a new row; a drop only stamps
dropped_at / clears is_active on the existing one), so the roster at
time T is every row with acquired_at <= T < dropped_at (or never
dropped). Re-adds and ownership changes are separate rows, so nothing is
lost — verified against live season 68, 2026-10-04.

drop_add_log is used only as an independent cross-check: start from the
current active rosters and undo every logged transaction after T,
newest first. If the two reconstructions disagree, something touched
one table without the other (e.g. a hand-run SQL fix), and callers
should surface that rather than trust either answer.

All DB timestamps are naive UTC (Aiven NOW()); callers query in ET via
et_to_utc().
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# drop_add_log.actioned_at is written by a later statement in the same
# transaction as the rosters change, so it trails the rosters timestamp
# by 0-1s on live data. Discrepancies for a team with a log event this
# close to the as-of time are that gap, not real drift.
DEFAULT_LOG_TOLERANCE = timedelta(seconds=5)


def et_to_utc(as_of_et: datetime) -> datetime:
    """
    Convert an ET wall-clock time to naive UTC for comparison against DB
    timestamps. A naive input is interpreted as America/New_York (DST
    handled by zoneinfo; during the fall-back hour, the earlier EDT
    reading wins, per fold=0). A tz-aware input is converted as-is.
    """
    if as_of_et.tzinfo is None:
        as_of_et = as_of_et.replace(tzinfo=ET)
    return as_of_et.astimezone(timezone.utc).replace(tzinfo=None)


def rosters_as_of(roster_rows: list[dict], as_of_utc: datetime) -> dict[int, set[int]]:
    """
    {player_id: {team_id, ...}} owned at as_of_utc, from rosters rows
    (each needing player_id, team_id, acquired_at, dropped_at). A team
    acquired exactly at T counts as owned; one dropped exactly at T
    doesn't. Players owning nothing at T are omitted.
    """
    result: dict[int, set[int]] = {}
    for row in roster_rows:
        dropped_at = row["dropped_at"]
        if row["acquired_at"] <= as_of_utc and (dropped_at is None or dropped_at > as_of_utc):
            result.setdefault(row["player_id"], set()).add(row["team_id"])
    return result


def replay_log_backwards(
    roster_rows: list[dict], log_rows: list[dict], as_of_utc: datetime
) -> tuple[dict[int, set[int]], list[str]]:
    """
    Reconstruct rosters at as_of_utc by starting from the current active
    rosters (rows with dropped_at NULL) and undoing every drop_add_log
    row (needing id, player_id, dropped_team_id, added_team_id,
    actioned_at) with actioned_at > as_of_utc, newest first.

    Returns (rosters, anomalies). anomalies lists log rows whose undo
    didn't fit the roster being rebuilt (undoing an add of a team the
    player didn't have, or a drop of one they still had) — each is a
    sign the log and rosters have diverged. Players owning nothing at T
    are omitted, matching rosters_as_of().
    """
    current: dict[int, set[int]] = {}
    for row in roster_rows:
        if row["dropped_at"] is None:
            current.setdefault(row["player_id"], set()).add(row["team_id"])

    anomalies: list[str] = []
    later = [r for r in log_rows if r["actioned_at"] > as_of_utc]
    for log in sorted(later, key=lambda r: (r["actioned_at"], r["id"]), reverse=True):
        teams = current.setdefault(log["player_id"], set())
        added, dropped = log["added_team_id"], log["dropped_team_id"]
        if added is not None:
            if added in teams:
                teams.remove(added)
            else:
                anomalies.append(
                    f"drop_add_log {log['id']}: undoing add of team {added}, "
                    f"but player {log['player_id']} doesn't have it"
                )
        if dropped is not None:
            if dropped in teams:
                anomalies.append(
                    f"drop_add_log {log['id']}: undoing drop of team {dropped}, "
                    f"but player {log['player_id']} still has it"
                )
            teams.add(dropped)

    return {pid: teams for pid, teams in current.items() if teams}, anomalies


@dataclass(frozen=True)
class RosterDiscrepancy:
    player_id: int
    team_id: int
    in_rosters: bool  # True: rosters table says owned, log replay says not


def compare_reconstructions(
    from_rosters: dict[int, set[int]],
    from_log: dict[int, set[int]],
    log_rows: list[dict],
    as_of_utc: datetime,
    tolerance: timedelta = DEFAULT_LOG_TOLERANCE,
) -> list[RosterDiscrepancy]:
    """
    Every (player, team) the two reconstructions disagree on, sorted,
    excluding pairs with a drop_add_log event within `tolerance` of
    as_of_utc (the rosters/log write gap — see DEFAULT_LOG_TOLERANCE).
    """
    near_t = {
        (log["player_id"], team_id)
        for log in log_rows
        if abs(log["actioned_at"] - as_of_utc) <= tolerance
        for team_id in (log["added_team_id"], log["dropped_team_id"])
        if team_id is not None
    }

    discrepancies = []
    for player_id in from_rosters.keys() | from_log.keys():
        a = from_rosters.get(player_id, set())
        b = from_log.get(player_id, set())
        for team_id in a ^ b:
            if (player_id, team_id) not in near_t:
                discrepancies.append(RosterDiscrepancy(player_id, team_id, team_id in a))
    return sorted(discrepancies, key=lambda d: (d.player_id, d.team_id))


class RosterHistoryMismatch(Exception):
    """rosters and drop_add_log disagree about who owned what at the as-of time."""

    def __init__(self, as_of_utc: datetime, discrepancies: list[RosterDiscrepancy], anomalies: list[str]):
        self.as_of_utc = as_of_utc
        self.discrepancies = discrepancies
        self.anomalies = anomalies
        lines = [f"rosters vs drop_add_log disagree at {as_of_utc} UTC:"]
        lines += [
            f"  player {d.player_id} / team {d.team_id}: "
            + ("owned per rosters, not per log replay" if d.in_rosters else "owned per log replay, not per rosters")
            for d in discrepancies
        ]
        lines += [f"  {a}" for a in anomalies]
        super().__init__("\n".join(lines))
