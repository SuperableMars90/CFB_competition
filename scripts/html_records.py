"""
scripts/html_records.py
-------------------------
Renders the "Records and Awards" table (lib.records.get_records_table_rows())
and pushes it live to WordPress (page id 100,
/standings-rosters-and-records/), replacing only the existing Gutenberg
table block -- everything else on the page (including the rosters
section scripts/html_rosters.py owns) is left byte-for-byte untouched.

Regenerates the whole table from current DB state every time, rather
than trying to patch individual rows in place -- simpler and can't
drift, since lib.records.get_records_table_rows() already resolves
"current best per record_type" the same way record-detection does.

Triggered weekly (Zach, 2026-07-05: "Records should be checked for and
posted weekly") -- see scripts/scoring_engine.py's finalize_week().

Public entry point: push_records_table()
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.records import get_records_table_rows

ROSTERS_RECORDS_SLUG = 'standings-rosters-and-records'

TABLE_BLOCK_START = '<!-- wp:table'
TABLE_BLOCK_END = '<!-- /wp:table -->'


def _format_value(row: dict) -> str:
    if row['value'] is None:
        return ''
    # Every seeded value is a whole number (554, -51, 35%, ...) -- int()
    # strips the DB's DECIMAL(10,2) trailing ".00" to match the live
    # page's existing style.
    formatted = str(int(row['value']))
    return f"{formatted}%" if row['value_type'] == 'percentage' else formatted


def _format_week(row: dict) -> str:
    if row['period'] is None:
        return ''
    return f"{row['period']} ({row['context']})" if row['context'] else row['period']


def render_records_table_html(rows: list[dict]) -> str:
    """The <figure class="wp-block-table">...</figure> markup, same shape as the live page's existing table."""
    header = (
        "<tr><td><strong>Award</strong></td><td><strong>Winner</strong></td>"
        "<td><strong>Week</strong></td><td><strong>Value</strong></td>"
        "<td><strong>Description</strong></td></tr>"
    )
    body = []
    for row in rows:
        body.append(
            f"<tr><td>{row['name']}</td><td>{row['player_name'] or ''}</td>"
            f"<td>{_format_week(row)}</td><td>{_format_value(row)}</td>"
            f"<td>{row['description']}</td></tr>"
        )
    return (
        f'<figure class="wp-block-table"><table><thead>{header}</thead>'
        f'<tbody>{"".join(body)}</tbody></table></figure>'
    )


def _build_records_block(rows: list[dict]) -> str:
    table_html = render_records_table_html(rows)
    return (
        f'{TABLE_BLOCK_START} {{"hasFixedLayout":false}} -->\n'
        f'{table_html}\n'
        f'{TABLE_BLOCK_END}'
    )


def merge_records_into_page(existing_content: str, new_block: str) -> str:
    """
    Pure string surgery. Replaces the single existing
    <!-- wp:table -->...<!-- /wp:table --> block with new_block, leaving
    everything else (critically, the rosters section above it) untouched.

    Raises ValueError if the page doesn't have exactly one such block --
    refuses to guess which one is the records table rather than risking
    silently touching the wrong content.
    """
    start_count = existing_content.count(TABLE_BLOCK_START)
    end_count = existing_content.count(TABLE_BLOCK_END)
    if start_count != 1 or end_count != 1:
        raise ValueError(
            f"Expected exactly one wp:table block on the page, found "
            f"{start_count} start marker(s) and {end_count} end marker(s) -- "
            "refusing to guess which one is the records table."
        )

    before, rest = existing_content.split(TABLE_BLOCK_START, 1)
    _, after = rest.split(TABLE_BLOCK_END, 1)
    return f"{before}{new_block}{after}"


def push_records_table() -> None:
    """Push the current Records and Awards table live to WordPress."""
    from scripts.wordpress_client import WordPressClient, WordPressError

    client = WordPressClient()
    page = client.get_by_slug('pages', ROSTERS_RECORDS_SLUG)
    if page is None:
        raise WordPressError(f"WordPress page with slug '{ROSTERS_RECORDS_SLUG}' not found.")

    existing_content = page['content']['raw']
    new_block = _build_records_block(get_records_table_rows())
    new_content = merge_records_into_page(existing_content, new_block)

    client.update('pages', page['id'], new_content)
