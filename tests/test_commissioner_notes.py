"""Tests for lib/commissioner_notes.py and scripts/import_commissioner_notes.py."""

import base64

import pytest
from PIL import Image

from lib import commissioner_notes as cn
from lib.commissioner_notes import (
    NotesError,
    embed_resolver,
    find_image,
    image_is_blank,
    inject_summary,
    normalize_markdown,
    render_notes,
    week_from_filename,
)


def _png(path, color=(255, 255, 255, 255), pixel=None):
    im = Image.new('RGBA', (8, 8), color)
    if pixel:
        im.putpixel((0, 0), pixel)
    im.save(path)
    return path


# ---------- markers stay in lockstep with the page generator ----------

def test_markers_match_html_scoring():
    from scripts import html_scoring
    assert cn.SUMMARY_MARKER_START == html_scoring.SUMMARY_MARKER_START
    assert cn.SUMMARY_MARKER_END == html_scoring.SUMMARY_MARKER_END


# ---------- filename ----------

@pytest.mark.parametrize('name,week', [
    ('week_3_commissioner_notes.md', 3),
    ('scratch/week_12_notes.md', 12),
    ('Week-1.md', 1),
])
def test_week_from_filename(name, week):
    assert week_from_filename(name) == week


def test_week_from_filename_missing():
    with pytest.raises(NotesError):
        week_from_filename('notes.md')


# ---------- normalize_markdown ----------

@pytest.mark.parametrize('marker', [
    '[CLAUDE: insert week_3_score_graph.png here]',
    '[CLAUDE: insert week_3_score_graph.png]',
    '[CLAUDE: week_3_score_graph.png]',
])
def test_image_placeholder_variants(marker):
    out = normalize_markdown(f"Before\n{marker}\n After")
    assert '![](week_3_score_graph.png)' in out
    assert '\n\n![](week_3_score_graph.png)\n\n' in out   # own paragraph even when glued to text


def test_unresolved_directive_raises():
    with pytest.raises(NotesError, match='add a link'):
        normalize_markdown('Text [CLAUDE: add a link to the scoring guide here] more')


def test_list_after_paragraph_gets_blank_line():
    html, _ = render_notes("Next week:\n* a\n* b\n")
    assert '<ul>' in html and '<li>a</li>' in html


def test_nested_list_survives():
    html, _ = render_notes("Intro:\n* one\n    * nested\n* two\n")
    assert html.count('<ul>') == 2


def test_loose_heading_and_hard_break_cleanup():
    out = normalize_markdown("Line one.  \n\n ## Title\n")
    assert 'Line one.\n' in out and '\n## Title' in out


def test_bare_url_autolinked_without_trailing_period():
    html, _ = render_notes("See https://example.com/x/. Done.")
    assert '<a href="https://example.com/x/">https://example.com/x/</a>.' in html


def test_existing_markdown_link_not_double_wrapped():
    html, _ = render_notes("See [the guide](https://example.com/guide).")
    assert html.count('<a ') == 1


def test_tm_superscript():
    html, _ = render_notes("The Right Way^TM")
    assert 'Way<sup>TM</sup>' in html


def test_wording_untouched_apart_from_markup():
    html, _ = render_notes("This are wrong but stay wrong, in the middle of day.")
    assert 'This are wrong but stay wrong, in the middle of day.' in html


# ---------- render_notes ----------

def test_headings_map_one_to_one_and_default_heading_added():
    html, _ = render_notes("## A\ntext\n### B\n")
    assert html.startswith("<h2>Commissioner's Notes</h2>")
    assert '<h2>A</h2>' in html and '<h3>B</h3>' in html


def test_heading_can_be_suppressed():
    html, _ = render_notes("text", heading=None)
    assert 'Commissioner' not in html


def test_resolver_called_for_local_images_only():
    seen = []

    def resolver(ref):
        seen.append(ref)
        return 'DATA:' + ref

    html, refs = render_notes("![](a.png)\n\n![x](https://cdn.example/b.png)", resolver)
    assert seen == ['a.png'] and refs == ['a.png']
    assert 'src="DATA:a.png"' in html and 'src="https://cdn.example/b.png"' in html


def test_alt_falls_back_to_filename_and_explicit_alt_wins():
    html, _ = render_notes("![](week_3_luck.png)\n\n![Real alt](x.png)")
    assert 'alt="week 3 luck"' in html and 'alt="Real alt"' in html


def test_ampersand_escaped():
    html, _ = render_notes("Texas A&M at LSU")
    assert 'A&amp;M' in html


# ---------- inject_summary ----------

PAGE = f"<div>top</div>\n{cn.SUMMARY_MARKER_START}\n<h2>old</h2><p></p>\n{cn.SUMMARY_MARKER_END}\n<div>bottom</div>"


def test_inject_replaces_only_between_markers():
    out = inject_summary(PAGE, '<p>new</p>')
    assert '<p>new</p>' in out and 'old' not in out
    assert out.startswith('<div>top</div>') and out.endswith('<div>bottom</div>')


def test_inject_is_idempotent():
    once = inject_summary(PAGE, '<p>new</p>')
    assert inject_summary(once, '<p>new</p>') == once


