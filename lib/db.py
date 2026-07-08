"""
Database connection helpers and data-access functions for the
CFB Game Streamlit app.

Connects to Aiven MySQL using credentials and CA cert content stored in
Streamlit secrets (.streamlit/secrets.toml locally, or the secrets UI on
Streamlit Cloud).
"""

import atexit
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import mysql.connector
import streamlit as st

from lib.game_context import GameLocation, TeamGameContext
from lib.game_rules import Pick, SlotType


# Cache the CA cert path for the lifetime of the process. The cert content
# lives in st.secrets, but mysql-connector-python needs an actual file path,
# so we write it to a temp file once.
_CA_CERT_PATH: Optional[str] = None


def _get_ca_cert_path() -> str:
    """Materialize the CA cert from st.secrets into a temp file; cached."""
    global _CA_CERT_PATH
    if _CA_CERT_PATH is not None and os.path.exists(_CA_CERT_PATH):
        return _CA_CERT_PATH

    cert_content = st.secrets["aiven_mysql"]["ssl_ca"]
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    tmp.write(cert_content)
    tmp.close()

    _CA_CERT_PATH = tmp.name
    atexit.register(_cleanup_ca_cert)
    return _CA_CERT_PATH


def _cleanup_ca_cert() -> None:
    """Remove the temp CA cert file on process exit."""
    global _CA_CERT_PATH
    if _CA_CERT_PATH and os.path.exists(_CA_CERT_PATH):
        try:
            os.unlink(_CA_CERT_PATH)
        except OSError:
            pass


def _connection_config() -> dict:
    """Build connection kwargs from st.secrets."""
    cfg = st.secrets["aiven_mysql"]
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password": cfg["password"],
        "database": cfg["database"],
        "ssl_ca": _get_ca_cert_path(),
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
        "connection_timeout": 10,
        "autocommit": False,
    }


@contextmanager
def get_connection():
    """
    Context manager yielding a live MySQL connection, closed on exit
    (even on exception). NOTE: closing does not commit — write paths must
    call conn.commit() explicitly before the block exits.
    """
    conn = mysql.connector.connect(**_connection_config())
    try:
        yield conn
    finally:
        conn.close()


def healthcheck() -> dict:
    """Run a trivial query to confirm the connection works."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION(), DATABASE(), NOW()")
                version, database, now = cur.fetchone()
        return {
            "ok": True,
            "mysql_version": version,
            "database": database,
            "server_time": str(now),
        }
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


def get_players() -> list[dict]:
    """
    Return all players as [{id, name, username, is_commissioner}, ...].

    Dev-seam helper for the player picker until auth.py exists; later, auth
    resolves the logged-in username to a single player_id and this is only
    needed for admin/commissioner views.
    """
    query = """
        SELECT id, name, username, is_commissioner
        FROM players
        ORDER BY name
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
    return rows


def get_player_by_username(username: str, season_id: int) -> Optional[dict]:
    """
    Return {id, name, username, is_commissioner, pod_id} for the given
    username, or None. pod_id is this player's pod *for season_id*
    specifically (via pod_memberships) -- pod membership is a per-season
    fact, not a permanent one, since pods themselves are re-formed each
    season (see pod_memberships' docstring in schema.sql). A player with
    no membership row for this season gets pod_id=None back rather than
    raising, so a not-yet-assigned player can still log in.
    """
    query = """
        SELECT pl.id, pl.name, pl.username, pl.is_commissioner, pm.pod_id
        FROM players pl
        LEFT JOIN pod_memberships pm ON pm.player_id = pl.id AND pm.season_id = %s
        WHERE pl.username = %s
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, username))
        row = cur.fetchone()
        cur.close()
    return row


@st.cache_data(ttl=300)
def get_roster_with_context(
    player_id: int,
    season_id: int,
    week: int,
) -> list[TeamGameContext]:
    """
    Return TeamGameContext for every team on the given player's roster,
    enriched with this week's game info (opponent, location, etc.).

    Teams on bye this week are included with opponent fields set to None.
    """
    query = """
        SELECT
            t.id                  AS team_id,
            t.name_display        AS name,
            t.conference_id       AS conference_id,
            c.abbreviation        AS conference_abbreviation,
            c.tier                AS tier,
            v.game_id             AS game_id,
            v.opponent_id         AS opponent_team_id,
            v.opponent_name       AS opponent_name,
            v.location            AS location
        FROM rosters r
        JOIN teams t
            ON t.id = r.team_id
        JOIN conferences c
            ON c.id = t.conference_id
        LEFT JOIN v_team_weekly_schedule v
            ON v.team_id = t.id
            AND v.season_id = %s
            AND v.week = %s
        WHERE r.player_id = %s
            AND r.season_id = %s
            AND r.is_active = 1
        ORDER BY c.abbreviation, t.name_display
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, week, player_id, season_id))
        rows = cur.fetchall()
        cur.close()

    results = []
    for row in rows:
        location = GameLocation(row["location"]) if row["location"] else None
        results.append(TeamGameContext(
            team_id=row["team_id"],
            name=row["name"],
            conference_id=row["conference_id"],
            conference_abbreviation=row["conference_abbreviation"],
            tier=row["tier"],
            game_id=row["game_id"],
            opponent_team_id=row["opponent_team_id"],
            opponent_name=row["opponent_name"],
            location=location,
        ))
    return results


# -----------------------------------------------------------------
# Drop / add
# -----------------------------------------------------------------
# Not cached like the read helpers above — these back a low-frequency,
# correctness-sensitive commissioner/player workflow, not a per-click UI
# loop, so staleness isn't worth risking for the connection savings.

