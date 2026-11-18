"""Unit tests for lib.dropadd_rules — pure Python, no DB/Streamlit needed."""

from lib.dropadd_rules import validate_dropadd_request


def test_drop_only_valid_when_team_on_roster():
    result = validate_dropadd_request(
        dropped_team_id=1, added_team_id=None,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert result.is_valid


def test_drop_rejected_when_team_not_on_roster():
    result = validate_dropadd_request(
        dropped_team_id=99, added_team_id=None,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert not result.is_valid
    assert any("not on your active roster" in e for e in result.errors)


def test_add_only_valid_when_team_unowned():
    result = validate_dropadd_request(
        dropped_team_id=None, added_team_id=50,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert result.is_valid


def test_add_rejected_when_already_on_own_roster():
    result = validate_dropadd_request(
        dropped_team_id=None, added_team_id=1,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert not result.is_valid
    assert any("already on your roster" in e for e in result.errors)


def test_add_rejected_when_owned_by_another_player():
    result = validate_dropadd_request(
        dropped_team_id=None, added_team_id=3,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert not result.is_valid
    assert any("already owned by another player" in e for e in result.errors)


def test_drop_and_add_together_valid():
    result = validate_dropadd_request(
        dropped_team_id=1, added_team_id=50,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert result.is_valid


def test_drop_and_add_same_team_rejected():
    result = validate_dropadd_request(
        dropped_team_id=1, added_team_id=1,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert not result.is_valid
    assert any("Cannot drop and add the same team" in e for e in result.errors)


def test_neither_drop_nor_add_rejected():
    result = validate_dropadd_request(
        dropped_team_id=None, added_team_id=None,
        roster_team_ids={1, 2}, owned_team_ids={1, 2, 3},
    )
    assert not result.is_valid
    assert any("Specify a team to drop, add, or both" in e for e in result.errors)


# -----------------------------------------------------------------
# validate_dropadd_batch / find_shared_drops
# -----------------------------------------------------------------

from lib.dropadd_rules import validate_dropadd_batch, find_shared_drops


def _req(rid, pid, drop, add, name=None):
    return {"id": rid, "player_id": pid, "dropped_team_id": drop,
            "added_team_id": add, "player_name": name}


# Two players in pod 10, one in pod 20; roster size 3.
ROSTERS = {1: {1, 2, 3}, 2: {4, 5, 6}, 3: {1, 2, 3}}
PODS = {1: 10, 2: 10, 3: 20}


def test_batch_valid_swaps():
    result = validate_dropadd_batch(
        [_req(100, 1, 1, 50), _req(101, 2, 4, 51)], ROSTERS, PODS, 3)
    assert result.is_valid, result.errors


def test_batch_rejects_same_team_dropped_twice():
    # The Jacob week-10 case: two fallbacks for one drop both approved.
    result = validate_dropadd_batch(
        [_req(157, 1, 1, 50), _req(158, 1, 1, 51)], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("#158" in e and "already dropped by request #157" in e for e in result.errors)
    assert any("4 active teams" in e for e in result.errors)


def test_batch_rejects_same_team_added_twice_in_pod():
    result = validate_dropadd_batch(
        [_req(100, 1, 1, 50), _req(101, 2, 4, 50)], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("#101" in e and "already added by request #100" in e for e in result.errors)


def test_batch_allows_same_add_in_different_pods():
    result = validate_dropadd_batch(
        [_req(100, 1, 1, 50), _req(101, 3, 1, 50)], ROSTERS, PODS, 3)
    assert result.is_valid, result.errors


def test_batch_rejects_add_owned_elsewhere_in_pod():
    result = validate_dropadd_batch([_req(100, 1, 1, 4)], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("already owned in this pod" in e for e in result.errors)


def test_batch_add_of_team_dropped_earlier_in_batch_by_podmate():
    # Player 2 drops 4; player 1 can then pick 4 up in the same batch.
    result = validate_dropadd_batch(
        [_req(100, 2, 4, 50), _req(101, 1, 1, 4)], ROSTERS, PODS, 3)
    assert result.is_valid, result.errors


def test_batch_rejects_stale_drop():
    # The Jacob week-11 case: team already gone before the batch.
    result = validate_dropadd_batch([_req(100, 1, 99, 50)], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("no longer on this player's roster" in e for e in result.errors)


def test_batch_rejects_add_only_that_grows_roster():
    result = validate_dropadd_batch([_req(100, 1, None, 50, "Jacob")], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("Jacob would end this batch with 4" in e for e in result.errors)


def test_batch_allows_drop_only_that_corrects_oversized_roster():
    rosters = {**ROSTERS, 1: {1, 2, 3, 7}}
    result = validate_dropadd_batch([_req(100, 1, 7, None)], rosters, PODS, 3)
    assert result.is_valid, result.errors


def test_batch_drop_only_plus_add_only_balances():
    result = validate_dropadd_batch(
        [_req(100, 1, 1, None), _req(101, 1, None, 50)], ROSTERS, PODS, 3)
    assert result.is_valid, result.errors


def test_batch_rejects_player_without_pod():
    result = validate_dropadd_batch([_req(100, 9, None, 50)], ROSTERS, PODS, 3)
    assert not result.is_valid
    assert any("no pod membership" in e for e in result.errors)


def test_batch_does_not_mutate_inputs():
    rosters = {pid: set(t) for pid, t in ROSTERS.items()}
    validate_dropadd_batch([_req(100, 1, 1, 50)], rosters, PODS, 3)
    assert rosters == ROSTERS


def test_batch_empty_is_valid():
    assert validate_dropadd_batch([], ROSTERS, PODS, 3).is_valid


def test_find_shared_drops():
    pending = [_req(1, 1, 10, 50), _req(2, 1, 10, 51), _req(3, 1, 11, 52),
               _req(4, 2, 10, 53), _req(5, 1, None, 54)]
    assert find_shared_drops(pending) == {1, 2}
