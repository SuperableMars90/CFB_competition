"""
lib/config.py
--------------
Small shared app-wide constants. Centralized here (rather than duplicated
per page, as before the st.navigation migration) since every page file
is now a normal, importable Python module.
"""

CURRENT_SEASON_ID = 8  # DEV SEAM — replace with a real current-season lookup later

DRAFT_PICKS_PER_PLAYER = 25  # single source of truth for draft length
