# Commissioner notes: file format and import

Each week's "Commissioner's Notes" section of the recap is written as a plain
Markdown file and imported by code. Hand a file to Claude, or run the script or
notebook call below.

**Convention:** `scratch/week_N_commissioner_notes.md`. The week is inferred from
the filename. Images referenced by the notes sit next to it in `scratch/`.

## What you can write

Ordinary Markdown. The importer tolerates how the notes are typed in practice, so
nothing below needs pre-cleaning:

| You write | Result |
|---|---|
| `## Title`, `### Sub` | `<h2>`, `<h3>` (1:1; the recap page already provides the "Commissioner's Notes" `<h2>`, so don't add your own) |
| Plain paragraphs | `<p>` |
| `* item` (nested with 4 spaces) | `<ul>`; a blank line before the list is optional |
| `**bold**`, `*italic*`, `[text](url)` | as expected; bare `https://...` URLs become links |
| `[CLAUDE: insert week_3_luck.png here]` | the image, in its own block (the trailing "here" is optional) |
| `![alt text](week_3_luck.png)` | same, with explicit alt text; this is the form to prefer going forward |
| `^TM` | superscript TM |

Images are found in: `--image-dir` folders, then the notes file's own folder, then
`output/scoring/`, then `output/`. Use filenames without spaces.

**Wording is never changed.** Typos in the notes are published as written. Fix the
`.md` and re-run; the import replaces the previous notes and is idempotent.

**Errors, not silent guesses.** These stop the import:
- any other `[CLAUDE: ...]` instruction (only image placeholders are auto-resolved)
- an image file that can't be found (the message lists where it looked)
- a recap page/post with no `<!-- SUMMARY:START -->` / `<!-- SUMMARY:END -->` markers

A flat single-colour image (an empty matplotlib figure saves as all-white) produces
a `[WARNING]` but still imports.

## Running it

```bash
# validate only: renders, checks images, writes nothing
python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md --check

# local: writes output/web/week_3_commissioner_notes.html (the fragment)
# and splices it into output/web/week_3_final.html
python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md

# also update the LIVE WordPress recap post (slug {year}-week-{N}-recap)
python scripts/import_commissioner_notes.py scratch/week_3_commissioner_notes.md --push
```

From a notebook or another script:

```python
from scripts.import_commissioner_notes import run

r = run("scratch/week_3_commissioner_notes.md")               # local only
r = run("scratch/week_3_commissioner_notes.md", push=True)    # + WordPress
r["html"], r["warnings"], r["images"]

# lower level, pure functions:
from lib.commissioner_notes import render_notes, inject_summary
html, image_refs = render_notes(open("...md").read())
```

## Images

- `--images embed` (default): each image is inlined as a base64 `data:` URI, so the
  page is self-contained and re-pushing never clutters the media library. Each
  image adds roughly 1.4x its file size to the post.
- `--images upload` (requires `--push`): uploads to the WordPress media library and
  uses the returned URL. Every run uploads a fresh copy, so use it once the images
  are final.

`--images upload --push` was run live for week 3 (2026-09-20): both images uploaded,
the post updated, and the URLs returned 200. `--images embed` with `--push` has not
been run against the live site (WordPress might strip `data:` URIs), so prefer `upload`
for anything you push.

## Saving notebook figures (why images come out blank)

An all-white 640x480 PNG is a brand-new empty figure. In a Jupyter notebook the inline
backend closes your figure when `plt.show()` runs (or when the cell ends), so
`plt.savefig(...)` after `plt.show()`, or in a later cell, saves an empty
one. Save the figure object, before `plt.show()`, to an absolute path:

```python
fig.savefig(REPO_ROOT / "scratch" / "week_3_luck.png", dpi=100)   # then plt.show()
```

A bare relative path lands in the notebook's own folder (`scratch/notebooks/`), not `scratch/`.
The importer flags blank images with a warning; it will not fix them for you.

## Interaction with `scoring_engine.py --finalize-only`

Finalize rewrites `output/web/week_N_final.html` from scratch (dropping local
notes) but preserves whatever notes are live on WordPress
(`merge_recap_summary`). So a local-only import is lost on the next finalize; a
`--push` import survives it. Re-run the import after a finalize if you're working
locally.

## For Claude, when handed a notes file

1. Proofread only if asked, and put the result in a separate file. Never edit the
   user's `.md`.
2. Run `--check` first and report any warnings (especially blank images) before writing.
3. Import locally. Do **not** `--push` unless the user says so; it's publishing.
4. Report what changed and anything flagged, in a couple of lines.