def get_active_roster_teams(player_id: int, season_id: int) -> list[dict]:
    """
    Return this player's currently active roster teams (no game/week
    context) — the drop-candidate pool for a drop/add request.
    """
    query = """
        SELECT
            t.id             AS team_id,
            t.name_display   AS name,
            c.abbreviation   AS conference_abbreviation,
            c.tier           AS tier
        FROM rosters r
        JOIN teams t ON t.id = r.team_id
        JOIN conferences c ON c.id = t.conference_id
        WHERE r.player_id = %s
          AND r.season_id = %s
          AND r.is_active = 1
        ORDER BY c.abbreviation, t.name_display
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (player_id, season_id))
        rows = cur.fetchall()
        cur.close()
    return rows


def get_available_teams(season_id: int, pod_id: int) -> list[dict]:
    """
    Return FBS teams with no active roster owner in this pod — the
    add-candidate pool for a drop/add request. Pod-scoped, not season-
    scoped: pods are completely severed rosters (Zach, 2026-07-05), each
    with its own independent claim on the full team universe, so a team
    owned in another pod is still available here.
    """
    query = """
        SELECT
            t.id             AS team_id,
            t.name_display   AS name,
            c.abbreviation   AS conference_abbreviation,
            c.tier           AS tier
        FROM teams t
        JOIN conferences c ON c.id = t.conference_id
        WHERE t.season_id = %s
          AND t.level = 'FBS'
          AND t.id NOT IN (
              SELECT team_id FROM rosters
              WHERE season_id = %s AND pod_id = %s AND is_active = 1
          )
        ORDER BY c.abbreviation, t.name_display
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, season_id, pod_id))
        rows = cur.fetchall()
        cur.close()
    return rows


def get_team_week_results(season_id: int, week: int, team_ids: Optional[list[int]] = None) -> list[dict]:
    """
    Return [{team_id, name, conference_abbreviation, tier, margin}, ...]
    for a given (already-played) week -- the input shape
    lib.optimal_lineup.optimize_lineup() expects (as TeamWeekResult
    fields). margin is None for a team with no game that week (a bye),
    which the optimizer treats the same as a non-positive margin: never
    worth using over a pass.

    team_ids: scope to this specific pool (e.g. one player's roster, or
    a pod's free-agent pool). None means every FBS team in the season --
    the "Max" hypothetical player with no roster constraint at all.
    """
    query = """
        SELECT
            t.id             AS team_id,
            t.name_display   AS name,
            c.abbreviation   AS conference_abbreviation,
            c.tier           AS tier,
            g.home_team_id   AS home_team_id,
            g.away_team_id   AS away_team_id,
            g.home_score     AS home_score,
            g.away_score     AS away_score
        FROM teams t
        JOIN conferences c ON c.id = t.conference_id
        LEFT JOIN games g
            ON (g.home_team_id = t.id OR g.away_team_id = t.id)
           AND g.season_id = t.season_id
           AND g.week = %s
        WHERE t.season_id = %s
          AND t.level = 'FBS'
    """
    params = [week, season_id]
    if team_ids is not None:
        if not team_ids:
            return []
        placeholders = ", ".join(["%s"] * len(team_ids))
        query += f" AND t.id IN ({placeholders})"
        params.extend(team_ids)

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()

    results = []
    for r in rows:
        if r['home_score'] is None or r['away_score'] is None:
            margin = None
        elif r['team_id'] == r['home_team_id']:
            margin = r['home_score'] - r['away_score']
        else:
            margin = r['away_score'] - r['home_score']
        results.append({
            'team_id': r['team_id'],
            'name': r['name'],
            'conference_abbreviation': r['conference_abbreviation'],
            'tier': r['tier'],
            'margin': margin,
        })
    return results


@st.cache_data(ttl=300)
def get_pods(season_id: int) -> list[dict]:
    """Return [{id, name}, ...] for every pod in this season."""
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, name FROM pods WHERE season_id = %s ORDER BY id", (season_id,))
        rows = cur.fetchall()
        cur.close()
    return rows


@st.cache_data(ttl=300)
def get_pod_players(pod_id: int) -> list[dict]:
    """Return [{id, name}, ...] for every player in this pod, name-sorted."""
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT pl.id, pl.name FROM players pl
            JOIN pod_memberships pm ON pm.player_id = pl.id
            WHERE pm.pod_id = %s
            ORDER BY pl.name
            """,
            (pod_id,),
        )
        rows = cur.fetchall()
        cur.close()
    return rows


@st.cache_data(ttl=300)
def get_pod_membership_map(season_id: int) -> dict[int, int]:
    """{player_id: pod_id} for every player with a pod assignment in this
    season. Single shared source for anything that needs "which pod is
    this player in" -- replaces the private per-script copies of this
    same query that used to read players.pod_id directly."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT player_id, pod_id FROM pod_memberships WHERE season_id = %s", (season_id,))
        rows = cur.fetchall()
        cur.close()
    return {player_id: pod_id for player_id, pod_id in rows}


