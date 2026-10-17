from datetime import datetime, timezone

import pytest

from lib.projection import (
    average_spread,
    build_live_games,
    expected_margin,
    format_game_clock,
    game_started,
    minutes_remaining,
    parse_clock_minutes,
    projected_margin,
)


# ---- expected margin / spread sign ----------------------------------------

def test_home_favorite_is_positive_expected_margin():
    # CFBD: negative spread = home favored
    assert expected_margin(-10, is_home=True) == 10
    assert expected_margin(-10, is_home=False) == -10


def test_away_favorite_is_positive_expected_margin():
    assert expected_margin(20, is_home=False) == 20
    assert expected_margin(20, is_home=True) == -20


def test_pickem_is_zero_not_negative_zero():
    assert str(expected_margin(0, is_home=True)) == "0.0"


def test_no_line_is_none():
    assert expected_margin(None, is_home=True) is None


def test_average_spread_across_providers():
    lines = [{"spread": -3}, {"spread": -3.5}, {"spread": -4}]
    assert average_spread(lines) == -3.5


def test_average_spread_ignores_null_and_handles_empty():
    assert average_spread([{"spread": None}, {"spread": -7}]) == -7
    assert average_spread([{"spread": None}]) is None
    assert average_spread([]) is None


# ---- clock parsing / minutes remaining ------------------------------------

def test_parse_clock():
    assert parse_clock_minutes("07:30") == 7.5
    assert parse_clock_minutes("00:00") == 0
    assert parse_clock_minutes(None) is None
    assert parse_clock_minutes("") is None
    assert parse_clock_minutes("garbage") is None


def test_minutes_remaining_by_status():
    assert minutes_remaining("scheduled", None, None) == 60
    assert minutes_remaining(None, None, None) == 60
    assert minutes_remaining("final", 4, "00:00") == 0


def test_minutes_remaining_in_progress():
    assert minutes_remaining("in_progress", 1, "15:00") == 60
    assert minutes_remaining("in_progress", 2, "00:00") == 30   # halftime
    assert minutes_remaining("in_progress", 3, "07:30") == 22.5
    assert minutes_remaining("in_progress", 4, "01:27") == pytest.approx(1.45)


def test_minutes_remaining_halftime_blank_clock_is_thirty():
    assert minutes_remaining("in_progress", 2, None) == 30


def test_minutes_remaining_blank_clock_other_periods_is_period_start():
    assert minutes_remaining("in_progress", 3, None) == 30
    assert minutes_remaining("in_progress", 1, None) == 60


def test_minutes_remaining_overtime_is_zero():
    assert minutes_remaining("in_progress", 5, "10:00") == 0
    assert minutes_remaining("in_progress", 7, None) == 0


def test_minutes_remaining_in_progress_without_period_treated_as_unstarted():
    assert minutes_remaining("in_progress", None, None) == 60


# ---- projection (the worked examples from the spec) -----------------------

def test_pregame_projection_is_the_spread():
    assert projected_margin(None, 30, 60) == 30


def test_thirty_point_favorite_up_15_at_half_projects_30():
    assert projected_margin(15, 30, minutes_remaining("in_progress", 2, "00:00")) == 30


def test_thirty_point_favorite_tied_at_half_projects_15():
    assert projected_margin(0, 30, 30) == 15


def test_thirty_point_favorite_up_40_at_half_projects_55():
    assert projected_margin(40, 30, 30) == 55


def test_underdog_projection_is_negative_spread_share():
    assert projected_margin(0, -30, 30) == -15


def test_final_game_projects_actual_margin():
    assert projected_margin(17, 30, minutes_remaining("final", 4, "00:00")) == 17
    assert projected_margin(-6, -3, 0) == -6


def test_overtime_projects_current_margin():
    assert projected_margin(0, 7, minutes_remaining("in_progress", 5, "15:00")) == 0


def test_missing_line_falls_back_to_current_margin():
    assert projected_margin(12, None, 30) == 12
    assert projected_margin(None, None, 60) == 0


