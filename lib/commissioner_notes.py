"""
lib/commissioner_notes.py
-------------------------
Turns a weekly commissioner-notes Markdown file into the HTML that goes
between the recap page's SUMMARY markers ("Commissioner's Notes"), and
splices it in. See docs/commissioner_notes_format.md for the file format.

Core is pure text-in / text-out (normalize_markdown, render_notes,
inject_summary). The only I/O is in the image helpers at the bottom (read a
PNG off disk, optionally upload it) -- render_notes() itself takes the image
step as an injected `resolve_image` callable, so it's testable with none.

Plain HTML only in the output -- NO `<!-- wp:... -->` block delimiters. The
recap lives inside one `<!-- wp:html -->` block and a nested delimiter
orphans everything above it (see the note on _DEFAULT_SUMMARY_BLOCK in
scripts/html_scoring.py). inject_summary() strips any that sneak in.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Callable, Iterable, Optional

import markdown

# Must match scripts/html_scoring.SUMMARY_MARKER_START/END (a test enforces it).
SUMMARY_MARKER_START = '<!-- SUMMARY:START -->'
SUMMARY_MARKER_END = '<!-- SUMMARY:END -->'

DEFAULT_HEADING = "Commissioner's Notes"

_IMAGE_EXT = r'(?:png|jpe?g|gif|webp|svg)'

# "[CLAUDE: insert week_3_luck.png here]" -- the trailing "here" is optional.
_PLACEHOLDER_RE = re.compile(
    rf'\[CLAUDE:\s*(?:insert\s+)?(?P<ref>[^\]\s]+?\.{_IMAGE_EXT})(?:\s+here)?\s*\]',
    re.IGNORECASE,
)
_ANY_DIRECTIVE_RE = re.compile(r'\[CLAUDE:[^\]]*\]', re.IGNORECASE)

_LIST_LINE_RE = re.compile(r'^\s*(?:[*+-]|\d+[.)])\s+')
_LOOSE_HEADING_RE = re.compile(r'^ {1,3}#{1,6}\s')
# A bare URL that isn't already the target of a link/image or an <autolink>.
_BARE_URL_RE = re.compile(r'(?<![(<"\'\[=])\bhttps?://[^\s<>()\[\]]+')
_IMG_TAG_RE = re.compile(r'<img\b[^>]*?\bsrc="(?P<src>[^"]*)"[^>]*?/?>')
_ALT_RE = re.compile(r'\balt="(?P<alt>[^"]*)"')
_WP_DELIM_RE = re.compile(r'<!--\s*/?wp:[^>]*-->')
_WEEK_RE = re.compile(r'week[_\- ]?(\d+)', re.IGNORECASE)


class NotesError(ValueError):
    """The notes file can't be turned into a valid summary block."""


# ------------------------------------------------------------------
# Markdown -> HTML (pure)
# ------------------------------------------------------------------

def week_from_filename(path) -> int:
    """`week_3_commissioner_notes.md` -> 3. NotesError if there's no week number."""
    m = _WEEK_RE.search(Path(path).name)
    if not m:
        raise NotesError(
            f"Can't infer the week from '{Path(path).name}' -- name it week_N_commissioner_notes.md or pass the week explicitly."
        )
    return int(m.group(1))


def _autolink_bare_urls(line: str) -> str:
    def repl(m: re.Match) -> str:
        url = m.group(0)
        trail = ''
        while url and url[-1] in '.,;:!?':
            trail = url[-1] + trail
            url = url[:-1]
        return f'<{url}>{trail}'
    return _BARE_URL_RE.sub(repl, line)


def normalize_markdown(text: str) -> str:
    """
    Make notes typed the way people actually type them parse the way they
    look, without changing any wording:
      - [CLAUDE: insert X.png here] -> a standalone ![](X.png) paragraph
      - any other leftover [CLAUDE: ...] -> NotesError (an unresolved
        instruction would otherwise silently publish)
      - list directly after a paragraph (no blank line) gets its blank line
      - "  ## Heading" (1-3 leading spaces) -> "## Heading"
      - trailing double-space "hard breaks" at paragraph end are dropped
      - bare URLs become links; ^TM becomes a superscript
    """
    text = text.replace('\r\n', '\n')
    text = _PLACEHOLDER_RE.sub(lambda m: f'\n\n![]({m.group("ref")})\n\n', text)

    leftover = _ANY_DIRECTIVE_RE.findall(text)
    if leftover:
        raise NotesError(
            "Unresolved [CLAUDE: ...] instruction(s) in the notes: "
            + "; ".join(leftover)
            + ". Only image placeholders like [CLAUDE: insert file.png here] are auto-resolved."
        )

    lines = text.split('\n')
    out: list[str] = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ''
        if _LOOSE_HEADING_RE.match(line):
            line = line.lstrip()
        if not nxt.strip():
            line = line.rstrip()
        if _LIST_LINE_RE.match(line) and out:
            prev = out[-1]
            if prev.strip() and not _LIST_LINE_RE.match(prev) and not prev[:1].isspace():
                out.append('')
        out.append(_autolink_bare_urls(line))

    return '\n'.join(out).replace('^TM', '<sup>TM</sup>')