def get_matchups_for_week(season_id: int, scoring_context_id: int, week: int) -> list[dict]:
    """
    Return [{id, matchup_type, player_a_id, player_a_name, player_b_id,
    player_b_name, winner_id, is_tie}, ...] for this week's PVP pairings.

    winner_id/is_tie are read straight from matchup_pairings, so they're
    NULL/False until finalize_week()'s resolve_pvp_matchups() runs for
    this week -- fine for both callers: the live status page (T20)
    ignores these two fields entirely and computes a live leader from
    that week's in-progress player_totals instead (matchup_pairings
    isn't resolved yet while a week's still in progress); the recap page
    (built after finalize_week()) reads them directly, since by then
    they're already resolved.
    """
    query = """
        SELECT mp.id, mp.matchup_type,
               mp.player_a_id, pa.name AS player_a_name,
               mp.player_b_id, pb.name AS player_b_name,
               mp.winner_id, mp.is_tie
        FROM matchup_pairings mp
        JOIN players pa ON pa.id = mp.player_a_id
        JOIN players pb ON pb.id = mp.player_b_id
        WHERE mp.season_id = %s AND mp.scoring_context_id = %s AND mp.week = %s
        ORDER BY mp.id
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, scoring_context_id, week))
        rows = cur.fetchall()
        cur.close()
    return rows


def get_lineup_team_ids(season_id: int, week: int) -> dict[int, set[int]]:
    """{player_id: {team_id, ...}} -- the teams actually played (not
    passed) in each player's submitted lineup for this week. A player
    with no submitted lineup simply doesn't appear as a key."""
    query = """
        SELECT wl.player_id, ls.team_id
        FROM lineup_slots ls
        JOIN weekly_lineups wl ON wl.id = ls.lineup_id
        WHERE wl.season_id = %s AND wl.week = %s AND ls.team_id IS NOT NULL
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, (season_id, week))
        rows = cur.fetchall()
        cur.close()
    result: dict[int, set[int]] = {}
    for player_id, team_id in rows:
        result.setdefault(player_id, set()).add(team_id)
    return result


def get_pick_results_by_player(season_id: int, through_week: int) -> dict[int, list[dict]]:
    """
    {player_id: [{'is_pass', 'margin'}, ...]} -- every lineup slot across
    weeks 1..through_week (inclusive), in the shape
    lib.recap.tally_pick_record() expects. Resolves each slot's game the
    same way _load_picks_with_display() does (week-0 elections via
    ls.game_id, everyone else via v_team_weekly_schedule), just
    aggregated across every week instead of scoped to one. margin is
    None if a slot's game somehow has no final score yet -- shouldn't
    happen for an already-finalized week, but not assumed blindly (a
    None margin is excluded from tally_pick_record()'s W/L/T buckets).
    """
    query = """
        SELECT
            wl.player_id,
            ls.team_id,
            COALESCE(g_w0.home_team_id, g_reg.home_team_id) AS home_team_id,
            COALESCE(g_w0.home_score,   g_reg.home_score)   AS home_score,
            COALESCE(g_w0.away_score,   g_reg.away_score)   AS away_score
        FROM lineup_slots ls
        JOIN weekly_lineups wl ON wl.id = ls.lineup_id
        LEFT JOIN games g_w0  ON g_w0.id = ls.game_id
        LEFT JOIN v_team_weekly_schedule v
            ON ls.game_id IS NULL
            AND v.team_id   = ls.team_id
            AND v.season_id = wl.season_id
            AND v.week      = wl.week
        LEFT JOIN games g_reg ON ls.game_id IS NULL AND g_reg.id = v.game_id
        WHERE wl.season_id = %s AND wl.week <= %s
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, through_week))
        rows = cur.fetchall()
        cur.close()

    by_player: dict[int, list[dict]] = {}
    for r in rows:
        is_pass = r['team_id'] is None
        margin = None
        if not is_pass and r['home_score'] is not None and r['away_score'] is not None:
            picked = r['home_score'] if r['home_team_id'] == r['team_id'] else r['away_score']
            opp = r['away_score'] if r['home_team_id'] == r['team_id'] else r['home_score']
            margin = picked - opp
        by_player.setdefault(r['player_id'], []).append({'is_pass': is_pass, 'margin': margin})
    return by_player


def get_pod_vs_pod_bonus_by_week(season_id: int, week: int) -> list[dict]:
    """
    [{week, pod_id, won}, ...] for every (week, pod) through the given
    week that has a computed pod_vs_pod_bonus -- one row per pod per
    week (every player in a pod gets the same bonus that week, so this
    collapses the per-player rows via MAX rather than returning one row
    per player). won is 1/0, read straight from the already-stored
    weekly_results.pod_vs_pod_bonus -- not independently recomputed from
    scratch, so a season "pod vs. pod" record can never contradict the
    actual bonus points a player received that week.
    """
    query = """
        SELECT wr.week, pm.pod_id, MAX(wr.pod_vs_pod_bonus) AS won
        FROM weekly_results wr
        JOIN pod_memberships pm ON pm.player_id = wr.player_id AND pm.season_id = wr.season_id
        WHERE wr.season_id = %s AND wr.week <= %s AND wr.pod_vs_pod_bonus IS NOT NULL
        GROUP BY wr.week, pm.pod_id
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (season_id, week))
        rows = cur.fetchall()
        cur.close()
    return rows


def get_owned_team_ids(season_id: int, pod_id: int) -> set[int]:
    """Return team_ids with an active roster owner in this pod. Pod-scoped
    for the same reason get_available_teams() is -- see its docstring."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT team_id FROM rosters WHERE season_id = %s AND pod_id = %s AND is_active = 1",
            (season_id, pod_id),
        )
        rows = cur.fetchall()
        cur.close()
    return {r[0] for r in rows}


def create_dropadd_request(
    player_id: int,
    season_id: int,
    week: int,
    dropped_team_id: Optional[int],
    added_team_id: Optional[int],
) -> int:
    """Insert a pending drop/add request; returns dropadd_requests.id."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO dropadd_requests
                (player_id, season_id, week, dropped_team_id, added_team_id, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            """,
            (player_id, season_id, week, dropped_team_id, added_team_id),
        )
        request_id = cur.lastrowid
        conn.commit()
        cur.close()
    return request_id


