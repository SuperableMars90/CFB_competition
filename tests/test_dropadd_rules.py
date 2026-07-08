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
