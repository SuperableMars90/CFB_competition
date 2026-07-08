"""
Unit tests for scripts.html_records's pure rendering and page-merge logic.
No DB, no network -- lib.records.get_records_table_rows()'s output shape
is faked directly.
"""

import pytest

from scripts.html_records import (
    render_records_table_html,
    merge_records_into_page,
    TABLE_BLOCK_START,
    TABLE_BLOCK_END,
)

ROW_POINTS = {
    'name': 'Best Week', 'description': 'Most net points in a week',
    'value_type': 'points', 'value': 554.0, 'player_name': 'Bob',
    'period': '2023 Week 1', 'context': None,
}
ROW_PERCENTAGE = {
    'name': 'Nailbiter Award', 'description': "Lowest percentage...",
    'value_type': 'percentage', 'value': 35.0, 'player_name': 'Bob',
    'period': '2021 Week 1', 'context': None,
}
ROW_WITH_CONTEXT = {
    'name': 'Biggest Win', 'description': 'Biggest win in a single game',
    'value_type': 'points', 'value': 74.0, 'player_name': 'Bob',
    'period': '2023 Week 1', 'context': 'Oregon',
}
ROW_LEAGUE_WIDE = {
    'name': 'Shootout', 'description': 'Most total points by all players in a week',
    'value_type': 'points', 'value': 1346.0, 'player_name': 'All',
    'period': '2021 Week 2', 'context': None,
}
ROW_EMPTY = {
    'name': 'Some New Award', 'description': 'Not yet set',
    'value_type': 'points', 'value': None, 'player_name': None,
    'period': None, 'context': None,
}


def test_renders_whole_number_value_without_decimal_point():
    html = render_records_table_html([ROW_POINTS])
    assert '<td>554</td>' in html
    assert '554.0' not in html


def test_renders_percentage_suffix():
    html = render_records_table_html([ROW_PERCENTAGE])
    assert '<td>35%</td>' in html


def test_renders_context_suffix_on_week_column():
    html = render_records_table_html([ROW_WITH_CONTEXT])
    assert '<td>2023 Week 1 (Oregon)</td>' in html


def test_renders_league_wide_winner_as_all():
    html = render_records_table_html([ROW_LEAGUE_WIDE])
    assert '<td>All</td>' in html


def test_renders_empty_row_as_blank_cells_not_crash():
    html = render_records_table_html([ROW_EMPTY])
    assert '<td>Some New Award</td>' in html
    assert '<td></td>' in html


def test_merge_replaces_only_the_table_block():
    existing = (
        "ROSTERS SECTION ABOVE"
        f'{TABLE_BLOCK_START} {{"hasFixedLayout":false}} -->\nOLD TABLE\n{TABLE_BLOCK_END}'
        "TRAILING CONTENT"
    )
    new_block = f'{TABLE_BLOCK_START} {{"hasFixedLayout":false}} -->\nNEW TABLE\n{TABLE_BLOCK_END}'

    result = merge_records_into_page(existing, new_block)

    assert result == f"ROSTERS SECTION ABOVE{new_block}TRAILING CONTENT"
    assert "OLD TABLE" not in result


def test_merge_raises_if_not_exactly_one_table_block():
    existing_zero = "no table block here at all"
    existing_two = (
        f'{TABLE_BLOCK_START} -->A{TABLE_BLOCK_END}'
        f'{TABLE_BLOCK_START} -->B{TABLE_BLOCK_END}'
    )
    new_block = f'{TABLE_BLOCK_START} -->NEW{TABLE_BLOCK_END}'

    with pytest.raises(ValueError):
        merge_records_into_page(existing_zero, new_block)
    with pytest.raises(ValueError):
        merge_records_into_page(existing_two, new_block)