def get_dropadd_requests(season_id: int, status: Optional[str] = None) -> list[dict]:
    """
    Return drop/add requests for this season, optionally filtered by
    status, newest first, joined with player/team display names for the
    commissioner view.
    """
    query = """
        SELECT
            r.id, r.player_id, p.name AS player_name,
            r.week, r.status, r.requested_at, r.decided_at, r.notes,
            r.dropped_team_id, dt.name_display AS dropped_team_name,
            r.added_team_id, at.name_display AS added_team_name
        FROM dropadd_requests r
        JOIN players p ON p.id = r.player_id
        LEFT JOIN teams dt ON dt.id = r.dropped_team_id
        LEFT JOIN teams at ON at.id = r.added_team_id
        WHERE r.season_id = %s
    """
    params: list = [season_id]
    if status is not None:
        query += " AND r.status = %s"
        params.append(status)
    query += " ORDER BY r.requested_at DESC"

    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        cur.close()
    return rows


class DropAddRequestError(Exception):
    """Raised when a drop/add request can't be decided (missing, already decided)."""


def decide_dropadd_request(request_id: int, decided_by: int, approve: bool) -> None:
    """
    Approve or deny a pending drop/add request.

    On approval: deactivates the dropped roster row (if any), inserts the
    added roster row (if any) as a free_agent acquisition, and logs the
    completed transaction to drop_add_log — all in the same transaction as
    the status update. Raises DropAddRequestError if the request is missing
    or already decided.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                "SELECT * FROM dropadd_requests WHERE id = %s FOR UPDATE",
                (request_id,),
            )
            req = cur.fetchone()
            if req is None:
                raise DropAddRequestError(f"Drop/add request {request_id} not found.")
            if req["status"] != "pending":
                raise DropAddRequestError(
                    f"Drop/add request {request_id} was already {req['status']}."
                )

            new_status = "approved" if approve else "denied"

            if approve:
                if req["dropped_team_id"] is not None:
                    cur.execute(
                        """
                        UPDATE rosters SET is_active = 0, dropped_at = NOW()
                        WHERE player_id = %s AND team_id = %s AND season_id = %s
                          AND is_active = 1
                        """,
                        (req["player_id"], req["dropped_team_id"], req["season_id"]),
                    )
                if req["added_team_id"] is not None:
                    cur.execute(
                        """
                        INSERT INTO rosters
                            (player_id, team_id, season_id, pod_id, is_active, acquired_at, acquisition_type)
                        SELECT %s, %s, %s, pm.pod_id, TRUE, NOW(), 'free_agent'
                        FROM pod_memberships pm
                        WHERE pm.player_id = %s AND pm.season_id = %s
                        """,
                        (req["player_id"], req["added_team_id"], req["season_id"],
                         req["player_id"], req["season_id"]),
                    )
                cur.execute(
                    """
                    INSERT INTO drop_add_log
                        (player_id, season_id, week, dropped_team_id, added_team_id, actioned_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        req["player_id"], req["season_id"], req["week"],
                        req["dropped_team_id"], req["added_team_id"], decided_by,
                    ),
                )

            cur.execute(
                """
                UPDATE dropadd_requests
                SET status = %s, decided_at = NOW(), decided_by = %s
                WHERE id = %s
                """,
                (new_status, decided_by, request_id),
            )
            conn.commit()
        finally:
            cur.close()

    if approve:
        # Roster changed — invalidate cached roster reads (keyed by week,
        # so a targeted clear isn't possible) so the picks page doesn't
        # keep serving a pre-drop/add roster for up to its 5-minute TTL.
        get_roster_with_context.clear()

        # T19: refresh the local rosters artifact and push the live
        # WordPress page (Zach, 2026-07-05: auto-push on every approval).
        # Imported here (not at module top) to avoid a circular import:
        # html_rosters.py itself uses lib.db's get_connection/roster helpers.
        # Two separate try/excepts so a WordPress hiccup doesn't skip the
        # local file (or vice versa) — neither should ever block the
        # drop/add approval itself from completing.
        try:
            from scripts.html_rosters import write_rosters_page
            write_rosters_page(req["season_id"])
        except Exception as e:
            print(f"[WARNING] Rosters local file refresh failed: {e}")

        try:
            from scripts.html_rosters import push_rosters_page
            push_rosters_page(req["season_id"])
        except Exception as e:
            print(f"[WARNING] Rosters WordPress push failed: {e}")


@st.cache_data(ttl=3600)
def get_conference_slot_tiers(season_id: int) -> dict[str, str]:
    """
    Return {abbreviation: tier} for the conferences that are lineup slots
    this season — the real P4/G6 conferences a player drafts one team from.
    Independent buckets (IndP4/IndG6) and FCS conferences are excluded via
    is_lineup_slot, even though the buckets carry a P4/G6 tier.

    NOTE: game_rules.validate_pick relies on the independent buckets being
    ABSENT from this dict for two things now: routing independents to
    flex/wildcard via tier, AND (settled 2026-07-04) letting a P4/G6
    independent fill ANY conference slot of its own tier. Do not loosen
    the filter to include the independent buckets.
    """
    query = """
        SELECT abbreviation, tier
        FROM conferences
        WHERE season_id = %s
          AND is_lineup_slot = 1
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(query, (season_id,))
        rows = cur.fetchall()
        cur.close()
    return {abbr: tier for abbr, tier in rows}


@st.cache_data(ttl=300)
def get_week0_elections(player_id: int, season_id: int) -> list[dict]:
    """
    Return undeployed week 0 elections for this player as
    [{team_id, game_id, week_deployed, declared}, ...].

    Only rows that have not yet been deployed (week_deployed IS NULL)
    and have not been explicitly declined (declared != 1) are returned.
    """
    query = """
        SELECT team_id, game_id, week_deployed, declared
        FROM week0_elections
        WHERE player_id = %s
          AND season_id = %s
          AND week_deployed IS NULL
          AND declared != 1
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, (player_id, season_id))
        rows = cur.fetchall()
        cur.close()
    return rows


