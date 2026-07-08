"""
CFB Game — Player App
Entry point / router. Deliberately thin: page config, the auth gate (so
the nav sidebar itself doesn't render until logged in), then dispatch to
whichever page was selected. Page content lives in pages/*.py; the page
registry (titles, icons, order) lives in lib/nav.py.
"""

import streamlit as st

from lib.auth import require_login
from lib.nav import visible_pages

st.set_page_config(
    page_title="CFB Game",
    page_icon="🏈",
    layout="wide",
)

player = require_login()

pg = st.navigation(visible_pages(player))
pg.run()