def test_inject_strips_wp_block_delimiters():
    out = inject_summary(PAGE, '<!-- wp:paragraph --><p>x</p><!-- /wp:paragraph -->')
    assert 'wp:' not in out


def test_inject_without_markers_raises():
    with pytest.raises(NotesError):
        inject_summary('<div>nothing</div>', '<p>x</p>')


def test_inject_handles_backslashes_and_dollars_in_notes():
    out = inject_summary(PAGE, r'<p>\1 \g<0> $1</p>')
    assert r'\1 \g<0> $1' in out


# ---------- images ----------

def test_find_image_search_order_and_error(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    _png(b / 'x.png')
    assert find_image('x.png', [a, b]) == b / 'x.png'
    with pytest.raises(NotesError, match='Looked in'):
        find_image('nope.png', [a, b])


def test_blank_detection(tmp_path):
    assert image_is_blank(_png(tmp_path / 'blank.png'))
    assert image_is_blank(_png(tmp_path / 'clear.png', color=(0, 0, 0, 0)))
    assert not image_is_blank(_png(tmp_path / 'real.png', pixel=(255, 0, 0, 255)))


def test_embed_resolver_data_uri_and_blank_warning(tmp_path):
    _png(tmp_path / 'blank.png')
    _png(tmp_path / 'real.png', pixel=(255, 0, 0, 255))
    warnings = []
    resolve = embed_resolver([tmp_path], warnings)
    uri = resolve('real.png')
    assert uri.startswith('data:image/png;base64,')
    assert base64.b64decode(uri.split(',', 1)[1]) == (tmp_path / 'real.png').read_bytes()
    assert warnings == []
    resolve('blank.png')
    assert len(warnings) == 1 and 'blank' in warnings[0]


def test_upload_media_posts_bytes_and_returns_url(tmp_path, monkeypatch):
    from scripts import wordpress_client as wc
    img = _png(tmp_path / 'fig.png', pixel=(1, 2, 3, 255))
    monkeypatch.setattr(wc.WordPressClient, '_load_config',
                        lambda self: {'url': 'https://example.com', 'username': 'u', 'password': 'p'})
    client = wc.WordPressClient()
    calls = {}

    def fake_request(method, path, **kw):
        calls.update(method=method, path=path, **kw)
        return {'source_url': 'https://example.com/wp-content/uploads/fig.png'}

    monkeypatch.setattr(client, '_request', fake_request)
    assert client.upload_media(img) == 'https://example.com/wp-content/uploads/fig.png'
    assert calls['method'] == 'POST' and calls['path'] == 'media'
    assert calls['data'] == img.read_bytes()
    assert calls['headers']['Content-Type'] == 'image/png'
    assert 'filename="fig.png"' in calls['headers']['Content-Disposition']


# ---------- run() end to end ----------

def test_run_writes_fragment_and_updates_page(tmp_path, monkeypatch):
    from scripts import import_commissioner_notes as imp
    monkeypatch.setattr(imp, 'WEB_DIR', tmp_path / 'web')
    (tmp_path / 'web').mkdir()
    page = tmp_path / 'web' / 'week_7_final.html'
    page.write_text(PAGE, encoding='utf-8')
    _png(tmp_path / 'fig.png', pixel=(9, 9, 9, 255))
    notes = tmp_path / 'week_7_commissioner_notes.md'
    notes.write_text("Hello.\n\n[CLAUDE: insert fig.png here]\n", encoding='utf-8')

    r = imp.run(notes)
    assert r['week'] == 7 and r['images'] == ['fig.png'] and r['warnings'] == []
    assert r['pushed'] is False
    text = page.read_text(encoding='utf-8')
    assert 'Hello.' in text and 'data:image/png;base64,' in text and 'old' not in text
    assert (tmp_path / 'web' / 'week_7_commissioner_notes.html').is_file()

    imp.run(notes)   # re-run replaces, not appends
    assert page.read_text(encoding='utf-8') == text


def test_run_dry_run_writes_nothing_and_flags_blank(tmp_path, monkeypatch):
    from scripts import import_commissioner_notes as imp
    monkeypatch.setattr(imp, 'WEB_DIR', tmp_path / 'web')
    _png(tmp_path / 'fig.png')   # blank
    notes = tmp_path / 'week_2_notes.md'
    notes.write_text("[CLAUDE: insert fig.png]", encoding='utf-8')
    r = imp.run(notes, dry_run=True)
    assert len(r['warnings']) == 1
    assert not (tmp_path / 'web').exists()


def test_run_upload_without_push_refused(tmp_path):
    from scripts import import_commissioner_notes as imp
    notes = tmp_path / 'week_2_notes.md'
    notes.write_text("hi", encoding='utf-8')
    with pytest.raises(NotesError, match='push'):
        imp.run(notes, images='upload')


def test_run_missing_image_is_an_error(tmp_path, monkeypatch):
    from scripts import import_commissioner_notes as imp
    monkeypatch.setattr(imp, 'WEB_DIR', tmp_path / 'web')
    notes = tmp_path / 'week_2_notes.md'
    notes.write_text("[CLAUDE: insert missing.png here]", encoding='utf-8')
    with pytest.raises(NotesError, match='missing.png'):
        imp.run(notes)
