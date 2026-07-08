"""Unit tests for lib.db._current_week_from_ranges (T25) — pure logic, no DB."""

from datetime import date

from lib.db import _current_week_from_ranges


def _ranges(*weeks):
    """Build a {week: {start_date, end_date}} map from (week, start, end) tuples."""
    return {w: {"start_date": start, "end_date": end} for w, start, end in weeks}


SEASON = _ranges(
    (0, date(2026, 8, 29), date(2026, 8, 30)),
    (1, date(2026, 9, 3), date(2026, 9, 7)),
    (2, date(2026, 9, 11), date(2026, 9, 13)),
    (3, date(2026, 9, 17), date(2026, 9, 20)),
)


def test_today_within_a_weeks_range():
    assert _current_week_from_ranges(SEASON, date(2026, 9, 5)) == 1


def test_today_in_the_gap_between_weeks_advances_to_the_next_week():
    # Week 1 ended 9/7, week 2 doesn't start until 9/11 — the gap should
    # already show week 2 (that's what a player should be picking for).
    assert _current_week_from_ranges(SEASON, date(2026, 9, 9)) == 2


def test_before_the_season_starts_shows_week_1():
    assert _current_week_from_ranges(SEASON, date(2026, 8, 1)) == 1


def test_after_the_season_ends_returns_none():
    assert _current_week_from_ranges(SEASON, date(2026, 12, 1)) is None


def test_week_0_is_never_returned_even_if_todays_date_falls_in_it():
    assert _current_week_from_ranges(SEASON, date(2026, 8, 29)) == 1


def test_weeks_with_no_known_dates_are_skipped():
    ranges = _ranges(
        (1, date(2026, 9, 3), date(2026, 9, 7)),
        (2, None, None),  # e.g. schedule not loaded yet for this week
        (3, date(2026, 9, 17), date(2026, 9, 20)),
    )
    # Gap after week 1 ends, before week 3 — week 2 has no dates so it's
    # skipped and week 3 is the next real candidate.
    assert _current_week_from_ranges(ranges, date(2026, 9, 9)) == 3


def test_empty_ranges_returns_none():
    assert _current_week_from_ranges({}, date(2026, 9, 5)) is None


def test_exact_boundary_dates_are_inclusive():
    assert _current_week_from_ranges(SEASON, date(2026, 9, 3)) == 1  # start
    assert _current_week_from_ranges(SEASON, date(2026, 9, 7)) == 1  # end
