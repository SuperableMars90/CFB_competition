"""
Unit tests for scripts.html_scoring.merge_recap_summary -- pure string
surgery, no DB/network. Mirrors the existing rosters/records marker-merge
tests (tests/test_html_rosters.py, tests/test_html_records.py).
"""

from scripts.html_scoring import (
    merge_recap_summary,
    SUMMARY_MARKER_START,
    SUMMARY_MARKER_END,
    _DEFAULT_SUMMARY_BLOCK,
)

NEW_CONTENT = f"REST OF PAGE ABOVE{_DEFAULT_SUMMARY_BLOCK}REST OF PAGE BELOW"


def test_first_push_keeps_the_fresh_empty_block():
    result = merge_recap_summary(None, NEW_CONTENT)
    assert result == NEW_CONTENT


def test_existing_content_with_no_markers_keeps_fresh_block():
    """An existing post that somehow has no markers (shouldn't happen in
    practice) falls back to the fresh block rather than erroring."""
    existing = "some old content with no summary markers at all"
    result = merge_recap_summary(existing, NEW_CONTENT)
    assert result == NEW_CONTENT


def test_preserves_a_written_summary_across_a_repush():
    written_block = f"{SUMMARY_MARKER_START}<p>What a week -- Bob's comeback was wild.</p>{SUMMARY_MARKER_END}"
    existing = f"OLD PAGE ABOVE{written_block}OLD PAGE BELOW"

    result = merge_recap_summary(existing, NEW_CONTENT)

    assert "Bob's comeback was wild" in result
    assert "REST OF PAGE ABOVE" in result
    assert "REST OF PAGE BELOW" in result
    # everything outside the summary markers comes from the NEW push, not the old page
    assert "OLD PAGE ABOVE" not in result
    assert "OLD PAGE BELOW" not in result
