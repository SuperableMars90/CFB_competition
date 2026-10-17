"""
scripts/import_commissioner_notes.py
-------------------------------------
Import a week's commissioner notes (Markdown) into that week's recap page.
Format spec: docs/commissioner_notes_format.md.

    python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md
        # renders the notes, writes output/web/week_3_commissioner_notes.html,
        # and splices them into output/web/week_3_final.html. Local only.

    python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md --check
        # render + validate (images exist, not blank, no stray [CLAUDE:] directives), write nothing

    python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md --push
        # also splices into the LIVE WordPress recap post (outward-facing)

From a notebook / another script:

    from scripts.import_commissioner_notes import run
    result = run("scratch/week_3_commissioner_notes.md")            # local
    result = run("scratch/week_3_commissioner_notes.md", push=True)  # + WordPress
    result["html"], result["warnings"]

Idempotent: re-running replaces the previous notes, so edit the .md and run again.
Note that a later `scoring_engine.py --finalize-only` rewrites the local final page
but preserves whatever notes are LIVE on WordPress -- so push if you want them to stick.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.commissioner_notes import (
    NotesError,
    embed_resolver,
    find_image,
    image_is_blank,
    inject_summary,
    render_notes,
    upload_resolver,
    week_from_filename,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / 'output' / 'web'
# Where images are looked for after the notes file's own folder.
DEFAULT_IMAGE_DIRS = [REPO_ROOT / 'output' / 'scoring', REPO_ROOT / 'output']


def _season_year() -> int:
    from lib.config import CURRENT_SEASON_ID
    from lib.db import get_connection
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT year FROM seasons WHERE id = %s", (CURRENT_SEASON_ID,))
        row = cur.fetchone()
        cur.close()
    if row is None:
        raise NotesError(f"season_id={CURRENT_SEASON_ID} not found in DB")
    return row[0]


def run(
    notes_path,
    week: Optional[int] = None,
    *,
    images: str = 'embed',
    image_dirs=(),
    page=None,
    push: bool = False,
    dry_run: bool = False,
    year: Optional[int] = None,
) -> dict:
    """
    Render the notes and splice them into the recap. Returns
    {week, html, images, warnings, fragment_path, page_path, pushed}.

    images: 'embed' (base64 inline, default) or 'upload' (WordPress media library; requires push=True).
    page:   the local recap file to update; default output/web/week_N_final.html.
    dry_run: validate and render only; write and push nothing.
    """
    notes_path = Path(notes_path)
    if not notes_path.is_file():
        raise NotesError(f"Notes file not found: {notes_path}")
    week = week if week is not None else week_from_filename(notes_path)
    if images not in ('embed', 'upload'):
        raise NotesError("images must be 'embed' or 'upload'")
    if images == 'upload' and not push and not dry_run:
        raise NotesError("images='upload' needs push=True (the uploaded URLs only make sense on the live post).")

    search_dirs = [*map(Path, image_dirs), notes_path.parent, *DEFAULT_IMAGE_DIRS]
    warnings: list[str] = []
    client = None

    if dry_run:
        def resolver(ref: str) -> str:   # validate only
            path = find_image(ref, search_dirs)
            if image_is_blank(path):
                warnings.append(f"{path} is a single flat colour (blank) -- the figure was probably never drawn before saving.")
            return ref
    elif images == 'upload':
        from scripts.wordpress_client import WordPressClient
        client = WordPressClient()
        resolver = upload_resolver(search_dirs, client, warnings)
    else:
        resolver = embed_resolver(search_dirs, warnings)

    html, refs = render_notes(notes_path.read_text(encoding='utf-8'), resolver)
    result = {'week': week, 'html': html, 'images': refs, 'warnings': warnings,
              'fragment_path': None, 'page_path': None, 'pushed': False}
    if dry_run:
        return result

    WEB_DIR.mkdir(parents=True, exist_ok=True)
    fragment_path = WEB_DIR / f'week_{week}_commissioner_notes.html'
    fragment_path.write_text(html, encoding='utf-8')
    result['fragment_path'] = fragment_path

    page_path = Path(page) if page else WEB_DIR / f'week_{week}_final.html'
    if page_path.is_file():
        page_path.write_text(inject_summary(page_path.read_text(encoding='utf-8'), html), encoding='utf-8')
        result['page_path'] = page_path
    else:
        warnings.append(f"{page_path} doesn't exist yet -- run `scoring_engine.py --week {week} --finalize-only` first. Fragment still written.")

    if push:
        from scripts.wordpress_client import WordPressClient
        client = client or WordPressClient()
        slug = f"{year or _season_year()}-week-{week}-recap"
        post = client.get_by_slug('posts', slug)
        if post is None:
            raise NotesError(f"No WordPress post at slug '{slug}'. Run `scoring_engine.py --week {week} --finalize-only` to create it first.")
        client.update('posts', post['id'], inject_summary(post['content']['raw'], html))
        result['pushed'] = True

    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Import commissioner notes (Markdown) into a week's recap page.")
    ap.add_argument('notes', help="Path to the notes file, e.g. scratch/week_3_commissioner_notes.md")
    ap.add_argument('--week', type=int, help="Week number (default: inferred from the filename)")
    ap.add_argument('--page', help="Local recap file to update (default: output/web/week_N_final.html)")
    ap.add_argument('--images', choices=['embed', 'upload'], default='embed',
                    help="embed = inline base64 (default); upload = WordPress media library (needs --push)")
    ap.add_argument('--image-dir', action='append', default=[], help="Extra folder to look for images in (repeatable)")
    ap.add_argument('--check', action='store_true', help="Validate and render only; write and push nothing")
    ap.add_argument('--push', action='store_true', help="Also update the live WordPress recap post")
    ap.add_argument('--year', type=int, help="Season year for the WordPress slug (default: looked up from the DB)")
    args = ap.parse_args()

    try:
        r = run(args.notes, args.week, images=args.images, image_dirs=args.image_dir,
                page=args.page, push=args.push, dry_run=args.check, year=args.year)
    except NotesError as e:
        sys.exit(f"[ERROR] {e}")

    for w in r['warnings']:
        print(f"[WARNING] {w}")
    print(f"Week {r['week']}: rendered {len(r['html']):,} chars of HTML, {len(r['images'])} image(s): {', '.join(r['images']) or 'none'}")
    if args.check:
        print("--check: nothing written.")
        return
    print(f"Fragment: {r['fragment_path']}")
    if r['page_path']:
        print(f"Updated:  {r['page_path']}")
    print("Pushed to WordPress." if r['pushed'] else "Not pushed (local only). Add --push to update the live recap post.")


if __name__ == '__main__':
    main()
