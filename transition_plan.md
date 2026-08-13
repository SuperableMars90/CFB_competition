# Transition Plan: 3-Player Pods (30-Team Rosters) + Revised PVP

Status: planning only — no code changed yet. This is the inventory + sequencing
requested before implementation starts.

## Confirmed scope (per conversation, 2026-08-09)

- Pod size: 3 players (was 4).
- Roster size: 30 teams per player (was 25) — driven directly by pod size, same
  1 pick-per-round-per-player relationship as today.
- League shape for this transition: **2 pods of 3 (6 players total)**, down from
  2 pods of 4 (8 players). Pod *count* stays at 2 — only pod size shrinks.
- PVP: revised to a **flat double round-robin across the whole league** — every
  player plays every other player twice, regardless of pod. This replaces
  today's tiered "podmates more often, cross-pod once" structure.
- Playoff logic: **in scope, fully built and verified (2026-08-09)** — bracket
  shape and seeding rule for the 6-player format, established from
  `playoff_games.csv`/`placement.csv` (see "Playoff logic — 6-team bracket"
  below). Originally called out as shelved for a later conversation; that call
  is reversed now that the design was worked out and implemented end-to-end
  (`SIX_TEAM_BRACKET`, `assign_seeds_6_team`, `bracket_format_for_league_shape`,
  live-verified via a full seed→advance→placements pipeline test). Nothing
  playoff-related remains deferred.
- Game structure itself is unchanged: 16-slot weekly lineup (4 P4 + 6 G6 + 3 P4
  flex + 2 G6 flex + 1 wild card), same conference/tier eligibility rules. None
  of that code is in scope.

## Good news: needs no code change

These were checked against "assumes 4 players" / "assumes 25 teams" and came
back clean — they're already parameterized by whatever pod/roster size is
passed in, not hardcoded:

- **`lib/draft.py`** — `slot_for_pick`, `total_picks`, `generate_full_draft_order`
  all take `base_order`/`picks_per_player` as arguments and do pure position
  math off `len(base_order)`. Passing a 3-player `base_order` and
  `picks_per_player=30` works with zero changes here.
- **`lib/db.py`: `is_draft_complete`, `submit_draft_picks`, `get_draft_page_state`,
  `set_draft_order`** — all pod-scoped by counting actual `draft_order`/`rosters`
  rows for the given `pod_id`, never by a hardcoded player or pick count.
- **`ingest_draft.py`** — player count and pick-in-round math are derived from
  `len(player_map)` (the real pod roster from the DB), not a constant. Only a
  cosmetic docstring reference to "Round number (1-25)" needs a wording fix.
- **`db/schema.sql`: `pods`, `pod_memberships`, `rosters`, `draft_order`** — pod
  size is emergent from how many `pod_memberships` rows exist per pod; nothing
  in the schema encodes "4" or "25" structurally. No migration needed for pod
  size itself.
- **`scoring_contexts.points_map`** (ADR-010) — already a data-driven JSON
  column, not code. A new ladder value is a config decision at season-init
  time, not a code change (see below, it does need a *new value* decided).
- **`lib/game_rules.py`, `lib/game_context.py`, `lib/dropadd_rules.py`** — operate
  entirely on the 16-slot weekly lineup and conference tiers; no roster-size or
  pod-size assumption found in any of the three.
- **`lib/optimal_lineup.py`, `lib/performance.py`** — the min-cost-flow optimizer
  is pool-size-agnostic by construction. "25-team roster" appears only in
  comments as a worked example (and in the float-precision bug writeup), never
  as a hardcoded bound.

## Needs code changes — draft & roster size