# ---- kickoff detection -----------------------------------------------------

NOW = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


def test_game_started_trusts_scoreboard_status():
    assert game_started("in_progress", None, NOW)
    assert game_started("final", None, NOW)
    # status says scheduled even though the start time has passed: not started
    assert not game_started("scheduled", "2026-09-19T19:00:00.000Z", NOW)


def test_game_started_falls_back_to_start_time():
    assert game_started(None, "2026-09-19T19:00:00.000Z", NOW)
    assert not game_started(None, "2026-09-19T21:00:00.000Z", NOW)
    assert not game_started(None, None, NOW)
    assert not game_started(None, "not a date", NOW)


# ---- game clock label ------------------------------------------------------

def test_format_game_clock():
    assert format_game_clock(3, "04:12") == "Q3 4:12"
    assert format_game_clock(4, "00:05") == "Q4 0:05"
    assert format_game_clock(2, "00:00") == "Half"
    assert format_game_clock(2, None) == "Half"
    assert format_game_clock(1, None) == "Q1"
    assert format_game_clock(5, "10:00") == "OT"
    assert format_game_clock(6, None) == "2OT"
    assert format_game_clock(None, None) == "Live"


# ---- live games list -------------------------------------------------------

def _game(status="in_progress", division="fbs", start="2026-09-19T20:00:00.000Z", **kw):
    g = {
        "status": status, "division": division, "start_date": start,
        "home_points": 10, "away_points": 7, "period": 2, "clock": "05:00",
        "home_name": "Home Mascots", "away_name": "Away Mascots", "spread": None,
    }
    g.update(kw)
    return g


def test_live_games_only_includes_in_progress():
    live = {1: _game(), 2: _game(status="final"), 3: _game(status="scheduled")}
    assert [g["id"] for g in build_live_games(live, {})] == [1]


def test_live_games_prefers_stored_line_and_short_names():
    live = {1: _game()}
    lines = {1: {"home_spread": -10, "home_team": "Tennessee", "away_team": "Vanderbilt"}}
    (g,) = build_live_games(live, lines)
    assert (g["home"], g["away"]) == ("Tennessee", "Vanderbilt")
    assert (g["favorite"], g["favLine"]) == ("Tennessee", -10)
    assert g["time"] == "Q2 5:00"
    assert (g["homePts"], g["awayPts"]) == (10, 7)


def test_live_games_falls_back_to_scoreboard_spread_and_names():
    live = {1: _game(spread=3.5)}
    (g,) = build_live_games(live, {})
    assert (g["home"], g["away"]) == ("Home Mascots", "Away Mascots")
    assert (g["favorite"], g["favLine"]) == ("Away Mascots", -3.5)


def test_live_games_no_line_anywhere_gives_none_spreads():
    (g,) = build_live_games({1: _game()}, {})
    assert g["favorite"] is None and g["favLine"] is None


def test_live_games_pickem_has_no_favorite():
    (g,) = build_live_games({1: _game()}, {1: {"home_spread": 0, "home_team": "A", "away_team": "B"}})
    assert g["favorite"] is None and g["favLine"] == 0


def test_live_games_attach_owners_per_side():
    owners = {1: {"home": ["Jacob", "Dan"], "away": []}}
    (g,) = build_live_games({1: _game()}, {}, owners)
    assert g["homePlayers"] == ["Jacob", "Dan"] and g["awayPlayers"] == []
    (g,) = build_live_games({1: _game()}, {})
    assert g["homePlayers"] == [] and g["awayPlayers"] == []


def test_live_games_keep_division_and_stable_kickoff_order():
    live = {
        1: _game(start="2026-09-19T22:00:00.000Z", home_name="B"),
        2: _game(start="2026-09-19T20:00:00.000Z", home_name="A", division="fcs"),
    }
    games = build_live_games(live, {})
    assert [g["id"] for g in games] == [2, 1]
    assert games[0]["division"] == "fcs"
    assert "_start" not in games[0]