@st.cache_data(ttl=3600)
def get_week0_cutoff_week(season_id: int) -> Optional[int]:
    """Return the last week a player may deploy a banked week 0 game, or None."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT week0_cutoff_week FROM seasons WHERE id = %s", (season_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


@st.cache_data(ttl=3600)
def get_week_date_ranges(season_id: int) -> dict[int, dict]:
    """
    Return {week: {'start_date': date, 'end_date': date}} for every week
    in this season, from v_week_date_ranges (MIN/MAX game_date per week,
    computed live from `games` — see v_week_date_ranges_schema.txt). A
    view rather than a stored table on purpose: kickoff dates/times often
    aren't finalized until close to game day, so a live view can't go
    stale the way a synced copy would (same reasoning as
    v_team_weekly_schedule). Long TTL here since the range itself rarely
    changes once the season's schedule is loaded.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT week, start_date, end_date FROM v_week_date_ranges WHERE season_id = %s",
            (season_id,),
        )
        rows = cur.fetchall()
        cur.close()
    return {r["week"]: {"start_date": r["start_date"], "end_date": r["end_date"]} for r in rows}


def format_week_label(week: int, date_ranges: dict[int, dict]) -> str:
    """
    Human-readable week label with its date range when known, e.g.
    "1 - 9/3-9/7"; falls back to the bare week number otherwise. Shared by
    the picks page and the lineup history page so the format can't drift
    between them.
    """
    r = date_ranges.get(week)
    if not r or not r["start_date"] or not r["end_date"]:
        return str(week)
    start, end = r["start_date"], r["end_date"]
    if start == end:
        return f"{week} - {start.month}/{start.day}"
    return f"{week} - {start.month}/{start.day}-{end.month}/{end.day}"


def _current_week_from_ranges(date_ranges: dict[int, dict], today: date) -> Optional[int]:
    """
    Pure logic (no DB) for T25: which week is "current" given a
    {week: {start_date, end_date}} map and today's date.

    Definition: the earliest week 1+ whose range hasn't fully concluded
    yet (end_date >= today). This means the displayed week advances to
    the next one as soon as the prior week's games finish, even before
    the next week's games start — matching what a player should be
    picking for during the gap between weeks, and defaulting to week 1
    before the season has started. Weeks with no known date range yet
    (e.g. schedule not loaded) are skipped. Returns None once every
    known week has concluded (season over) or no week has dates yet.
    """
    candidates = sorted(
        (w, r) for w, r in date_ranges.items()
        if w >= 1 and r.get("start_date") and r.get("end_date")
    )
    for week, r in candidates:
        if r["end_date"] >= today:
            return week
    return None


@st.cache_data(ttl=3600)
def get_current_week(season_id: int) -> Optional[int]:
    """Return which week is "current" today for this season — see _current_week_from_ranges()."""
    return _current_week_from_ranges(get_week_date_ranges(season_id), date.today())


# -----------------------------------------------------------------
# Lineup read / write
# -----------------------------------------------------------------

@dataclass
class StoredLineup:
    """
    A persisted lineup read back for display / pre-fill.

    slot_assignments maps (slot_type, slot_identifier) ->
    Optional[tuple[int, Optional[int]]] where the tuple is (team_id, game_id)
    and None means the slot was passed. game_id is None for regular current-week
    picks and populated for banked week 0 picks.

    Conference slots use the conference abbreviation as the identifier;
    flex/wildcard slots use a positional "1"/"2"/"3" assigned by stored row
    order (the ordinal is not stored, and the flex slots are interchangeable,
    so position is enough to pre-fill the form).
    """
    lineup_id: int
    is_locked: bool
    submitted_at: Optional[datetime]
    slot_assignments: dict[tuple[str, str], Optional[tuple[int, Optional[int]]]]