1. **`lib/config.py: DRAFT_PICKS_PER_PLAYER = 25`** — the actual problem. It's a
   single flat Python constant, imported once at `lib/db.py:1192` inside
   `set_draft_order()`. That's a good, contained call site — but a flat global
   can't represent "season 1 was 25/player, this new format is 30/player"
   simultaneously, and this repo already keeps old seasons' history alongside
   the live one. This needs to become **data-driven per season** (or per pod,
   if pod size ever varies within a season) rather than a code constant — the
   same move ADR-010 already made for `points_map`. Suggested shape: a
   `seasons.draft_picks_per_player` column, read at `set_draft_order()` time.
   This is an ADR candidate for the implementation session, not decided here.
2. **Comment/docstring-only updates** (no logic change): `lib/optimal_lineup.py`
   and `lib/performance.py`'s "25-team roster" language, `ingest_draft.py`'s
   "Round number (1-25)" docstring line.

## Needs code changes — PVP scheduling

- **`lib/scheduling.py`** — `build_pvp_schedule`'s two-pod branch
  (`bipartite_round_robin` + `interleave_pvp_weeks` combining in-pod and
  cross-pod rounds into separate week-types) is built for exactly the tiered
  structure you're replacing. Given the confirmed flat double round-robin
  design, the recommended approach is to route the 2-pod case through the
  **same code path `single_pod_players` already uses**
  (`circle_method_round_robin` + `repeat_rounds`, over the full 6-player list
  combined) and derive `matchup_type` (`'in_pod'`/`'cross_pod'`) *after*
  generating each pairing, by comparing pod membership — rather than
  generating pod-partitioned schedules and stitching them together. This also
  fully retires `bipartite_round_robin`'s "equal pod size" restriction as a
  going concern, and makes `interleave_pvp_weeks`'s in-pod/cross-pod
  interleaving logic unnecessary (there'd be exactly one round type again, as
  in single-pod mode).
- **`scripts/generate_pvp_schedule.py`** — branches hard on `len(pods) == 1` vs.
  else, and calls `bracket_format_for_pod_count` (renamed
  `bracket_format_for_league_shape` — playoff logic is no longer shelved, see
  below) to determine `regular_season_weeks = season_weeks - playoff_rounds`.
  This was a real coupling point, not just a cosmetic one — see the
  dependency flagged below (resolved along with the playoff work).
- **`lib/scheduling.py`'s module docstring** ("Supports exactly two scenarios...
  3+ pods explicitly unsupported") should be rewritten once the above lands to
  describe "one flat player pool, `matchup_type` derived post-hoc from pod
  membership" rather than "single pod vs. two equal pods."

## Regular-season standings (confirmed + verified, 2026-08-09)

**Done.** Prediction held: `lib/metagame.py: compute_pod_standings` needed
**zero code changes** for the 3-player-pod (6-total-player) case — it was
already pod-size-agnostic (`_points_for_rank` indexes whatever-length
`points_map` it's given; `pod_bonus`/`pod_vs_pod_bonus` are "rank 1 within
whatever `pod_of_player` groups exist," not hardcoded to 3 or 4 per pod).
The "exactly 2 distinct pods" guard stays valid since pod count is staying
at 2.

**Points ladder (Zach, 2026-08-09):** `[5, 4, 3, 2, 1, 0]` — same "1 point
per spot" logic as season 8's `[7,6,5,4,3,2,1,0]` (rank `r` gets `n - r`
points), just for `n=6` total players instead of 8. Bonus logic
(`pod_bonus`/`pod_vs_pod_bonus`/`overall_bonus`) is unchanged and stacks the
same way. Max weekly total for this format is **8** (5 base + 3 bonuses),
not season 8's 10 — that 10 was an artifact of `n=8`, not an invariant this
format needs to preserve. This is a data decision (a `scoring_contexts` row
at season-init time, per ADR-010), not a code change — record it in
`docs/commissioner_checklist.md`'s example when step 7 (docs) happens.