def _image_html(src: str, alt: str) -> str:
    return (f'<img src="{src}" alt="{alt}" '
            f'style="display:block;max-width:100%;height:auto;margin:16px auto;">')


def render_notes(
    markdown_text: str,
    resolve_image: Optional[Callable[[str], str]] = None,
    heading: Optional[str] = DEFAULT_HEADING,
) -> tuple[str, list[str]]:
    """
    Markdown -> (HTML for the inside of the SUMMARY markers, image refs used).

    Heading levels map 1:1 (## -> <h2>, ### -> <h3>). `resolve_image` maps
    an image ref as written in the notes ("week_3_luck.png") to the `src`
    to publish (a data: URI, an uploaded URL, ...); http(s) refs are left
    alone. With resolve_image=None the refs are left as written.
    """
    body = markdown.markdown(normalize_markdown(markdown_text))
    refs: list[str] = []

    def swap(m: re.Match) -> str:
        ref = m.group('src')
        alt_m = _ALT_RE.search(m.group(0))
        alt = alt_m.group('alt') if alt_m else ''
        if not alt:
            alt = re.sub(r'[_\-]+', ' ', Path(ref).stem)
        if ref.startswith(('http://', 'https://')):
            return _image_html(ref, alt)
        refs.append(ref)
        return _image_html(resolve_image(ref) if resolve_image else ref, alt)

    body = _IMG_TAG_RE.sub(swap, body)
    head = f'<h2>{heading}</h2>\n' if heading else ''
    return head + body, refs


def inject_summary(page_html: str, summary_inner_html: str) -> str:
    """
    Replace whatever sits between the SUMMARY markers in a recap page (local
    file or the raw content of the WP post) with `summary_inner_html`. Same
    marker contract as scripts/html_scoring.merge_recap_summary(). Raises
    NotesError if the page has no markers rather than guessing where to put it.
    """
    if SUMMARY_MARKER_START not in page_html or SUMMARY_MARKER_END not in page_html:
        raise NotesError("Page has no <!-- SUMMARY:START --> / <!-- SUMMARY:END --> markers to write into.")
    inner = _WP_DELIM_RE.sub('', summary_inner_html)
    before, rest = page_html.split(SUMMARY_MARKER_START, 1)
    _, after = rest.split(SUMMARY_MARKER_END, 1)
    return f'{before}{SUMMARY_MARKER_START}\n{inner}\n{SUMMARY_MARKER_END}{after}'


# ------------------------------------------------------------------
# Images (file I/O lives here)
# ------------------------------------------------------------------

def find_image(ref: str, search_dirs: Iterable) -> Path:
    """Resolve an image ref to a real file: absolute path, else first hit across search_dirs."""
    p = Path(ref)
    if p.is_absolute():
        if p.is_file():
            return p
        raise NotesError(f"Image not found: {ref}")
    tried = []
    for d in search_dirs:
        cand = Path(d) / ref
        tried.append(str(cand))
        if cand.is_file():
            return cand
    raise NotesError(f"Image '{ref}' not found. Looked in:\n  " + "\n  ".join(tried))


def image_is_blank(path: Path) -> bool:
    """
    True if the image is one flat colour (an empty/never-drawn figure saves
    as an all-white PNG). False if it isn't -- or if Pillow isn't installed,
    since this is only a safety net.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    with Image.open(path) as im:
        return all(lo == hi for lo, hi in im.convert('RGBA').getextrema())


def embed_resolver(search_dirs: Iterable, warnings: Optional[list[str]] = None) -> Callable[[str], str]:
    """resolve_image that inlines each file as a base64 data: URI (self-contained page)."""
    search_dirs = list(search_dirs)

    def resolve(ref: str) -> str:
        path = find_image(ref, search_dirs)
        _warn_if_blank(path, warnings)
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')

    return resolve


def upload_resolver(search_dirs: Iterable, client, warnings: Optional[list[str]] = None) -> Callable[[str], str]:
    """resolve_image that uploads each file via client.upload_media(path) -> URL."""
    search_dirs = list(search_dirs)

    def resolve(ref: str) -> str:
        path = find_image(ref, search_dirs)
        _warn_if_blank(path, warnings)
        return client.upload_media(path)

    return resolve


def _warn_if_blank(path: Path, warnings: Optional[list[str]]) -> None:
    if warnings is not None and image_is_blank(path):
        warnings.append(f"{path} is a single flat colour (blank) -- the figure was probably never drawn before saving.")
