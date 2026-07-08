# Commissioner checklist

Every script you need to run, grouped by how often you run it. Assumes
the environment is already set up — see `INSTALL.md` for that. All
commands run from the `cfb_game/` directory with `PYTHONPATH=.` set:

```bash
export PYTHONPATH=.
```

A few of the "once per season" steps below are direct SQL, not
scripts — flagged explicitly where that's true, rather than pretending
a script exists.

---

## Once per season (before Week 1)

Run in this order — each step depends on the one before it.

1. **Pull the season's teams/conferences/schedule from CFBD:**
   ```bash
   python scripts/init_season.py --year 2026 --dry-run   # preview first
   python scripts/init_season.py --year 2026
   ```
   Check the dry-run output: FBS conference count (~10), FBS team count
   (130+), FCS teams present, game count (900+). Investigate any
   skipped teams before running for real.
   Optional flags: `--week0-ids`/`--week0-file` (CFBD game IDs played
   in the season's opening week-0 slate), `--overrides` (canonical name
   corrections), `--week0-cutoff` (which week undeployed week-0
   elections must be resolved by — default 4).

2. **Assign P4/G6 tiers to conferences — direct SQL, no script.**
   The script's own docstring mentions an `--assign-tiers` flag; it
   doesn't actually exist in the current code (checked — not in this
   doc's imagination, in `init_season.py`'s argparse setup). Do this
   instead:
   ```sql
   UPDATE conferences SET tier = 'P4' WHERE abbreviation IN (...);
   UPDATE conferences SET tier = 'G6' WHERE abbreviation IN (...);
   ```
   Independent teams (Notre Dame etc.) and FCS teams are classified
   separately on the `teams` table, not via `conferences.tier` —
   check `teams.classification`/independent flags directly if unsure.

3. **Create this season's pods — direct SQL, no script.**
   ```sql
   INSERT INTO pods (season_id, name) VALUES (8, 'Orange Pod'), (8, 'White Pod');
   ```
   One pod = no split needed, just make sure exactly one `pods` row
   exists for the season (multi-pod scoring requires exactly 2 — see
   `lib/metagame.py: compute_pod_standings()`).

4. **Create player rows and pod memberships — direct SQL, no script.**
   ```sql
   INSERT INTO players (name, username, is_commissioner) VALUES (...);
   INSERT INTO pod_memberships (season_id, player_id, pod_id) VALUES (...);
   ```
   Then add each player's login to `.streamlit/secrets.toml`'s
   `[auth]` section (see `INSTALL.md`) — exact-case username matching
   `players.username`.

5. **Seed the scoring context — direct SQL, no script.**
   ```sql
   INSERT INTO scoring_contexts (season_id, name, scoring_type, points_map)
   VALUES (8, 'cross_pod_8player', 'match_play_pod', '[7,6,5,4,3,2,1,0]');
   ```
   Use `scoring_type = 'match_play_pod'` for a 2-pod season,
   `'match_play'` for single-pod.

6. **(Optional) Export the pre-draft schedule guide:**
   ```bash
   python scripts/export_draft_guide.py --season 8 --output draft_guide.xlsx
   ```
   Every FBS team's full schedule in one grid — handy to have open
   during the draft.

7. **Run the draft.** Two ways to do this, not both:
   - **Live, in-app** (the normal path today): players draft directly
     through the Streamlit app's draft page (`app_pages/draft.py`).
     Nothing to run from the commissioner side.
   - **Import an externally-run draft** (e.g. a spreadsheet draft, or
     backfilling a historical season): a CSV/XLSX matching the
     expected format —
     ```bash
     python ingest_draft.py --file 2026_draft.xlsx --season 2026 --pod "Orange Pod" --dry-run
     python ingest_draft.py --file 2026_draft.xlsx --season 2026 --pod "Orange Pod"
     ```
     Run once per pod.

8. **Generate the PVP regular-season + playoff schedule:**
   ```bash
   python scripts/generate_pvp_schedule.py --season 2026 --season-weeks 13 --dry-run
   python scripts/generate_pvp_schedule.py --season 2026 --season-weeks 13
   ```
   `--season-weeks` is the real number of weeks this metagame season
   runs (not `seasons.last_week`, which is the full CFB calendar
   including weeks after your season ends — see `PROGRESS.md`'s note
   on this if the numbers look off). Check the dry-run's printed
   schedule (round count, bye weeks, matchup counts) before writing.
   `--scoring-context` defaults to the season's active context from
   step 5 if only one exists; pass it explicitly if there's ambiguity.

You should now have a season ready for Week 1 lineups.

---

## Weekly, during the season

1. **Game day — start live scoring:**
   ```bash
   python scripts/scoring_engine.py --season 8 --week 1
   ```
   Polls the CFBD scoreboard every 5 minutes (`--interval` to change
   it), scores every submitted lineup, writes a local JSON checkpoint,
   updates the score plot, and pushes the live status page to
   WordPress. Leave it running through the day's games, or re-run
   periodically — either works, it's idempotent per checkpoint.

2. **Once every game is final — finalize the week:**
   ```bash
   python scripts/scoring_engine.py --season 8 --week 1 --finalize-only
   ```
   Pulls final scores from CFBD, writes `weekly_results`, resolves
   this week's PVP matchups, backfills the `games` table, detects new
   records, and pushes both the Records table and the recap page to
   WordPress. Also safe to re-run as a correction (e.g. a stat
   correction from CFBD after the fact) — it's built to be idempotent.

3. **Drop/add requests** — reviewed entirely in-app
   (`app_pages/drop_add.py`, approve/deny). Approving one auto-pushes
   the updated rosters page to WordPress; no separate script needed.
   **Known gap, not yet built:** if a player has an undeployed Week 0
   election still outstanding once `week0_cutoff_week` passes, nothing
   currently auto-assigns it for them — see `PROGRESS.md`'s open items.
   Check for this manually until that's built.

---

## End of regular season (once, at PVP regular-season end)

1. **Seed the playoffs:**
   ```bash
   python scripts/seed_playoffs.py --season 2026
   ```
   Computes final standings, assigns bracket seeds (pod-aware for the
   8-team format), and resolves every seed-sourced bracket placeholder
   to a real player. **Refuses to run if the regular season isn't
   actually fully scored yet** — that's intentional, not a bug.

2. **If step 1 raises `UnresolvedTieError`:**
   ```bash
   python scripts/break_tie.py --season 2026
   ```
   Interactive — prints the tied group's context, prompts you for the
   resolved order, saves it, then re-runs seeding automatically. Repeat
   if there's more than one unresolved tie group.

---

## Playoffs, weekly

```bash
python scripts/advance_playoffs.py --season 2026 --week 12
```
Run once per real playoff week (`--week` is the real calendar week,
not a playoff-round number). Scores that week's playoff games,
decides them, and propagates winners into next-round placeholder slots
via the bracket engine. On the bracket's final round, also writes final
placements to `season_standings`.

---

## Ad hoc / as needed

- `python scripts/test_db_connection.py` — quick DB connectivity smoke
  test. Run this first if anything above is failing to connect.
- `python -m scripts.test_roster_context [player_id] [season_id] [week]`
  — pull one player's roster with game context, for debugging.
- Records/rosters WordPress pages regenerate automatically as part of
  `--finalize-only` above (step 2 of the weekly section) — you
  shouldn't need to run `scripts/html_records.py`/`scripts/html_rosters.py`
  directly; they're library modules called from there, not standalone
  CLI tools.
