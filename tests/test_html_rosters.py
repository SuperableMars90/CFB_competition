"""
Unit tests for scripts.html_rosters's pure page-merge logic.

merge_rosters_into_page() is the piece that matters most to get right --
it's what stands between an automated push and silently clobbering the
hand-maintained "Records and Awards" table on the live WordPress page.
Pure string surgery, no DB, no network.
"""

import pytest

from scripts.html_rosters import (
    merge_rosters_into_page,
    ROSTERS_MARKER_START,
    ROSTERS_MARKER_END,
    RECORDS_HEADING,
)

MANAGED_HTML = f"{ROSTERS_MARKER_START}\n<h1>Current Rosters</h1>\nNEW CONTENT\n{ROSTERS_MARKER_END}"


def test_first_run_cuts_before_records_heading_when_no_markers_exist():
    existing = (
        "<h1> Current Rosters</h1>\nSTALE OLD CONTENT\n"
        f"{RECORDS_HEADING}\n"
        "<!-- wp:table -->HAND MAINTAINED TABLE<!-- /wp:table -->"
    )

    result = merge_rosters_into_page(existing, MANAGED_HTML)

    assert result == (
        f"{MANAGED_HTML}\n{RECORDS_HEADING}\n"
        "<!-- wp:table -->HAND MAINTAINED TABLE<!-- /wp:table -->"
    )
    assert "STALE OLD CONTENT" not in result
    assert "HAND MAINTAINED TABLE" in result


def test_subsequent_run_replaces_only_the_marked_section():
    existing = (
        "PREAMBLE (shouldn't exist in practice, but must survive if it does)"
        f"{ROSTERS_MARKER_START}\nOLD ROSTERS\n{ROSTERS_MARKER_END}"
        f"{RECORDS_HEADING}HAND MAINTAINED TABLE"
    )

    result = merge_rosters_into_page(existing, MANAGED_HTML)

    assert result == (
        "PREAMBLE (shouldn't exist in practice, but must survive if it does)"
        f"{MANAGED_HTML}{RECORDS_HEADING}HAND MAINTAINED TABLE"
    )
    assert "OLD ROSTERS" not in result
    assert "HAND MAINTAINED TABLE" in result


def test_raises_when_neither_anchor_is_found():
    existing = "<h1>Some Unrelated Page</h1>No markers, no records heading here."

    with pytest.raises(ValueError):
        merge_rosters_into_page(existing, MANAGED_HTML)
