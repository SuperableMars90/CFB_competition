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
   elections must be resolved by — default 4), `--draft-picks-per-player`
   (draft length / active roster size per player — **default 25**, for
   4-player pods; pass `--draft-picks-per-player 30` for a 3-player-pod
   season). This is `seasons.draft_picks_per_player` (added 2026-08, see
   `transition_plan.md`) — set once here at season creation, not editable
   from the app afterward without a direct SQL update.

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
   `lib/metagame.py: compute_pod_standings()`). Pod *count* and pod *size*
   are independent settings: 2 pods works whether each pod has 3 or 4
   players (or any other size), size is just however many
   `pod_memberships` rows you create per pod in the next step. The
   combination of (pod count, total players across all pods) has to be
   one with a confirmed playoff bracket format though — currently
   1 pod/4 players (`four_team`), 2 pods/6 players (`six_team`), or
   2 pods/8 players (`eight_team`); see
   `lib.playoffs.bracket_format_for_league_shape`. Anything else raises
   an error at PVP-schedule-generation time (step 8) rather than
   guessing a shape.

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
   -- 2 pods of 4 (8 total players, eight_team playoffs):
   INSERT INTO scoring_contexts (season_id, name, scoring_type, points_map)
   VALUES (8, 'cross_pod_8player', 'match_play_pod', '[7,6,5,4,3,2,1,0]');

   -- 2 pods of 3 (6 total players, six_team playoffs):
   INSERT INTO scoring_contexts (season_id, name, scoring_type, points_map)
   VALUES (9, 'cross_pod_6player', 'match_play_pod', '[5,4,3,2,1,0]');
   ```
   Use `scoring_type = 'match_play_pod'` for a 2-pod season,
   `'match_play'` for single-pod. `points_map` is base match-play points
   by overall rank (index 0 = 1st place) — `compute_pod_standings()`
   stacks up to three +1 bonuses on top (pod winner, pod-vs-pod, overall
   winner), so max weekly total is `points_map[0] + 3`. The `[5,4,3,2,1,0]`
   ladder above is a confirmed decision (Zach, 2026-08-09) for the 6-player
   format, not a placeholder — same "1 point per spot" logic as the
   8-player ladder, just for 6 total players (max weekly total 8, not 10).

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
   Bracket format (and therefore scheduling shape) is auto-detected from
   your pods/players, not a flag — `--single-pod-repeats` (default 3,
   `four_team`), `--two-pod-in-pod-repeats` (default 2, `eight_team`),
   and `--six-team-repeats` (default 2, `six_team` — a flat schedule
   where everyone plays everyone this many times regardless of pod, per
   `transition_plan.md`'s confirmed design) only matter for whichever
   one actually applies to this season.

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
   `six_team`/`eight_team` formats), and resolves every seed-sourced
   bracket placeholder to a real player. **Refuses to run if the regular
   season isn't actually fully scored yet** — that's intentional, not a bug.

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
