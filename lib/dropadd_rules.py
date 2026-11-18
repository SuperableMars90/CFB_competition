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


def _label(req: dict) -> str:
    who = req.get("player_name") or f"player {req['player_id']}"
    return f"Request #{req['id']} ({who})"


def validate_dropadd_batch(
    approvals: list[dict],
    rosters: dict[int, set[int]],
    pod_of: dict[int, int],
    roster_size: int,
) -> ValidationResult:
    """
    Validate a set of drop/add approvals as one all-or-nothing batch,
    before any of it is written.

    Simulates applying `approvals` in the order given (callers pass them
    oldest-first, the order they'll be applied) against the current
    rosters, so a conflict *between* approvals is caught as well as a
    request that's gone stale since it was submitted — e.g. two approved
    requests dropping the same team, which is the case that let a roster
    grow past its size in 2026 weeks 10-11 (the second drop matched no
    row and was silently a no-op while its add still went through).

    Any error means the whole batch should be rejected; nothing is
    partially valid.

    Args:
        approvals: request dicts with id, player_id, dropped_team_id,
            added_team_id, and optionally player_name (for messages).
        rosters: {player_id: set of active team_ids}, current state.
        pod_of: {player_id: pod_id} for this season. Pods are severed
            rosters, so add-uniqueness is checked per pod.
        roster_size: required active roster size
            (seasons.draft_picks_per_player). Every player touched by the
            batch must end at exactly this size.
    """
    result = ValidationResult()

    sim = {pid: set(teams) for pid, teams in rosters.items()}
    pod_owned: dict[int, set[int]] = {}
    for pid, teams in rosters.items():
        if pid in pod_of:
            pod_owned.setdefault(pod_of[pid], set()).update(teams)

    dropped_by: dict[tuple[int, int], int] = {}   # (player, team) -> request id
    added_by: dict[tuple[int, int], int] = {}     # (pod, team) -> request id
    touched: list[int] = []

    for req in approvals:
        pid = req["player_id"]
        drop, add = req["dropped_team_id"], req["added_team_id"]
        label = _label(req)
        if pid not in touched:
            touched.append(pid)

        if pid not in pod_of:
            result.add_error(f"{label}: player has no pod membership this season.")
            continue
        pod = pod_of[pid]
        roster = sim.setdefault(pid, set())
        owned = pod_owned.setdefault(pod, set())

        if drop is None and add is None:
            result.add_error(f"{label}: request has neither a drop nor an add.")
            continue
        if drop is not None and drop == add:
            result.add_error(f"{label}: drops and adds the same team.")
            continue

        if drop is not None:
            if (pid, drop) in dropped_by:
                result.add_error(
                    f"{label}: drops a team already dropped by "
                    f"request #{dropped_by[(pid, drop)]} in this batch."
                )
            elif drop not in roster:
                result.add_error(f"{label}: the team to drop is no longer on this player's roster.")
            else:
                roster.discard(drop)
                owned.discard(drop)
                dropped_by[(pid, drop)] = req["id"]

        if add is not None:
            if (pod, add) in added_by:
                result.add_error(
                    f"{label}: adds a team already added by "
                    f"request #{added_by[(pod, add)]} in this batch."
                )
            elif add in owned:
                result.add_error(f"{label}: the team to add is already owned in this pod.")
            else:
                roster.add(add)
                owned.add(add)
                added_by[(pod, add)] = req["id"]

    names = {r["player_id"]: r.get("player_name") for r in approvals}
    for pid in touched:
        size = len(sim.get(pid, set()))
        if size != roster_size:
            who = names.get(pid) or f"player {pid}"
            result.add_error(
                f"{who} would end this batch with {size} active teams "
                f"(roster size is {roster_size})."
            )

    return result


def find_shared_drops(pending: list[dict]) -> set[int]:
    """
    Return ids of pending requests where the same player has more than one
    pending request dropping the same team — ranked fallbacks, of which at
    most one should be approved. Used to flag rows in the commissioner view.
    """
    groups: dict[tuple[int, int], list[int]] = {}
    for r in pending:
        if r["dropped_team_id"] is not None:
            groups.setdefault((r["player_id"], r["dropped_team_id"]), []).append(r["id"])
    return {rid for ids in groups.values() if len(ids) > 1 for rid in ids}
