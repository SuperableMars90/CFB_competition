"""
lib/config.py
--------------
Small shared app-wide constants. Centralized here (rather than duplicated
per page, as before the st.navigation migration) since every page file
is now a normal, importable Python module.
"""

CURRENT_SEASON_ID = 68  # DEV SEAM — replace with a real current-season lookup later

# Draft length / roster size moved to seasons.draft_picks_per_player (2026-08) --
# see lib.db.get_draft_picks_per_player() -- so pod size is season-init-time
# config, not a code constant. Removed from here rather than left as a stale,
# unused fallback.