Verified via 2 new unit tests in `tests/test_metagame.py`
(`test_pod_standings_six_player_base_points_by_overall_rank`,
`test_pod_standings_six_player_bonuses_stack_to_max_eight_for_overall_winner`),
mirroring the existing 8-player fixtures exactly with 3-player pods and the
new ladder. Full suite: 211 unit tests passing. **Not verified live**: the
DB-writing wrapper (`compute_weekly_standings`/`write_weekly_results`) with
a real `match_play_pod` scoring context and 6 players — consistent with this
test file's own stated convention (that wrapper "isn't unit tested since it
opens its own DB connection... verified live instead"), so this is
deferred the same way live verification always has been for this repo, not
a gap introduced here.

## Playoff logic — 6-team bracket (confirmed design, built + verified 2026-08-09)

Originally shelved for a later conversation; un-shelved once the bracket
shape and seeding rule for the 2-pods-of-3 (6-player) format were worked
out from `playoff_games.csv`/`placement.csv`. The design below is not just
decided — it's fully implemented and verified end-to-end against live
Aiven, including a full seed→advance→placements pipeline test and a real
live-schema fix (see "Follow-up checklist items" and step 8 below).

**Seeding.** Seed 1 and seed 2 = the two pod winners (each pod's own
`pod_rank == 1` player in `lib.seeding.compute_seeding`'s output), ordered
against each other by that same standings logic — whichever pod winner ranks
better overall gets seed 1. Seeds 3-6 = the remaining 4 players, ordered among
themselves by that same logic, preserving their relative overall order.

This is **not new logic** — it's algebraically identical to what
`assign_seeds_8_team` already does ("seed 1 = overall #1, seed 2 = first
remaining player from a different pod, seeds 3-N = everyone else in original
overall order"), for any 2-pod league regardless of pod size: a pod's own top
finisher is always the first member of that pod encountered walking the full
overall order (both are drawn from the same single total order), so "first
remaining player from the other pod" and "that pod's own winner" are always
the same player, and "everyone else in original order" is already relative
order among the remaining players. `assign_seeds_8_team` just needs its
hardcoded `len(seeding) != 8` guard relaxed to also accept 6 — the algorithm
itself carries over unchanged. (Whether to rename it to something
size-neutral like `assign_seeds_two_pod`, with `assign_seeds_6_team`/
`assign_seeds_8_team` as thin length-checked wrappers, is an implementation
detail for that session.)

**Bracket** (3 playoff weeks, 7 games) — this is the existing
`EIGHT_TEAM_BRACKET` with its "7-seed vs 8-seed" placement game (and seeds
7/8 themselves) removed; every other game, round, and placement maps 1:1:

```python
SIX_TEAM_BRACKET: list[GameSpec] = [
    GameSpec(1, 1, seed(3), seed(6)),                                        # POG1
    GameSpec(2, 1, seed(4), seed(5)),                                        # POG2
    GameSpec(3, 2, loser_of(1), loser_of(2), winner_place=5, loser_place=6), # POG3
    GameSpec(4, 2, seed(1), winner_of(2)),                                   # POG4
    GameSpec(5, 2, seed(2), winner_of(1)),                                   # POG5
    GameSpec(6, 3, loser_of(4), loser_of(5), winner_place=3, loser_place=4), # POG6
    GameSpec(7, 3, winner_of(4), winner_of(5), winner_place=1, loser_place=2),# POG7
]
```

Week 1: seeds 3-6 play (POG1, POG2); seeds 1-2 bye. Week 2: the two week-1
losers play each other for 5th/6th (POG3); seeds 1 and 2 join, each facing
the other bracket's week-1 winner (POG4, POG5). Week 3: POG4/POG5 losers play
for 3rd/4th (POG6), POG4/POG5 winners play the championship for 1st/2nd
(POG7). Matches `placement.csv` exactly (1=POG7 winner, 2=POG7 loser, 3=POG6
winner, 4=POG6 loser, 5=POG3 winner, 6=POG3 loser).

**Exhibition game.** The final week (week 3) leaves the 5th/6th finishers —
already decided in week 2 — with nothing scheduled. That's exactly the
"2 idle players" case `find_exhibition_pairing` already handles (today's
4-team format uses the same branch), so **no change needed** to that
function; `EXHIBITION_GAME_COUNT['six_team'] = 1`.

**Implemented (2026-08-09) — see step 3 in "Suggested sequencing" below for
the full change list.** `SIX_TEAM_BRACKET` added to `BRACKET_SPECS`;
`bracket_format_for_pod_count` replaced with `bracket_format_for_league_shape
(n_pods, n_players)`, keyed on the full `(n_pods, n_players)` combination
rather than pod count alone; `assign_seeds_8_team`'s algorithm extracted into
a size-agnostic `_assign_seeds_two_pod()` shared with the new
`assign_seeds_6_team`; `compute_final_placements` generalized off
`BRACKET_SPECS[bracket_format]`; all 3 script call sites and
`seed_playoffs.py`'s dispatch updated. `lib/bracket_engine.py` needed no
changes, as expected (confirmed genuinely format-agnostic).

### Dependency worth flagging now, not when we get there

`scripts/generate_pvp_schedule.py` computes
`regular_season_weeks = season_weeks - playoff_rounds`, which needs
`bracket_format` resolved first. With the design above, `playoff_rounds` for
`six_team` is **3** (same as `eight_team`) — so once
`bracket_format_for_player_count` exists, PVP scheduling (step 3 below) is no
longer blocked on undecided playoff design; it just needs the rekeyed
dispatch function to exist first.

## Docs & tests — status (updated 2026-08-09, most of this is now done)

- ~~`docs/commissioner_checklist.md`~~ **Done, see step 7.**
- **`tests/test_draft.py` — still open, the one real remaining gap.**
  `BASE_ORDER = [10, 20, 30, 40]` (4-player fixture); several assertions are
  4-length-specific. `lib/draft.py` itself needs no change (confirmed back in
  "Good news" above — it's pure position math off `len(base_order)`), but
  there's still no test proving that generically for a 3-player case —
  everything else in this list got 3-player-pod coverage along the way, this
  file didn't.
- ~~`tests/test_scheduling.py`~~ **Done, see step 4** — gained
  `pod_of_player`-labeled-matchup-type coverage. Correction to this doc's
  earlier note: the old two-pod-specific tests (`bipartite_round_robin`/
  `interleave_pvp_weeks`) were **kept, not deleted** — that tiered path is
  still live for `eight_team`, per the "keep 4-player-pod logic in place"
  instruction, so its tests still apply.
- ~~`tests/test_metagame.py`, `tests/test_playoffs.py`,
  `tests/test_playoff_integration.py`, `tests/test_draft_integration.py`~~
  **Done** — 3-player-pod / 6-total-player fixtures added throughout (steps
  1, 3, 4, 5, and the playoff follow-up checklist items).
- ~~`README.md`~~ **Done, see step 7.**

Also done: the `CLAUDE.md` "What this is" section (previously noted here as
stale) was rewritten in step 7 to describe the live season and the
codebase's supported formats separately, rather than stating the 4-player/
8-player shape as the only one.

## Suggested sequencing

1. ~~**Decide the roster-size config mechanism**~~ **Done (2026-08-09).**
   `seasons.draft_picks_per_player` column added (live Aiven migrated, both
   real seasons backfilled to 25 — unchanged behavior). `lib/db.py:
   get_draft_picks_per_player()` added; `set_draft_order()` reads it instead
   of importing a constant. `lib/config.py`'s `DRAFT_PICKS_PER_PLAYER` removed
   (only call site). `scripts/init_season.py --draft-picks-per-player` (default
   25) sets it at season-init time. Comment-only "25-team" references fixed in
   `lib/optimal_lineup.py`/`lib/performance.py`/`ingest_draft.py`. New
   integration test `test_set_draft_order_uses_season_draft_picks_per_player`
   (synthetic season, `draft_picks_per_player=30`, 3-player pod, 90 slots).
   Full suite verified against live Aiven: 207 unit + 12 integration passing.
2. ~~**Draft & roster**~~ Folded into step 1 above — same commit of work,
   no separate step needed in practice.
3. ~~**Playoff dispatch rekey**~~ **Done (2026-08-09).** Turned out step 3 and
   step 6 (below) couldn't be split cleanly — `seed_playoffs.py`'s
   `assign_seeds_*` dispatch needed the relaxed length guard to actually work
   for `six_team`, so both landed together:
   - `lib/playoffs.py`: added `SIX_TEAM_BRACKET` (verified 1:1 against
     `playoff_games.csv`/`placement.csv`, game numbers match POG1-7 directly),
     `BRACKET_SPECS['six_team']`, `PLAYOFF_ROUND_COUNT['six_team']=3`,
     `EXHIBITION_GAME_COUNT['six_team']=1`. Extracted `_assign_seeds_two_pod()`
     (the actual algorithm, size-agnostic) out of `assign_seeds_8_team`, which
     is now a thin `len==8` guard + delegate; added `assign_seeds_6_team` as
     the same pattern with `len==6`. Replaced `bracket_format_for_pod_count`
     with `bracket_format_for_league_shape(n_pods, n_players)`, keyed on a
     `(n_pods, n_players) -> format` dict (`{(1,4): 'four_team', (2,6):
     'six_team', (2,8): 'eight_team'}`) rather than pod count alone — this is
     the piece that actually satisfies "don't re-refactor if 4-player pods
     come back": going from 2-pods-of-3 to 2-pods-of-4 next season resolves to
     `eight_team` automatically, no code change. `compute_final_placements`
     generalized off `BRACKET_SPECS[bracket_format]` instead of hardcoding
     "not four_team → eight_team". `find_exhibition_pairing`'s docstring
     updated (no logic change — six_team's 2-idle-player final week already
     matched the existing four_team branch).
   - Updated all 3 call sites (`generate_pvp_schedule.py`, `seed_playoffs.py`,
     `advance_playoffs.py`) to compute `n_players` before the format lookup
     and use the new function; `seed_playoffs.py` gained a `six_team` branch
     calling `assign_seeds_6_team`.
   - `tests/test_playoffs.py`: renamed/expanded the format-dispatch test class,
     added `TestAssignSeeds6Team` (mirrors `TestAssignSeeds8Team`'s two key
     scenarios at 3-player-pod scale) and a `six_team` case to
     `TestComputeFinalPlacements`. All verified against live Aiven.
4. ~~**PVP scheduling**~~ **Done (2026-08-09).** `four_team`/`eight_team`
   scheduling shapes are byte-for-byte unchanged — per your instruction to
   keep the 4-player-pod logic in place, this landed as an *addition*, not a
   replacement:
   - `lib/scheduling.py: build_pvp_schedule` gained an optional
     `pod_of_player` param, usable only alongside `single_pod_players`. When
     given, each pair's `matchup_type` is derived from pod membership
     (`'in_pod'`/`'cross_pod'`) instead of the hardcoded `'in_pod'` a true
     single-pod season already used. `pod_a`/`pod_b` (the tiered two-pod
     path) untouched. This is how the confirmed 6-player design ("everyone
     plays everyone twice, regardless of pod") gets built: pass all 6
     players from both pods as one flat `single_pod_players` list with
     `single_pod_repeats=2` — no new round-robin machinery needed, since
     mechanically it's identical to the existing single-pod path.
   - `scripts/generate_pvp_schedule.py`: new `--six-team-repeats` CLI flag
     (default 2, separate from `--single-pod-repeats`/`--two-pod-in-pod-repeats`
     since the three scenarios have different confirmed repeat counts); new
     `get_pod_of_player()` helper (mirrors the existing per-script pattern in
     `seed_playoffs.py`/`advance_playoffs.py`); `main()`'s branching rekeyed
     from `if len(pods) == 1` to a 3-way branch on `bracket_format`
     (`'four_team'`/`'six_team'`/else), since pod count alone no longer
     determines scheduling shape.
   - Tests: `tests/test_scheduling.py` gained 2 new cases (labeled
     matchup_type with `pod_of_player`, and a backward-compat check that
     omitting it still labels everything `'in_pod'`). New integration test
     `test_six_team_pvp_schedule_writes_correct_matchup_types` in
     `tests/test_playoff_integration.py` (new 2-pod-of-3 synthetic-season
     fixture) confirms the real DB write path — `matchup_pairings`' ENUM +
     unique key — handles the new shape correctly (30 rows, 12 in_pod/18
     cross_pod, verified against live Aiven).
   - Full suite verified against live Aiven: 209 unit + 13 integration passing.
5. ~~**Regular-season standings**~~ **Done (2026-08-09).** Confirmed
   `points_map = [5,4,3,2,1,0]` for 6 total players (Zach — same "1 point per
   spot" logic as season 8, `n-r` for `n=6`). `compute_pod_standings` needed
   zero code changes, as predicted; verified with 2 new 3-player-pod unit
   tests in `tests/test_metagame.py`. See "Regular-season standings" section
   above for the full writeup. Full suite: 211 unit tests passing.
6. ~~**Playoff bracket/seeding implementation**~~ Absorbed into step 3 above.
7. **Docs — partially done (2026-08-09).**
   - ~~`docs/commissioner_checklist.md`~~ **Done.** `--draft-picks-per-player`
     documented in step 1; pod count/size independence + confirmed
     `(n_pods, n_players)` shapes documented in step 3; a parallel 6-player
     `scoring_contexts` example (with the confirmed `[5,4,3,2,1,0]` ladder)
     added alongside the existing 8-player one in step 5; `--six-team-repeats`
     documented in step 8; `seed_playoffs.py`'s step 1 wording fixed
     (`six_team`/`eight_team`, not just "the 8-team format").
   - ~~`CLAUDE.md`~~ **Done.** "What this is" rewritten to describe the *live*
     season (still real, still 8 players/2×4 pods) separately from what the
     *codebase* now supports (also 2×3/6-player, not yet used by a real
     season) — was previously stated as if 8-players-fixed was the only
     shape. Also fixed earlier in this session: the `lib/config.py`
     `DRAFT_PICKS_PER_PLAYER` reference in "Current state," and a new
     "Settled architecture decisions" bullet recording the
     configurability requirement itself.
   - ~~`README.md`~~ **Done.** Was byte-for-byte identical to
     `secrets/README.md` (confirmed via `diff`) — an accidental overwrite of
     the real project README that predated this session, not something done
     here. Restored via `git checkout HEAD -- README.md` (Zach confirmed
     after reviewing the diff), then "The actual game" section updated with
     the same "season-configured roster size, pod count *and* size both
     drive the bracket format" framing used in `CLAUDE.md`.

## Follow-up checklist items — both done (2026-08-09)

The two items flagged as open at the end of step 7 (a full six_team pipeline
integration test, and live verification of the weekly-results write path):

- **Full six_team pipeline test** — `test_full_six_team_pipeline_end_to_end`
  in `tests/test_playoff_integration.py`, mirroring
  `test_full_four_team_pipeline_end_to_end`'s structure exactly: engineered
  a clean, tie-free regular-season record (pod A sweeps overall ranks 1-3,
  pod B takes 4-6) via a hand-verified circle-method schedule, walked
  seeding → `resolve_seed_placeholders` → all 3 playoff weeks
  (`resolve_playable_games`/`propagate_bracket`) → the final week's
  exhibition game → `maybe_finalize_placements`, asserting exact expected
  state at every step. Deliberately used a non-trivial pod composition (pod
  A holding the top 3 overall finishers) specifically to exercise
  `assign_seeds_6_team`'s pod-bump rule (seed 2 must be pod B's own best,
  not overall #2) through the *real* DB-backed pipeline, not just the pure
  unit tests in `tests/test_playoffs.py`.
  - **Real bug found and fixed, not just proven absent:**
    `playoff_games.bracket_format` was still `ENUM('four_team', 'eight_team')`
    on live Aiven — step 3's playoff-dispatch rekey added `'six_team'` to
    every Python-side dict/constant but missed the actual MySQL ENUM
    constraint, since nothing had tried to write a `six_team` row until this
    test ran. First run failed with `Data truncated for column
    'bracket_format'`. Fixed live (Zach confirmed first):
    `ALTER TABLE playoff_games MODIFY COLUMN bracket_format ENUM('four_team',
    'six_team', 'eight_team') NOT NULL ...` — purely additive, existing rows
    unaffected — and `db/schema.sql` updated to match. This is exactly the
    kind of gap "verify live, not just unit tests" (already this repo's
    stated practice, see the `MARGIN_SCALE` bug in `CLAUDE.md`) is meant to
    catch — the six_team format had 209+ passing unit tests and one
    live-verified schedule-writing test before this, all green, and still
    had a real live-schema gap no test had reached yet.
- **Live weekly-results write path** —
  `test_compute_weekly_standings_writes_match_play_pod_results_for_six_players`,
  same file: mutates the shared fixture's own `scoring_context` row to
  `scoring_type='match_play_pod'`/`points_map='[5,4,3,2,1,0]'` (rather than
  inserting a second active context, which would make
  `get_scoring_context()`'s `LIMIT 1` pick nondeterministic), calls
  `compute_weekly_standings()` for real (exercising the real
  `get_scoring_context()` DB read + JSON `points_map` parse, not a
  hand-built `ctx` dict like `tests/test_metagame.py`'s unit tests use),
  then `write_weekly_results()` and reads back the 4 match_play_pod-only
  columns from `weekly_results` directly. Passed on the first run — no code
  gap here, unlike the bracket_format one above.
- Fixed the `synthetic_six_team_season` fixture's teardown along the way: it
  was missing `playoff_games`/`season_standings`/`weekly_results` deletes
  (added in step 4, only ever populated by the two tests above), which would
  have hit an FK violation on `DELETE FROM players` the moment either test
  used it. Caught before it could leave orphaned rows, not after.
- Full suite verified against live Aiven: 211 unit + 15 integration passing,
  confirmed zero leftover synthetic rows (seasons 9997/9998/9999) and real
  seasons (1, 8) unaffected.

## Suggested sequencing (continued)

8. ~~**Playoff logic redesign for the 6-player format**~~ **Done (2026-08-09).**
   Originally called out as explicitly shelved for a later conversation — that
   call is reversed: the bracket shape and seeding rule were established from
   `playoff_games.csv`/`placement.csv` (see "Playoff logic — 6-team bracket"
   above), implemented (`SIX_TEAM_BRACKET`, `assign_seeds_6_team`,
   `bracket_format_for_league_shape`, step 3), and verified end-to-end with a
   full seed→advance→placements pipeline test plus a live schema fix (see
   "Follow-up checklist items" above). Nothing playoff-related is deferred
   anymore.

## Remaining open items

~~`tests/test_draft.py`~~ **Done (2026-08-09).** Added `BASE_ORDER_3 = [100,
200, 300]` and 9 parallel 3-player-pod tests across `TestRoundOrder`,
`TestSlotForPick`, `TestGenerateFullDraftOrder` (`test_n3_picks_per_player_30`
— the confirmed real 3-player-pod draft length, 90 slots, not just an
arbitrary smaller number), and `TestIsDoublePick`. All passed on the first
run — confirms `lib/draft.py`'s pure position math holds for 3 players
exactly as predicted. Full suite: 219 unit tests passing.

**Nothing else is currently open.** Every item tracked in this document —
draft/roster config, playoff dispatch, PVP scheduling, regular-season
standings, playoff logic/bracket/seeding, docs, and test coverage across
every touched file — is done and verified (unit + live Aiven integration
where applicable). The only work explicitly out of scope was never claimed
otherwise: the game structure itself (16-slot weekly lineup) was never in
scope, per the very first message of this transition.
