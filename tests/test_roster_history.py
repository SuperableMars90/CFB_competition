"""Unit tests for lib.roster_history — pure Python, no DB/Streamlit needed."""

from datetime import datetime, timedelta, timezone

from lib.roster_history import (
    RosterDiscrepancy,
    compare_reconstructions,
    et_to_utc,
    replay_log_backwards,
    rosters_as_of,
)

DRAFT = datetime(2026, 8, 28, 12, 0)
T1 = datetime(2026, 9, 8, 22, 0)   # player 1 drops 10, adds 30
T2 = datetime(2026, 9, 16, 22, 0)  # player 1 drops 30, re-adds 10
T3 = datetime(2026, 9, 23, 22, 0)  # player 2 drops 20; player 1 picks it up


def _row(player_id, team_id, acquired_at, dropped_at=None):
    return {"player_id": player_id, "team_id": team_id, "acquired_at": acquired_at, "dropped_at": dropped_at}


def _log(log_id, player_id, dropped, added, actioned_at):
    return {"id": log_id, "player_id": player_id, "dropped_team_id": dropped,
            "added_team_id": added, "actioned_at": actioned_at}


# One stint per row: player 1 holds team 10 twice, team 20 changes owner.
ROSTER_ROWS = [
    _row(1, 10, DRAFT, T1),
    _row(1, 11, DRAFT),
    _row(2, 20, DRAFT, T3),
    _row(2, 21, DRAFT),
    _row(1, 30, T1, T2),
    _row(1, 10, T2),
    _row(1, 20, T3),
]

# actioned_at trails the rosters write by up to 1s, as on live data.
LOG_ROWS = [
    _log(1, 1, 10, 30, T1 + timedelta(seconds=1)),
    _log(2, 1, 30, 10, T2 + timedelta(seconds=1)),
    _log(3, 2, 20, None, T3),
    _log(4, 1, None, 20, T3 + timedelta(seconds=1)),
]


# --- et_to_utc ---

def test_et_to_utc_during_edt():
    assert et_to_utc(datetime(2026, 10, 4, 6, 53)) == datetime(2026, 10, 4, 10, 53)


def test_et_to_utc_during_est():
    assert et_to_utc(datetime(2026, 12, 1, 12, 0)) == datetime(2026, 12, 1, 17, 0)


def test_et_to_utc_aware_input_converted_as_is():
    aware = datetime(2026, 10, 4, 10, 53, tzinfo=timezone.utc)
    assert et_to_utc(aware) == datetime(2026, 10, 4, 10, 53)


# --- rosters_as_of ---

def test_before_draft_is_empty():
    assert rosters_as_of(ROSTER_ROWS, DRAFT - timedelta(days=1)) == {}


def test_after_draft():
    assert rosters_as_of(ROSTER_ROWS, DRAFT + timedelta(days=1)) == {1: {10, 11}, 2: {20, 21}}


def test_between_drop_and_readd():
    assert rosters_as_of(ROSTER_ROWS, T1 + timedelta(days=1)) == {1: {11, 30}, 2: {20, 21}}


def test_after_readd_and_owner_change():
    assert rosters_as_of(ROSTER_ROWS, T3 + timedelta(days=1)) == {1: {10, 11, 20}, 2: {21}}


def test_boundaries_acquired_inclusive_dropped_exclusive():
    assert rosters_as_of(ROSTER_ROWS, T1)[1] == {11, 30}


def test_player_with_no_teams_omitted():
    rows = [_row(1, 10, DRAFT, T1)]
    assert rosters_as_of(rows, T1 + timedelta(days=1)) == {}


# --- replay_log_backwards ---

def test_replay_matches_rosters_at_each_point():
    for t in [DRAFT - timedelta(days=1), DRAFT + timedelta(days=1), T1 + timedelta(days=1),
              T2 + timedelta(days=1), T3 + timedelta(days=1)]:
        from_log, anomalies = replay_log_backwards(ROSTER_ROWS, LOG_ROWS, t)
        assert anomalies == []
        # Replay only undoes drop/adds, so before the draft it still shows the
        # drafted teams — compare from the draft onward.
        if t > DRAFT:
            assert from_log == rosters_as_of(ROSTER_ROWS, t), t


def test_replay_flags_missing_log_entry():
    # A hand-run SQL drop of team 11 at T2 with no log row: rosters still
    # has it at T1 + 1 day, but replay can't undo a drop it never saw.
    rows = [r if r["team_id"] != 11 else _row(1, 11, DRAFT, T2) for r in ROSTER_ROWS]
    from_log, anomalies = replay_log_backwards(rows, LOG_ROWS, T1 + timedelta(days=1))
    from_rosters = rosters_as_of(rows, T1 + timedelta(days=1))
    assert compare_reconstructions(from_rosters, from_log, LOG_ROWS, T1 + timedelta(days=1)) == [
        RosterDiscrepancy(player_id=1, team_id=11, in_rosters=True)
    ]


def test_replay_anomaly_when_undoing_add_of_unowned_team():
    log = LOG_ROWS + [_log(5, 2, None, 99, T3 + timedelta(days=1))]
    _, anomalies = replay_log_backwards(ROSTER_ROWS, log, T3)
    assert len(anomalies) == 1 and "team 99" in anomalies[0]


# --- compare_reconstructions ---

def test_write_gap_near_as_of_time_is_tolerated():
    # T falls between the rosters write (T1) and the log write (T1 + 1s).
    from_rosters = rosters_as_of(ROSTER_ROWS, T1)
    from_log, anomalies = replay_log_backwards(ROSTER_ROWS, LOG_ROWS, T1)
    assert from_rosters != from_log
    assert anomalies == []
    assert compare_reconstructions(from_rosters, from_log, LOG_ROWS, T1) == []


def test_write_gap_flagged_with_zero_tolerance():
    from_rosters = rosters_as_of(ROSTER_ROWS, T1)
    from_log, _ = replay_log_backwards(ROSTER_ROWS, LOG_ROWS, T1)
    assert compare_reconstructions(from_rosters, from_log, LOG_ROWS, T1, tolerance=timedelta(0)) == [
        RosterDiscrepancy(player_id=1, team_id=10, in_rosters=False),
        RosterDiscrepancy(player_id=1, team_id=30, in_rosters=True),
    ]
