"""
scripts/render_debug_page.py
----------------------------
Render the live weekly status page to a LOCAL html file from the current live
CFBD scoreboard, for eyeballing changes without touching anything shared.

Strictly read-only against everything but the one local output file:
  * CFBD: GET requests only (scoreboard).
  * Aiven: SELECTs only (lineups, standings, stored game_lines). It does NOT
    run refresh_game_lines(), so no lines are captured or locked.
  * WordPress: never contacted.
  * Files: writes only the --out path (default output/web/debug_week_N_live.html),
    never the scoring engine's own output/web/week_N_live.html.

Usage:
    python scripts/render_debug_page.py --week 3
    python scripts/render_debug_page.py --week 3 --out /tmp/preview.html
    python scripts/render_debug_page.py --week 3 --no-live   # DB statuses only, no CFBD call
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.config import CURRENT_SEASON_ID
from scripts.html_scoring import OUTPUT_DIR, _wrap_doc, render_fragment


def render_debug_page(season_id: int, week: int, out: Path, live: bool = True) -> Path:
    if live:
        # Imported here so --no-live works without CFBD credentials.
        from scripts.cfbd_client import CFBDClient
        from scripts.scoring_engine import fetch_live_scores
        live_scores = fetch_live_scores(CFBDClient())
    else:
        live_scores = {}

    fragment = render_fragment(season_id, week, live_scores)
    html = _wrap_doc(f'DEBUG &mdash; CFB Fantasy Week {week} (local render)', fragment)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding='utf-8')
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Render the live status page locally (read-only).')
    parser.add_argument('--season', type=int, default=CURRENT_SEASON_ID,
                        help=f'season_id in DB (default: {CURRENT_SEASON_ID}, the live season)')
    parser.add_argument('--week', type=int, required=True, help='Week number to render')
    parser.add_argument('--out', type=Path, default=None,
                        help='Output file (default: output/web/debug_week_N_live.html)')
    parser.add_argument('--no-live', action='store_true',
                        help='Skip the CFBD scoreboard; show DB statuses only')
    args = parser.parse_args()

    out = args.out or (OUTPUT_DIR / f'debug_week_{args.week}_live.html')
    path = render_debug_page(args.season, args.week, out, live=not args.no_live)
    print(f"Wrote {path.resolve()}")
    print(f"Open it with: xdg-open {path.resolve()}   (or file://{path.resolve()})")
