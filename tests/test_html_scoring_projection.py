"""Projected-score / spread plumbing in scripts/html_scoring.py (pure logic, no DB)."""

from scripts.html_scoring import _build_slot, _build_status_payload, _game_owners, _projection_totals


def _row(team_id=1, home_id=1, cfbd_game_id=100, db_status="scheduled", **kw):
    row = {
        "team_id": team_id, "team_name": "Tennessee", "slot_type": "conference",
        "conference_slug": "SEC", "conference_tier": "P4", "is_week0": 0,
        "cfbd_game_id": cfbd_game_id, "home_team_id": home_id,
        "home_name": "Tennessee", "away_name": "Vanderbilt",
        "db_status": db_status, "home_score": None, "away_score": None,
        "game_time": None,
    }
    row.update(kw)
    return row


def _live(status="in_progress", home=0, away=0, period=1, clock="15:00"):
    return {100: {"status": status, "home_points": home, "away_points": away,
                  "period": period, "clock": clock}}


# Tennessee at home, favored by 10 -> CFBD home_spread -10
LINES = {100: {"home_spread": -10.0, "home_team": "Tennessee", "away_team": "Vanderbilt"}}


def test_pregame_slot_projects_the_full_spread():
    slot = _build_slot(_row(), {}, LINES)
    assert slot["spread"] == 10
    assert slot["proj"] == 10


def test_underdog_slot_has_negative_spread_and_projection():
    slot = _build_slot(_row(team_id=2, home_id=1), {}, LINES)
    assert slot["spread"] == -10
    assert slot["proj"] == -10


def test_live_slot_adds_remaining_share_of_spread_to_margin():
    # up 15 at halftime as a 10-pt favorite: 15 + 10 * 30/60 = 20
    slot = _build_slot(_row(), _live(home=21, away=6, period=2, clock="00:00"), LINES)
    assert slot["margin"] == 15
    assert slot["proj"] == 20


def test_final_slot_projects_actual_margin():
    slot = _build_slot(_row(), _live(status="final", home=17, away=20, period=4, clock=None), LINES)
    assert slot["proj"] == -3


def test_slot_with_no_line_projects_current_margin():
    slot = _build_slot(_row(), _live(home=7, away=0, period=1, clock="10:00"), {})
    assert slot["spread"] is None
    assert slot["proj"] == 7


def test_pass_slot_projects_zero():
    slot = _build_slot(_row(team_id=None), {}, LINES)
    assert slot["proj"] == 0 and slot["spread"] is None


def test_projection_totals_sum_and_count_missing_lines():
    with_line = _build_slot(_row(), {}, LINES)                               # +10, has line
    no_line = _build_slot(_row(cfbd_game_id=101), {}, LINES)                  # no line, unstarted
    passed = _build_slot(_row(team_id=None), {}, LINES)
    final_no_line = _build_slot(_row(cfbd_game_id=102),
                                {102: {"status": "final", "home_points": 3, "away_points": 0,
                                       "period": 4, "clock": None}}, {})
    total, missing = _projection_totals([with_line, no_line, passed, final_no_line])
    assert total == 10 + 0 + 0 + 3
    assert missing == 1   # only the unfinished, unlined pick


def test_status_payload_carries_projection_and_live_games():
    slot = _build_slot(_row(), {}, LINES)
    summary = {
        "player_id": 1, "name": "Zach", "pod_id": 1, "pod_name": "Orange Pod",
        "week": 0, "proj": 10.0, "no_line": 0, "season": 0,
        "pod_rank": 1, "overall_rank": 1, "match_pts": None, "base_pts": None,
        "pod_bonus": None, "vs_pod_bonus": None, "overall_bonus": None,
        "counts": {"final_pass": 0, "in_progress": 0, "pending": 1, "pass_count": 0},
        "slots": [slot],
    }
    games = [{"id": 5, "division": "fcs", "home": "A", "away": "B"}]
    payload = _build_status_payload(3, "ts", [summary], [], {}, None, games)
    assert payload["players"][0]["proj"] == 10.0
    assert payload["players"][0]["noLine"] == 0
    assert payload["players"][0]["slots"][0]["spread"] == 10
    assert payload["liveGames"] == games
    assert _build_status_payload(3, "ts", [summary], [], {}, None)["liveGames"] == []


def test_game_owners_groups_players_by_game_side():
    def slot(gid, home, is_pass=False):
        return {"cfbd_game_id": gid, "is_home": home, "is_pass": is_pass}
    summaries = [
        {"name": "Jacob", "slots": [slot(1, True), slot(None, None, True)]},
        {"name": "Dan", "slots": [slot(1, True), slot(2, False)]},
    ]
    assert _game_owners(summaries) == {
        1: {"home": ["Jacob", "Dan"], "away": []},
        2: {"home": [], "away": ["Dan"]},
    }
