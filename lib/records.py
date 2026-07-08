"""
lib/records.py
---------------
Detects new league records against the seeded record_types (see
migration/records_2026.sql) — T21.1.

check_weekly_records() is pure computation: given this week's already-
scored picks (from lib.scoring.compute_player_totals) and the current
best-known value per record_type (read from the DB), it returns which
record_types were beaten — it does NOT write anything. write_records()
does the actual INSERT. Callers (scripts/scoring_engine.py's
finalize_week()) decide when to run the check and whether/when to push
a WordPress update from the result — kept separate the same way
lib.metagame's compute/write functions are split.

Record semantics (extracted from the live "Records and Awards" page —
see migration/records_2026.sql for the full list with descriptions):
  - Per-player, week-scoped: Best/Worst Week (net total), Best Offensive/
    Defensive Week (raw points scored/allowed across the week's games),
    Bad Luck / Really Bad Luck (high net total without winning / without
    finishing top-2), Good Luck (lowest winning net total), Nailbiter/
    Blowout (winner's % share of the week's total net points), Big 12 /
    "What's a Forward Pass" (winner's points allowed/scored).
  - Per-player, game-scoped: Biggest Win / Biggest Loss (single-game
    margin; context = the team that produced it).
  - League-wide, week-scoped, no single player: Shootout / Defensive
    Battle (sum of all players' net totals for the week).
"""

from typing import Optional

from lib.db import get_connection

_FORWARD_PASS_AWARD = '"What\'s a Forward Pass" Award'


def get_current_bests() -> dict[str, dict]:
    """
    Return {record_name: {record_type_id, better_direction, value_type,
    scope, current_value}}. current_value is None if no instance of that
    record_type has been recorded yet (anything beats "no record").
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name, scope, better_direction, value_type FROM record_types")
        types = cur.fetchall()

        bests = {}
        for t in types:
            order = 'DESC' if t['better_direction'] == 'higher' else 'ASC'
            cur.execute(
                f"SELECT value FROM records WHERE record_type_id = %s ORDER BY value {order} LIMIT 1",
                (t['id'],),
            )
            row = cur.fetchone()
            bests[t['name']] = {
                'record_type_id': t['id'],
                'better_direction': t['better_direction'],
                'value_type': t['value_type'],
                'scope': t['scope'],
                'current_value': float(row['value']) if row else None,
            }
        cur.close()
    return bests


def get_records_table_rows() -> list[dict]:
    """
    Return one display-ready row per record_type, in seeded id order —
    matching the live "Records and Awards" page's exact row order. Distinct
    from get_current_bests(): that returns bare values keyed by name for
    record-detection; this returns full rows for rendering the table
    (scripts/html_records.py).

    Each row: {name, description, value_type, value, player_name
    ('All' for a league-wide record with no single player_id holder),
    period, context}. period is records.legacy_period verbatim for
    pre-2026 entries, or "{season year} Week {week}" once a real
    season_id-backed record is set. value/player_name/period are None
    if nothing has ever been recorded for that type (kept for the
    caller to decide how to render an empty row, rather than this
    function guessing).
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, name, description, better_direction, value_type FROM record_types ORDER BY id"
        )
        types = cur.fetchall()

        rows = []
        for t in types:
            order = 'DESC' if t['better_direction'] == 'higher' else 'ASC'
            cur.execute(
                f"""
                SELECT r.value, r.context, r.legacy_period, r.week,
                       pl.name AS player_name, s.year AS season_year
                FROM records r
                LEFT JOIN players pl ON pl.id = r.player_id
                LEFT JOIN seasons s ON s.id = r.season_id
                WHERE r.record_type_id = %s
                ORDER BY r.value {order}
                LIMIT 1
                """,
                (t['id'],),
            )
            best = cur.fetchone()

            value = player_name = period = context = None
            if best is not None:
                value = float(best['value'])
                context = best['context']
                player_name = best['player_name'] or 'All'
                if best['legacy_period']:
                    period = best['legacy_period']
                elif best['season_year'] is not None:
                    period = f"{best['season_year']} Week {best['week']}"

            rows.append({
                'name': t['name'],
                'description': t['description'],
                'value_type': t['value_type'],
                'value': value,
                'player_name': player_name,
                'period': period,
                'context': context,
            })
        cur.close()
    return rows


