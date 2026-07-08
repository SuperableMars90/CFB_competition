"""
scripts/html_rosters.py
------------------------
Generates the "Unclaimed" (free agent) + per-player roster listing for a
season, matching the format of the live "Rosters and Records" WordPress
page (id 100, /standings-rosters-and-records/) — one section per pod
(pods are completely severed rosters, Zach 2026-07-05), each with its
own "Unclaimed" section grouped by conference, then one section per
player in that pod, also grouped by conference.

Two entry points:
  - write_rosters_page(season_id) — local file only
    (output/web/rosters_season_N.html), unaffected by the WordPress push
    below. Kept for quick local inspection.
  - push_rosters_page(season_id) — the real thing: pushes the rosters
    section live to WordPress, preserving the hand-maintained "Records
    and Awards" table below it via an HTML comment marker. Triggered on
    every approved drop/add — see lib.db.decide_dropadd_request().

Marker strategy: the live page has no marker yet (this is the first
automation to touch it). merge_rosters_into_page() handles both cases —
subsequent runs replace only the marker-bounded section; the very first
run anchors on the existing "<h1>Records and Awards</h1>" heading and
cuts there instead, since nothing before that heading on the live page
today is hand-maintained (it's last season's stale roster dump).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.db import get_active_roster_teams, get_available_teams, get_pods, get_pod_players

OUTPUT_DIR = Path(__file__).parent.parent / 'output' / 'web'

# Mirrors lib.links.WORDPRESS_ROSTERS_RECORDS_URL's path — this is the
# only place a WordPress *slug* (as opposed to a public URL) is needed,
# so it isn't centralized there too.
ROSTERS_RECORDS_SLUG = 'standings-rosters-and-records'

ROSTERS_MARKER_START = '<!-- ROSTERS:START -->'
ROSTERS_MARKER_END = '<!-- ROSTERS:END -->'
RECORDS_HEADING = '<h1>Records and Awards</h1>'


def _render_conference_groups(teams: list[dict]) -> str:
    """
    Group teams by conference_abbreviation and render as
    <b>Conf</b><ul><li>Team</li>...</ul> blocks — matches the live
    page's existing format (modulo a stray missing '>' on every <b> tag
    in the live page's current content, a pre-existing typo this does
    not reproduce).
    """
    by_conf: dict[str, list[str]] = {}
    for t in teams:
        by_conf.setdefault(t['conference_abbreviation'], []).append(t['name'])

    parts = []
    for conf in sorted(by_conf):
        items = ''.join(f'<li>{name}</li>' for name in sorted(by_conf[conf]))
        parts.append(f'<b>{conf}</b>\n<ul>\n{items}\n</ul>')
    return '\n'.join(parts)


def render_rosters_fragment(season_id: int) -> str:
    """
    Build the free-agent + per-player roster listing for this season,
    grouped by pod: <h2>{Pod Name}</h2> containing an <h3>Unclaimed</h3>
    and one <h3>{player}</h3> per player, each with their own
    <b>/<ul> conference groups.
    """
    sections = []

    for pod in get_pods(season_id):
        sections.append(f"<h2>{pod['name']}</h2>")

        sections.append("<h3>Unclaimed</h3>")
        sections.append(_render_conference_groups(get_available_teams(season_id, pod['id'])))

        for player in get_pod_players(pod['id']):
            roster = get_active_roster_teams(player['id'], season_id)
            sections.append(f"<h3>{player['name']}</h3>")
            sections.append(_render_conference_groups(roster))

    return '\n\n'.join(sections)


def write_rosters_page(season_id: int) -> Path:
    """Render and write the rosters fragment to output/web/rosters_season_N.html (local only)."""
    fragment = render_rosters_fragment(season_id)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CFB Fantasy &mdash; Rosters</title>
</head>
<body>
<h1>Current Rosters</h1>
{fragment}
</body>
</html>"""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f'rosters_season_{season_id}.html'
    path.write_text(html, encoding='utf-8')
    return path


def _build_managed_rosters_html(season_id: int) -> str:
    """The full marker-wrapped chunk this automation owns end to end —
    title, refresh timestamp (server-local time), and every pod's
    sections. Regenerated from scratch on every push, so nothing here
    is ever hand-edited on the WordPress side."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    fragment = render_rosters_fragment(season_id)
    return (
        f"{ROSTERS_MARKER_START}\n"
        f"<h1>Current Rosters</h1>\n"
        f"<p>Rosters current as of {now}</p>\n"
        f"{fragment}\n"
        f"{ROSTERS_MARKER_END}"
    )


def merge_rosters_into_page(existing_content: str, managed_html: str) -> str:
    """
    Pure string surgery. Replaces only the marker-bounded rosters
    section with managed_html, leaving everything else — critically,
    the hand-maintained Records and Awards Gutenberg table — byte-for-
    byte untouched.

    First run (no markers on the live page yet): anchors on the
    existing "<h1>Records and Awards</h1>" heading instead, replacing
    everything before it. Raises ValueError if neither the markers nor
    that heading can be found, rather than guessing where to cut.
    """
    if ROSTERS_MARKER_START in existing_content and ROSTERS_MARKER_END in existing_content:
        before = existing_content.split(ROSTERS_MARKER_START, 1)[0]
        after = existing_content.split(ROSTERS_MARKER_END, 1)[1]
        return f"{before}{managed_html}{after}"

    if RECORDS_HEADING in existing_content:
        after = existing_content.split(RECORDS_HEADING, 1)[1]
        return f"{managed_html}\n{RECORDS_HEADING}{after}"

    raise ValueError(
        "Can't find either the ROSTERS markers or '<h1>Records and Awards</h1>' "
        "on the live page -- refusing to guess where the rosters section ends."
    )


def push_rosters_page(season_id: int) -> None:
    """
    Push the rosters section live to WordPress (page id 100,
    /standings-rosters-and-records/), preserving the hand-maintained
    Records and Awards table below it. Triggered on every approved
    drop/add — see lib.db.decide_dropadd_request().
    """
    from scripts.wordpress_client import WordPressClient, WordPressError

    client = WordPressClient()
    page = client.get_by_slug('pages', ROSTERS_RECORDS_SLUG)
    if page is None:
        raise WordPressError(f"WordPress page with slug '{ROSTERS_RECORDS_SLUG}' not found.")

    existing_content = page['content']['raw']
    managed_html = _build_managed_rosters_html(season_id)
    new_content = merge_rosters_into_page(existing_content, managed_html)

    client.update('pages', page['id'], new_content)
