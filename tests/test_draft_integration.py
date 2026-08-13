"""
Integration test for the live draft DB layer: set_draft_order,
submit_draft_picks, get_draft_page_state, is_draft_complete.

Uses synthetic pods/players under the REAL season 8 (rather than a fully
synthetic season) so team-availability queries -- scoped by
teams.season_id -- behave realistically against real team rows. Only
ever references real teams confirmed unrostered at test-write time, and
every fixture tears down everything it created; no real pod/player/
roster row is ever modified.
"""

import pytest

from db.connection import get_connection
from lib.config import CURRENT_SEASON_ID
from lib.db import (
    get_draft_page_state,
    set_draft_order,
    submit_draft_picks,
    is_draft_complete,
    DraftSetupError,
    DraftOutOfSyncError,
)

SEASON_ID = CURRENT_SEASON_ID  # real live season -- see module docstring


def _get_unrostered_team_ids(n):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM teams
            WHERE season_id = %s AND level = 'FBS'
              AND id NOT IN (SELECT team_id FROM rosters WHERE season_id = %s AND is_active = 1)
            LIMIT %s
            """,
            (SEASON_ID, SEASON_ID, n),
        )
        rows = [r[0] for r in cur.fetchall()]
        cur.close()
    assert len(rows) == n, f"expected {n} unrostered teams, found {len(rows)}"
    return rows


def _make_pod(cur, name):
    cur.execute("INSERT INTO pods (season_id, name) VALUES (%s, %s)", (SEASON_ID, name))
    pod_id = cur.lastrowid
    player_ids = []
    for i in range(4):
        cur.execute(
            "INSERT INTO players (name, username, password_hash) VALUES (%s, %s, 'x')",
            (f'{name}P{i}', f'draft_test_{name}_{i}_{pod_id}'),
        )
        player_id = cur.lastrowid
        cur.execute(
            "INSERT INTO pod_memberships (season_id, pod_id, player_id) VALUES (%s, %s, %s)",
            (SEASON_ID, pod_id, player_id),
        )
        player_ids.append(player_id)
    return {'pod_id': pod_id, 'player_ids': player_ids}


def _teardown_pod(cur, pod):
    placeholders = ",".join(["%s"] * len(pod['player_ids']))
    cur.execute("DELETE FROM draft_order WHERE pod_id = %s", (pod['pod_id'],))
    cur.execute(f"DELETE FROM rosters WHERE player_id IN ({placeholders})", tuple(pod['player_ids']))
    cur.execute(f"DELETE FROM pod_memberships WHERE player_id IN ({placeholders})", tuple(pod['player_ids']))
    cur.execute(f"DELETE FROM players WHERE id IN ({placeholders})", tuple(pod['player_ids']))
    cur.execute("DELETE FROM pods WHERE id = %s", (pod['pod_id'],))


@pytest.fixture
def synthetic_pod():
    """One synthetic 4-player pod under the real season 8."""
    with get_connection() as conn:
        cur = conn.cursor()
        pod = _make_pod(cur, 'DraftTest')
        conn.commit()
        cur.close()

    yield pod

    with get_connection() as conn:
        cur = conn.cursor()
        _teardown_pod(cur, pod)
        conn.commit()
        cur.close()


@pytest.fixture
def two_synthetic_pods():
    """Two independent synthetic 4-player pods under the real season 8."""
    with get_connection() as conn:
        cur = conn.cursor()
        pod_a = _make_pod(cur, 'DraftTestA')
        pod_b = _make_pod(cur, 'DraftTestB')
        conn.commit()
        cur.close()

    yield pod_a, pod_b

    with get_connection() as conn:
        cur = conn.cursor()
        _teardown_pod(cur, pod_a)
        _teardown_pod(cur, pod_b)
        conn.commit()
        cur.close()


@pytest.mark.integration
def test_set_draft_order_prepopulates_full_schedule(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']

    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    state = get_draft_page_state(SEASON_ID, pod_id)
    assert len(state.all_slots) == 100
    assert all(s.team_id is None for s in state.all_slots)
    assert [s.pick_number for s in state.all_slots] == list(range(1, 101))
    assert state.all_slots[0].player_id == p1   # pick 1
    assert state.all_slots[3].player_id == p4   # pick 4
    assert state.all_slots[4].player_id == p4   # pick 5 -- boundary carry-over
    assert state.all_slots[7].player_id == p1   # pick 8


@pytest.mark.integration
def test_submit_draft_picks_sets_team_and_writes_roster(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']
    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    team_id = _get_unrostered_team_ids(1)[0]
    submit_draft_picks(SEASON_ID, pod_id, p1, [team_id])

    state = get_draft_page_state(SEASON_ID, pod_id)
    assert state.pick_log[0].team_id == team_id
    assert state.open_slots[0].pick_number == 2

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM rosters WHERE player_id=%s AND team_id=%s "
            "AND season_id=%s AND acquisition_type='draft'",
            (p1, team_id, SEASON_ID),
        )
        assert cur.fetchone()[0] == 1
        cur.close()


@pytest.mark.integration
def test_submit_draft_picks_wrong_player_raises(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']
    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    team_id = _get_unrostered_team_ids(1)[0]
    with pytest.raises(DraftOutOfSyncError):
        submit_draft_picks(SEASON_ID, pod_id, p2, [team_id])  # p1 is actually on the clock


@pytest.mark.integration
def test_submit_draft_picks_team_already_taken_raises(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']
    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    team_id = _get_unrostered_team_ids(1)[0]
    # Simulate a genuine race: the team gets rostered via some other path
    # between when the page was loaded and when this submission arrives.
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO rosters (player_id, team_id, season_id, pod_id, is_active, acquisition_type) "
            "VALUES (%s, %s, %s, %s, TRUE, 'draft')",
            (p4, team_id, SEASON_ID, pod_id),
        )
        conn.commit()
        cur.close()

    try:
        with pytest.raises(DraftOutOfSyncError):
            submit_draft_picks(SEASON_ID, pod_id, p1, [team_id])

        state = get_draft_page_state(SEASON_ID, pod_id)
        assert state.open_slots[0].pick_number == 1  # nothing written for pick 1
    finally:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM rosters WHERE player_id=%s AND team_id=%s AND season_id=%s",
                (p4, team_id, SEASON_ID),
            )
            conn.commit()
            cur.close()


@pytest.mark.integration
def test_double_pick_at_round_boundary(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']
    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    teams = _get_unrostered_team_ids(5)
    submit_draft_picks(SEASON_ID, pod_id, p1, [teams[0]])
    submit_draft_picks(SEASON_ID, pod_id, p2, [teams[1]])
    submit_draft_picks(SEASON_ID, pod_id, p3, [teams[2]])
    # Pick 4 (p4) and pick 5 (p4 again, round boundary) submitted together.
    submit_draft_picks(SEASON_ID, pod_id, p4, [teams[3], teams[4]])

    state = get_draft_page_state(SEASON_ID, pod_id)
    assert state.open_slots[0].pick_number == 6
    made = {s.pick_number: s.team_id for s in state.pick_log}
    assert made[4] in teams[3:5] and made[5] in teams[3:5]
    assert made[4] != made[5]


@pytest.mark.integration
def test_double_pick_wrong_count_raises(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']
    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)

    teams = _get_unrostered_team_ids(3)
    submit_draft_picks(SEASON_ID, pod_id, p1, [teams[0]])
    submit_draft_picks(SEASON_ID, pod_id, p2, [teams[1]])
    submit_draft_picks(SEASON_ID, pod_id, p3, [teams[2]])
    # Pick 4/5 is a legitimate double for p4 -- submitting only 1 should fail.
    extra = _get_unrostered_team_ids(4)[3]
    with pytest.raises(DraftOutOfSyncError):
        submit_draft_picks(SEASON_ID, pod_id, p4, [extra])


@pytest.mark.integration
def test_is_draft_complete_and_setup_lock(synthetic_pod):
    pod_id = synthetic_pod['pod_id']
    p1, p2, p3, p4 = synthetic_pod['player_ids']

    assert is_draft_complete(SEASON_ID, pod_id) is False  # not even set up yet

    set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)
    assert is_draft_complete(SEASON_ID, pod_id) is False  # set up, no picks made

    team_id = _get_unrostered_team_ids(1)[0]
    submit_draft_picks(SEASON_ID, pod_id, p1, [team_id])

    with pytest.raises(DraftSetupError):
        set_draft_order(SEASON_ID, pod_id, [p1, p2, p3, p4], set_by=p1)


@pytest.mark.integration
def test_pod_scoped_availability_two_pods(two_synthetic_pods):
    pod_a, pod_b = two_synthetic_pods
    set_draft_order(SEASON_ID, pod_a['pod_id'], pod_a['player_ids'], set_by=pod_a['player_ids'][0])
    set_draft_order(SEASON_ID, pod_b['pod_id'], pod_b['player_ids'], set_by=pod_b['player_ids'][0])

    team_id = _get_unrostered_team_ids(1)[0]
    submit_draft_picks(SEASON_ID, pod_a['pod_id'], pod_a['player_ids'][0], [team_id])

    # Team drafted in Pod A must still be available in Pod B's own draft.
    state_b = get_draft_page_state(SEASON_ID, pod_b['pod_id'])
    assert team_id in {t['team_id'] for t in state_b.available_teams}


@pytest.fixture
def synthetic_3player_pod_season():
    """
    A fully synthetic season (draft_picks_per_player=30) plus one
    synthetic 3-player pod under it -- proves set_draft_order() reads
    the per-season config rather than a hardcoded pick count. No teams
    needed: this only exercises draft_order, not team assignment.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO seasons (year, last_week, draft_picks_per_player) VALUES (%s, %s, %s)",
            (9998, 15, 30),
        )
        season_id = cur.lastrowid
        cur.execute("INSERT INTO pods (season_id, name) VALUES (%s, %s)", (season_id, 'ThreePlayerTest'))
        pod_id = cur.lastrowid
        player_ids = []
        for i in range(3):
            cur.execute(
                "INSERT INTO players (name, username, password_hash) VALUES (%s, %s, 'x')",
                (f'ThreePlayerTestP{i}', f'draft_test_3p_{i}_{pod_id}'),
            )
            player_id = cur.lastrowid
            cur.execute(
                "INSERT INTO pod_memberships (season_id, pod_id, player_id) VALUES (%s, %s, %s)",
                (season_id, pod_id, player_id),
            )
            player_ids.append(player_id)
        conn.commit()
        cur.close()

    yield {'season_id': season_id, 'pod_id': pod_id, 'player_ids': player_ids}

    with get_connection() as conn:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(player_ids))
        cur.execute("DELETE FROM draft_order WHERE pod_id = %s", (pod_id,))
        cur.execute(f"DELETE FROM pod_memberships WHERE player_id IN ({placeholders})", tuple(player_ids))
        cur.execute(f"DELETE FROM players WHERE id IN ({placeholders})", tuple(player_ids))
        cur.execute("DELETE FROM pods WHERE id = %s", (pod_id,))
        cur.execute("DELETE FROM seasons WHERE id = %s", (season_id,))
        conn.commit()
        cur.close()


@pytest.mark.integration
def test_set_draft_order_uses_season_draft_picks_per_player(synthetic_3player_pod_season):
    """3 players x 30 picks/player (this season's config) = 90 total slots,
    not the 4x25=100 every other test in this file exercises against real
    season 8 -- confirms set_draft_order() reads seasons.draft_picks_per_player
    per-season rather than a hardcoded constant."""
    season_id = synthetic_3player_pod_season['season_id']
    pod_id = synthetic_3player_pod_season['pod_id']
    p1, p2, p3 = synthetic_3player_pod_season['player_ids']

    set_draft_order(season_id, pod_id, [p1, p2, p3], set_by=p1)

    state = get_draft_page_state(season_id, pod_id)
    assert len(state.all_slots) == 90
    assert [s.pick_number for s in state.all_slots] == list(range(1, 91))
    assert state.all_slots[0].player_id == p1   # pick 1
    assert state.all_slots[2].player_id == p3   # pick 3
    assert state.all_slots[3].player_id == p3   # pick 4 -- boundary carry-over (3-player snake)