def get_teams_by_ids(team_ids: set[int]) -> dict[int, dict]:
    """
    Return {team_id: {name, conference_abbreviation, tier}} for exactly the
    given team ids, regardless of current roster ownership. Used by the
    lineup history page, where a stored pick may reference a team the
    player no longer owns (dropped since that week).
    """
    if not team_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(team_ids))
    query = f"""
        SELECT t.id AS team_id, t.name_display AS name,
               c.abbreviation AS conference_abbreviation, c.tier AS tier
        FROM teams t
        JOIN conferences c ON c.id = t.conference_id
        WHERE t.id IN ({placeholders})
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, tuple(team_ids))
        rows = cur.fetchall()
        cur.close()
    return {r["team_id"]: r for r in rows}


def get_games_by_ids(game_ids: set[int]) -> dict[int, dict]:
    """
    Return {game_id: {home_team_id, away_team_id, home_score, away_score,
    is_neutral, game_date, week, is_week0}} for exactly the given game ids.
    Used by the lineup history page to show opponent/score context for a
    stored pick, including banked week 0 games from earlier than the
    viewed week (identifiable via is_week0 / a week that doesn't match the
    lineup week being displayed).

    Note: games.status predating 2026-07-03 (season 1's historical data) is
    not reliable as a "final" indicator, since it was never backfilled after
    the fact. Weeks finalized via finalize_week() going forward are written
    by update_game_results() below and can be trusted. Callers that need to
    work across both eras should still treat a non-NULL home_score/away_score
    as the final signal rather than trusting status alone.
    """
    if not game_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(game_ids))
    query = f"""
        SELECT id AS game_id, home_team_id, away_team_id,
               home_score, away_score, is_neutral, game_date, week, is_week0
        FROM games
        WHERE id IN ({placeholders})
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(query, tuple(game_ids))
        rows = cur.fetchall()
        cur.close()
    return {r["game_id"]: r for r in rows}


def update_game_results(games: dict[int, dict]) -> int:
    """
    Persist authoritative final scores/status onto the `games` table.

    `games` is keyed by cfbd_game_id, matching the dict shape produced by
    scripts/scoring_engine.py's fetch_live_scores()/_fetch_final_scores():
    {cfbd_game_id: {status, home_points, away_points, ...}}.

    Only rows whose status is 'final' with both scores present are written —
    still-scheduled or in-progress games are left alone here; the live
    scoreboard/checkpoint JSON is the source of truth for those, not this
    table. Rows already marked 'final' are skipped (nothing left to update).

    Returns the number of rows actually updated.
    """
    final_games = {
        gid: g for gid, g in games.items()
        if g.get("status") == "final"
        and g.get("home_points") is not None
        and g.get("away_points") is not None
    }
    if not final_games:
        return 0

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            n = 0
            for cfbd_game_id, g in final_games.items():
                cur.execute(
                    """
                    UPDATE games
                    SET home_score = %s, away_score = %s, status = 'final',
                        last_updated = NOW()
                    WHERE cfbd_game_id = %s AND status <> 'final'
                    """,
                    (g["home_points"], g["away_points"], cfbd_game_id),
                )
                n += cur.rowcount
            conn.commit()
        finally:
            cur.close()
    return n


@st.cache_data(ttl=60)
def get_lineup(player_id: int, season_id: int, week: int) -> Optional[StoredLineup]:
    """
    Return the StoredLineup for this (player, season, week), or None if the
    player has not submitted a lineup for that week.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, is_locked, submitted_at FROM weekly_lineups "
            "WHERE player_id = %s AND season_id = %s AND week = %s",
            (player_id, season_id, week),
        )
        header = cur.fetchone()
        if header is None:
            cur.close()
            return None

        # ORDER BY id preserves insert order, so flex/wildcard rows come back
        # in the order they were saved -> stable positional identifiers.
        cur.execute(
            "SELECT team_id, game_id, slot_type, conference_slug FROM lineup_slots "
            "WHERE lineup_id = %s ORDER BY id",
            (header["id"],),
        )
        slot_rows = cur.fetchall()
        cur.close()

    assignments: dict[tuple[str, str], Optional[tuple[int, Optional[int]]]] = {}
    flex_counters: dict[str, int] = {}
    for row in slot_rows:
        slot_type = row["slot_type"]
        if slot_type == SlotType.CONFERENCE.value:
            identifier = row["conference_slug"]
        else:
            flex_counters[slot_type] = flex_counters.get(slot_type, 0) + 1
            identifier = str(flex_counters[slot_type])
        if row["team_id"] is None:
            assignments[(slot_type, identifier)] = None  # pass
        else:
            assignments[(slot_type, identifier)] = (row["team_id"], row["game_id"])

    return StoredLineup(
        lineup_id=header["id"],
        is_locked=bool(header["is_locked"]),
        submitted_at=header["submitted_at"],
        slot_assignments=assignments,
    )


class LineupLockedError(Exception):
    """Raised when attempting to overwrite a locked lineup."""


def save_lineup(player_id: int, season_id: int, week: int, picks: list[Pick]) -> int:
    """
    Persist a validated 16-slot lineup, overwriting any existing lineup for
    this (player, season, week). Returns weekly_lineups.id.

    Caller must have already run validate_lineup on `picks`. Passes (team=None)
    are stored with team_id = NULL. Raises LineupLockedError if the existing
    lineup is locked. All work is one transaction; nothing persists unless the
    final commit is reached, so a failure mid-write leaves the prior lineup intact.
    """
    with get_connection() as conn:           # close on exit; rolls back if uncommitted
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id, is_locked FROM weekly_lineups "
                "WHERE player_id = %s AND season_id = %s AND week = %s",
                (player_id, season_id, week),
            )
            row = cur.fetchone()

            if row is not None:
                lineup_id, is_locked = row
                if is_locked:
                    raise LineupLockedError(
                        f"Lineup for player {player_id}, week {week} is locked."
                    )
                cur.execute("DELETE FROM lineup_slots WHERE lineup_id = %s", (lineup_id,))
                cur.execute(
                    "UPDATE weekly_lineups SET submitted_at = NOW() WHERE id = %s",
                    (lineup_id,),
                )
            else:
                cur.execute(
                    "INSERT INTO weekly_lineups (player_id, season_id, week, submitted_at) "
                    "VALUES (%s, %s, %s, NOW())",
                    (player_id, season_id, week),
                )
                lineup_id = cur.lastrowid

            slot_rows = [
                (
                    lineup_id,
                    None if pick.is_pass else pick.team.team_id,
                    pick.game_id,
                    pick.slot_type.value,
                    pick.slot_identifier if pick.slot_type == SlotType.CONFERENCE else None,
                )
                for pick in picks
            ]
            cur.executemany(
                "INSERT INTO lineup_slots (lineup_id, team_id, game_id, slot_type, conference_slug) "
                "VALUES (%s, %s, %s, %s, %s)",
                slot_rows,
            )

            # Mark any deployed week 0 elections in the same transaction.
            week0_game_ids = {p.game_id for p in picks if not p.is_pass and p.game_id is not None}
            for gid in week0_game_ids:
                cur.execute(
                    "UPDATE week0_elections SET week_deployed = %s, declared = 1 "
                    "WHERE player_id = %s AND game_id = %s",
                    (week, player_id, gid),
                )

            conn.commit()   # REQUIRED — close-on-exit would otherwise roll this back

            # This write invalidates cached reads of the same data (see the
            # @st.cache_data functions above) — clear them so the next rerun
            # doesn't serve stale pre-write results.
            get_lineup.clear(player_id, season_id, week)
            if week0_game_ids:
                get_week0_elections.clear(player_id, season_id)

            return lineup_id
        finally:
            cur.close()


# ------------------------------------------------------------------
# Live in-app draft
# ------------------------------------------------------------------
#
# draft_order is pre-populated in full (every pick_number, all rounds) at
# commissioner setup time via set_draft_order() -- see lib/draft.py's
# generate_full_draft_order(). "Whose turn is it" is just the lowest
# pick_number row still having team_selected IS NULL, read directly, not
# recomputed from a running count.

@dataclass(frozen=True)
class DraftPickRow:
    pick_number: int
    round_number: int
    pick_in_round: int
    player_id: int
    player_name: str
    team_id: Optional[int]
    team_name: Optional[str]
    conference_abbreviation: Optional[str]
    tier: Optional[str]


@dataclass(frozen=True)
class DraftPageState:
    all_slots: list  # list[DraftPickRow], every pick_number 1..total_picks, made and not-yet-made alike
    available_teams: list  # [{team_id, name, conference_abbreviation, tier}, ...] -- pod-scoped pool

    @property
    def pick_log(self) -> list:
        """Only the slots that have actually been picked -- feeds the rosters-so-far and pick-log sections."""
        return [s for s in self.all_slots if s.team_id is not None]

    @property
    def open_slots(self) -> list:
        """Not-yet-picked slots, still in pick_number order."""
        return [s for s in self.all_slots if s.team_id is None]


@st.cache_data(ttl=60)
def get_draft_page_state(season_id: int, pod_id: int) -> DraftPageState:
    """
    One connection, two queries: the full draft_order schedule for this
    pod (LEFT JOINs so an unmade pick still appears as a row with
    team_id=None, rather than being silently dropped), and the pool of
    teams still undrafted *for this pod specifically* -- each pod drafts
    independently, so a team already taken in another pod is still
    available here.

    NOTE: this pod-scoped availability query is deliberately NOT the same
    as get_available_teams(season_id) above (which is season-scoped, not
    pod-scoped) and isn't simply calling it either (that would open a
    second connection). get_available_teams() has this same latent gap —
    harmless today (one pod), but would need the identical fix the day a
    second pod goes live, for every page that calls it. Out of scope here.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """
            SELECT
                d.pick_number, d.round_number, d.pick_in_round,
                d.player_id, p.name AS player_name,
                d.team_selected AS team_id, t.name_display AS team_name,
                c.abbreviation AS conference_abbreviation, c.tier AS tier
            FROM draft_order d
            JOIN players p ON p.id = d.player_id
            LEFT JOIN teams t ON t.id = d.team_selected
            LEFT JOIN conferences c ON c.id = t.conference_id
            WHERE d.season_id = %s AND d.pod_id = %s
            ORDER BY d.pick_number
            """,
            (season_id, pod_id),
        )
        all_rows = cur.fetchall()

        cur.execute(
            """
            SELECT
                t.id AS team_id, t.name_display AS name,
                c.abbreviation AS conference_abbreviation, c.tier AS tier
            FROM teams t
            JOIN conferences c ON c.id = t.conference_id
            WHERE t.season_id = %s AND t.level = 'FBS'
              AND t.id NOT IN (
                  SELECT team_id FROM rosters
                  WHERE season_id = %s AND pod_id = %s AND is_active = 1
              )
            ORDER BY c.abbreviation, t.name_display
            """,
            (season_id, season_id, pod_id),
        )
        available_rows = cur.fetchall()
        cur.close()

    all_slots = [DraftPickRow(**row) for row in all_rows]
    return DraftPageState(all_slots=all_slots, available_teams=available_rows)