def _beats(direction: str, candidate: float, current: Optional[float]) -> bool:
    if current is None:
        return True
    return candidate > current if direction == 'higher' else candidate < current


def check_weekly_records(
    season_id: int,
    week: int,
    player_totals: dict[int, dict],
) -> list[dict]:
    """
    Compare this week's results against the current record bests.

    player_totals: from lib.scoring.compute_player_totals() — each slot
    dict must carry 'picked_score'/'opp_score' (added by score_picks).

    Returns a list of candidate new-record dicts (record_name, player_id,
    value, context, season_id, week) — one per record_type beaten this
    week. Empty if nothing changed. Does not write to the DB.
    """
    bests = get_current_bests()
    candidates: list[dict] = []

    def _maybe(record_name, player_id, value, context=None):
        info = bests[record_name]
        if _beats(info['better_direction'], value, info['current_value']):
            candidates.append({
                'record_name': record_name,
                'player_id': player_id,
                'value': value,
                'context': context,
                'season_id': season_id,
                'week': week,
            })

    week_totals = {pid: data['total'] for pid, data in player_totals.items()}
    ranked = sorted(week_totals.items(), key=lambda kv: kv[1], reverse=True)

    for pid, data in player_totals.items():
        _maybe('Best Week', pid, data['total'])
        _maybe('Worst Week', pid, data['total'])

        played = [s for s in data['slots'] if not s['is_pass']]
        scored_for = sum(s.get('picked_score') or 0 for s in played)
        scored_against = sum(s.get('opp_score') or 0 for s in played)
        _maybe('Best Offensive Week', pid, scored_for)
        _maybe('Best Defensive Week', pid, scored_against)

        # Biggest Win / Biggest Loss — per single game, not per week
        for s in played:
            if s.get('margin') is None:
                continue
            _maybe('Biggest Win', pid, s['margin'], context=s['team_name'])
            _maybe('Biggest Loss', pid, s['margin'], context=s['team_name'])

    if ranked:
        winner_id, winner_total = ranked[0]
        winner_slots = [s for s in player_totals[winner_id]['slots'] if not s['is_pass']]

        _maybe('Good Luck Award', winner_id, winner_total)

        winner_scored = sum(s.get('picked_score') or 0 for s in winner_slots)
        winner_allowed = sum(s.get('opp_score') or 0 for s in winner_slots)
        _maybe('Big 12 Award', winner_id, winner_allowed)
        _maybe(_FORWARD_PASS_AWARD, winner_id, winner_scored)

        week_sum = sum(week_totals.values())
        if week_sum:
            winner_pct = round(winner_total / week_sum * 100, 2)
            _maybe('Nailbiter Award', winner_id, winner_pct)
            _maybe('Blowout Award', winner_id, winner_pct)

        for pid, total in ranked[1:]:
            _maybe('Bad Luck Award', pid, total)

        if len(ranked) >= 3:
            third_id, third_total = ranked[2]
            _maybe('Really Bad Luck Award', third_id, third_total)

    # League-wide, no single player holder
    total_all = sum(week_totals.values())
    _maybe('Shootout', None, total_all)
    _maybe('Defensive Battle', None, total_all)

    return candidates


def write_records(candidates: list[dict]) -> int:
    """
    Insert candidate new-record rows (from check_weekly_records) into
    `records`. Returns the number of rows inserted.
    """
    if not candidates:
        return 0

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM record_types")
        type_id_by_name = {r['name']: r['id'] for r in cur.fetchall()}

        for c in candidates:
            cur.execute(
                """
                INSERT INTO records (record_type_id, season_id, week, player_id, value, context)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    type_id_by_name[c['record_name']],
                    c['season_id'],
                    c['week'],
                    c['player_id'],
                    c['value'],
                    c.get('context'),
                ),
            )
        conn.commit()
        cur.close()
    return len(candidates)