class DraftSetupError(Exception):
    """Raised when the commissioner tries to set the draft order after picks have already been made."""


def set_draft_order(season_id: int, pod_id: int, base_order: list, set_by: int) -> None:
    """
    One-time commissioner write. Raises DraftSetupError if any row for
    this pod already has team_selected IS NOT NULL (a pick has actually
    been made -- too late to reorder). Otherwise deletes any existing
    (fully-open) rows for this pod and bulk-inserts the entire draft's
    schedule from lib.draft.generate_full_draft_order(), team_selected
    left NULL throughout. `set_by` isn't persisted (kept the schema to
    exactly what this feature needed) -- accepted here for call-site
    clarity about who's performing the setup, even though it isn't stored.
    """
    from lib.config import DRAFT_PICKS_PER_PLAYER
    from lib.draft import generate_full_draft_order

    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT COUNT(*) FROM draft_order
                WHERE season_id = %s AND pod_id = %s AND team_selected IS NOT NULL
                """,
                (season_id, pod_id),
            )
            if cur.fetchone()[0] > 0:
                raise DraftSetupError("The draft has already started — the pick order can't be changed now.")

            cur.execute(
                "DELETE FROM draft_order WHERE season_id = %s AND pod_id = %s",
                (season_id, pod_id),
            )

            slots = generate_full_draft_order(base_order, DRAFT_PICKS_PER_PLAYER)
            for slot in slots:
                cur.execute(
                    """
                    INSERT INTO draft_order
                        (season_id, pod_id, player_id, pick_number, round_number, pick_in_round)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (season_id, pod_id, slot.player_id, slot.pick_number,
                     slot.round_number, slot.pick_in_round),
                )
            conn.commit()
        finally:
            cur.close()

    get_draft_page_state.clear(season_id, pod_id)
    get_draft_picks_made.clear(season_id, pod_id)
    is_draft_complete.clear(season_id, pod_id)


@st.cache_data(ttl=3600)
def get_draft_picks_made(season_id: int, pod_id: int) -> int:
    """COUNT of draft_order rows with team_selected already set for this
    pod. Long TTL mirrors get_conference_slot_tiers -- explicit .clear()
    from submit_draft_picks/set_draft_order keeps it live-accurate."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM draft_order
            WHERE season_id = %s AND pod_id = %s AND team_selected IS NOT NULL
            """,
            (season_id, pod_id),
        )
        count = cur.fetchone()[0]
        cur.close()
    return count


@st.cache_data(ttl=3600)
def is_draft_complete(season_id: int, pod_id: int) -> bool:
    """
    True iff draft_order has rows for this pod (setup has happened) AND
    none of them still have team_selected IS NULL. Zero rows (setup not
    done yet) is explicitly NOT complete -- a distinct state from finished.

    Cached (unlike most is_*/get_*_complete-style checks in this file) —
    this is called on every single page load in the whole app (see
    lib.nav.visible_pages()), not just the Draft page, so it must be
    cheap; explicit .clear() from set_draft_order/submit_draft_picks
    keeps it accurate while a draft is actually in progress.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), SUM(team_selected IS NULL) FROM draft_order
            WHERE season_id = %s AND pod_id = %s
            """,
            (season_id, pod_id),
        )
        total, still_open = cur.fetchone()
        cur.close()
    return total > 0 and (still_open or 0) == 0


class DraftOutOfSyncError(Exception):
    """Raised when a draft pick submission no longer matches the live draft state."""


def submit_draft_picks(season_id: int, pod_id: int, player_id: int, team_ids: list) -> None:
    """
    Submits 1 or 2 picks (a double-pick only at a snake round boundary)
    for whoever is actually on the clock. Re-derives and re-validates
    everything inside a row lock rather than trusting anything the caller
    computed beforehand — draft already complete, wrong player, wrong
    pick count, a team someone else just took (checked pod-scoped, live)
    all raise DraftOutOfSyncError naming what changed, and nothing gets
    written. The final UPDATE's "WHERE team_selected IS NULL" guard is
    the authoritative, atomic "did I actually win this slot" check — if
    it affects 0 rows, someone else's write landed first between the read
    and here, so we roll back and raise rather than trusting the earlier read.
    """
    with get_connection() as conn:
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT pick_number, round_number, player_id FROM draft_order
                WHERE season_id = %s AND pod_id = %s AND team_selected IS NULL
                ORDER BY pick_number
                LIMIT 2
                FOR UPDATE
                """,
                (season_id, pod_id),
            )
            open_slots = cur.fetchall()

            if not open_slots:
                raise DraftOutOfSyncError("The draft is already complete.")
            if open_slots[0]["player_id"] != player_id:
                raise DraftOutOfSyncError("It's no longer your turn to pick.")

            is_double = len(open_slots) > 1 and open_slots[1]["player_id"] == player_id
            expected_count = 2 if is_double else 1

            if len(team_ids) != expected_count:
                raise DraftOutOfSyncError(
                    f"Expected {expected_count} pick(s) this submission, got {len(team_ids)}."
                )
            if len(set(team_ids)) != len(team_ids):
                raise DraftOutOfSyncError("The same team was submitted twice in one pick.")

            placeholders = ", ".join(["%s"] * len(team_ids))
            cur.execute(
                f"""
                SELECT team_id FROM rosters
                WHERE season_id = %s AND pod_id = %s AND is_active = 1
                  AND team_id IN ({placeholders})
                """,
                (season_id, pod_id, *team_ids),
            )
            already_taken = {row["team_id"] for row in cur.fetchall()}
            if already_taken:
                raise DraftOutOfSyncError(
                    f"Team(s) {sorted(already_taken)} were already drafted in this pod."
                )

            for slot, team_id in zip(open_slots[:expected_count], team_ids):
                cur.execute(
                    """
                    UPDATE draft_order SET team_selected = %s
                    WHERE season_id = %s AND pod_id = %s AND pick_number = %s
                      AND team_selected IS NULL
                    """,
                    (team_id, season_id, pod_id, slot["pick_number"]),
                )
                if cur.rowcount == 0:
                    raise DraftOutOfSyncError(
                        f"Pick {slot['pick_number']} was already claimed by someone else."
                    )

                cur.execute(
                    """
                    INSERT INTO rosters
                        (player_id, team_id, season_id, pod_id, draft_round, draft_pick,
                         is_active, acquisition_type)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, 'draft')
                    """,
                    (player_id, team_id, season_id, pod_id, slot["round_number"], slot["pick_number"]),
                )

            conn.commit()
        finally:
            cur.close()

    get_draft_page_state.clear(season_id, pod_id)
    get_draft_picks_made.clear(season_id, pod_id)
    is_draft_complete.clear(season_id, pod_id)
    get_roster_with_context.clear()