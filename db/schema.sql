-- =============================================================
-- CFB Fantasy Game Database Schema
-- =============================================================
-- Canonical reference for the Aiven MySQL schema.
-- Run this once per environment setup.
-- Safe to re-run: uses IF NOT EXISTS throughout.
--
-- Last synced to Aiven: 2026-07-05
-- =============================================================

CREATE DATABASE IF NOT EXISTS cfb_game CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE cfb_game;

-- -------------------------------------------------------------
-- FOOTBALL DATA LAYER
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seasons (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    year            INT NOT NULL UNIQUE,
    first_week      INT NOT NULL DEFAULT 1,
    last_week       INT NOT NULL,
    week0_cutoff_week INT COMMENT 'Last week a player may deploy week 0 points',
    pvp_regular_season_end_week INT COMMENT 'Last week of the PVP regular-season round-robin, weeks after this are playoffs/other content. NULL = not configured.',
    draft_date      DATE,
    default_lock_time TIME NOT NULL DEFAULT '12:00:00' COMMENT 'Weekly lineup lock time (local)',
    draft_picks_per_player INT NOT NULL DEFAULT 25
                    COMMENT 'Draft length / active roster size for this season -- read by lib.db.set_draft_order() instead of a hardcoded constant, so pod size (e.g. 25 for 4-player pods, 30 for 3-player pods) is season-init-time config, not a code change. Default 25 preserves existing seasons unchanged.',
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conferences (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    name            VARCHAR(100) NOT NULL,
    abbreviation    VARCHAR(20) NOT NULL,
    tier            ENUM('P4', 'G6', 'Independent', 'FCS') COMMENT 'NULL for unclassified',
    cfbd_id         VARCHAR(50) COMMENT 'CFBD API conference identifier',
    notes           TEXT COMMENT 'e.g. P4 Independent, G6 Independent',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_lineup_slot  BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'TRUE = conference appears as a lineup slot',
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    UNIQUE KEY uq_conference_season (season_id, abbreviation)
);

CREATE TABLE IF NOT EXISTS teams (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    conference_id   INT NOT NULL,
    name_short      VARCHAR(50) NOT NULL COMMENT 'Short display name (e.g. Alabama)',
    name_display    VARCHAR(100) NOT NULL COMMENT 'Medium display name (e.g. Alabama Crimson Tide)',
    name_full       VARCHAR(150) NOT NULL COMMENT 'Full official name',
    abbreviation    VARCHAR(20),
    cfbd_id         VARCHAR(50) NOT NULL COMMENT 'CFBD API team identifier',
    level           ENUM('FBS', 'FCS') NOT NULL DEFAULT 'FBS',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (conference_id) REFERENCES conferences(id),
    UNIQUE KEY uq_team_season (season_id, cfbd_id)
);

CREATE TABLE IF NOT EXISTS week_schedule (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    week            INT NOT NULL,
    lock_time       DATETIME COMMENT 'Override per-week lock; NULL means use season default',
    notes           TEXT,
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    UNIQUE KEY uq_week_season (season_id, week)
);

CREATE TABLE IF NOT EXISTS games (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    home_team_id    INT NOT NULL,
    away_team_id    INT NOT NULL,
    is_neutral      BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'True for neutral-site games',
    cfbd_game_id    INT NOT NULL COMMENT 'CFBD API game identifier for score lookups',
    week            INT NOT NULL COMMENT 'Our corrected week number (0 for week 0 games)',
    is_week0        BOOLEAN NOT NULL DEFAULT FALSE,
    game_date       DATE,
    game_time       TIME,
    home_score      INT,
    away_score      INT,
    status          ENUM('scheduled', 'in_progress', 'final') NOT NULL DEFAULT 'scheduled',
    last_updated    DATETIME,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (home_team_id) REFERENCES teams(id),
    FOREIGN KEY (away_team_id) REFERENCES teams(id),
    UNIQUE KEY uq_cfbd_game (cfbd_game_id)
);

-- -------------------------------------------------------------
-- METAGAME LAYER
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pods (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    name            VARCHAR(100) NOT NULL,
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(id)
);

CREATE TABLE IF NOT EXISTS players (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    username        VARCHAR(50) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    is_commissioner BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pod_memberships (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    pod_id          INT NOT NULL,
    player_id       INT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (pod_id) REFERENCES pods(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE KEY uq_pod_membership (season_id, player_id)
    COMMENT 'Which pod a player is in for a given season -- pod membership is per-season (pods are re-formed each season, per pods.season_id), not a permanent property of a player. Replaced a players.pod_id column (dropped 2026-07-05) that conflated the two.'
);

CREATE TABLE IF NOT EXISTS draft_order (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    pod_id          INT NOT NULL,
    player_id       INT NOT NULL,
    pick_number     INT NOT NULL COMMENT 'Overall pick number across all rounds',
    round_number    INT NOT NULL,
    pick_in_round   INT NOT NULL,
    team_selected   INT NULL COMMENT 'The team picked for this slot; NULL until the on-the-clock player actually picks.',
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (pod_id) REFERENCES pods(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (team_selected) REFERENCES teams(id),
    UNIQUE KEY uq_pick (season_id, pod_id, pick_number)
);

CREATE TABLE IF NOT EXISTS rosters (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    team_id         INT NOT NULL,
    season_id       INT NOT NULL,
    pod_id          INT NOT NULL COMMENT 'Denormalized from pod_memberships at write time -- pods are completely severed rosters (2026-07-05), so the uniqueness below must be scoped by pod, not just season; a DB-level UNIQUE KEY can only reference columns on this table',
    draft_round     INT,
    draft_pick      INT COMMENT 'Overall pick number',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    acquired_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dropped_at      DATETIME,
    acquisition_type ENUM('draft', 'free_agent', 'commissioner') NOT NULL DEFAULT 'draft',
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (pod_id) REFERENCES pods(id),
    UNIQUE KEY uq_roster_active_team (season_id, pod_id, team_id, is_active)
);

CREATE TABLE IF NOT EXISTS week0_elections (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    season_id       INT NOT NULL,
    game_id         INT NOT NULL COMMENT 'The week 0 game being banked',
    team_id         INT NOT NULL COMMENT 'Which team in the game the player is banking',
    declared        BOOLEAN NOT NULL DEFAULT FALSE,
    week_deployed   INT COMMENT 'NULL until player elects to use it',
    commissioner_assigned BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'TRUE if commissioner assigned it',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    UNIQUE KEY uq_week0_player_game (player_id, game_id)
);

CREATE TABLE IF NOT EXISTS weekly_lineups (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    season_id       INT NOT NULL,
    week            INT NOT NULL,
    submitted_at    DATETIME,
    locked_at       DATETIME COMMENT 'Timestamp when this lineup was locked',
    is_locked       BOOLEAN NOT NULL DEFAULT FALSE,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    UNIQUE KEY uq_lineup (player_id, season_id, week)
);

CREATE TABLE IF NOT EXISTS lineup_slots (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    lineup_id       INT NOT NULL,
    team_id         INT NULL COMMENT 'NULL for passed slots',
    game_id         INT NULL COMMENT 'NULL = current-week game; populated for week 0 picks',
    slot_type       ENUM('conference', 'p4_flex', 'g6_flex', 'wildcard') NOT NULL,
    conference_slug VARCHAR(20) COMMENT 'Populated for conference slots, NULL for flex/wildcard',
    points_scored   INT COMMENT 'Net margin; populated after games final',
    FOREIGN KEY (lineup_id) REFERENCES weekly_lineups(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

-- -------------------------------------------------------------
-- SCORING AND STANDINGS
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scoring_contexts (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    season_id       INT NOT NULL,
    name            VARCHAR(100) NOT NULL COMMENT 'e.g. standard_4player, cross_pod_8player',
    scoring_type    ENUM('match_play', 'point_play', 'matchup', 'match_play_pod', 'custom') NOT NULL
                    COMMENT 'match_play_pod = cross-pod formula: base points by overall_rank plus pod/pod-vs-pod/overall bonuses, see lib/metagame.py',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    points_map      VARCHAR(255) COMMENT 'JSON array e.g. [4,2,1,0] — index 0 = 1st place',
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (season_id) REFERENCES seasons(id)
);

CREATE TABLE IF NOT EXISTS matchup_pairings (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    scoring_context_id INT NOT NULL,
    season_id       INT NOT NULL,
    week            INT NOT NULL,
    matchup_type    ENUM('in_pod', 'cross_pod') NOT NULL DEFAULT 'in_pod'
                    COMMENT 'in_pod = same pod; cross_pod = different pods (single-pod seasons always in_pod)',
    player_a_id     INT NOT NULL,
    player_b_id     INT NOT NULL,
    winner_id       INT COMMENT 'NULL until week closes',
    is_tie          BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'TRUE = confirmed tie (winner_id stays NULL), FALSE + winner_id NULL = not yet played',
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (player_a_id) REFERENCES players(id),
    FOREIGN KEY (player_b_id) REFERENCES players(id),
    FOREIGN KEY (winner_id) REFERENCES players(id),
    UNIQUE KEY uq_matchup_pairing (scoring_context_id, week, player_a_id, player_b_id)
);

CREATE TABLE IF NOT EXISTS manual_tiebreaks (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    season_id           INT NOT NULL,
    scoring_context_id  INT NOT NULL,
    tied_player_ids     VARCHAR(100) NOT NULL COMMENT 'Sorted comma-separated player_ids identifying the tied group, e.g. "3,7,12"',
    resolved_order      VARCHAR(100) NOT NULL COMMENT 'Comma-separated player_ids, commissioner-decided best-to-worst order',
    decided_by          INT NOT NULL COMMENT 'player_id of the commissioner who made the call',
    decided_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes               TEXT,
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    FOREIGN KEY (decided_by) REFERENCES players(id),
    UNIQUE KEY uq_tiebreak_group (season_id, scoring_context_id, tied_player_ids)
);

CREATE TABLE IF NOT EXISTS weekly_results (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    season_id       INT NOT NULL,
    scoring_context_id INT NOT NULL,
    week            INT NOT NULL,
    total_points    INT NOT NULL DEFAULT 0 COMMENT 'Raw net margin sum for the week',
    match_play_points INT COMMENT 'Points awarded by scoring context (e.g. 4/2/1/0); the total including any bonuses for match_play_pod contexts',
    pod_rank        INT COMMENT 'Finish position within this player\'s pod for the week',
    overall_rank    INT COMMENT 'Finish position across all pods; same as pod_rank in single-pod seasons',
    base_points     INT COMMENT 'Points from overall_rank on the base ladder alone, before bonuses (match_play_pod contexts only)',
    pod_bonus       INT COMMENT '+1 if this player had the top total_points within their own pod this week (match_play_pod contexts only)',
    pod_vs_pod_bonus INT COMMENT '+1 to every player in the pod whose combined weekly total_points beat the other pod\'s; 0 to both on an exact tie (match_play_pod contexts only)',
    overall_bonus   INT COMMENT '+1 to the single overall_rank=1 player this week (match_play_pod contexts only)',
    computed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    UNIQUE KEY uq_result (player_id, season_id, scoring_context_id, week)
);

CREATE TABLE IF NOT EXISTS standings_snapshots (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    season_id       INT NOT NULL,
    scoring_context_id INT NOT NULL,
    after_week      INT NOT NULL,
    cumulative_match_play_points INT NOT NULL DEFAULT 0,
    cumulative_point_play_total  INT NOT NULL DEFAULT 0,
    cumulative_rank INT,
    snapshot_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    UNIQUE KEY uq_snapshot (player_id, season_id, scoring_context_id, after_week)
);

-- Playoff/exhibition bracket games. One row per lib.bracket_engine.GameSpec
-- slot (game_type='bracket') plus the fixed-count exhibition placeholders
-- for the final playoff week (game_type='exhibition', resolved dynamically
-- by scripts/advance_playoffs.py rather than from a fixed GameSpec source).
CREATE TABLE IF NOT EXISTS playoff_games (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    season_id               INT NOT NULL,
    scoring_context_id      INT NOT NULL,
    bracket_format          ENUM('four_team', 'six_team', 'eight_team') NOT NULL
        COMMENT 'Which lib.playoffs bracket spec this row belongs to',
    game_type               ENUM('bracket', 'exhibition') NOT NULL DEFAULT 'bracket'
        COMMENT 'bracket = real GameSpec game, feeds placements, exhibition = fills an idle eliminated players final week, never affects placement',
    game_number             INT NOT NULL
        COMMENT 'Matches GameSpec.game_number for bracket rows (1-2 four_team, 1-7 six_team, 1-8 eight_team), exhibition rows continue the sequence (3 for four_team, 8 for six_team, 9-10 for eight_team)',
    round                   INT NOT NULL
        COMMENT 'GameSpec.round label, exhibition rows use the brackets final round number',
    week                    INT NOT NULL COMMENT 'Real calendar week = regular_season_weeks + round',

    side_a_source_type     ENUM('seed', 'winner_of', 'loser_of', 'exhibition') NOT NULL,
    side_a_source_ref      INT NULL
        COMMENT 'Seed number (source_type=seed) or source game_number (winner_of/loser_of), NULL for exhibition',
    side_b_source_type     ENUM('seed', 'winner_of', 'loser_of', 'exhibition') NOT NULL,
    side_b_source_ref      INT NULL,

    player_a_id             INT NULL COMMENT 'NULL until side_a resolves to a real player',
    player_b_id             INT NULL,
    winner_id               INT NULL,
    loser_id                INT NULL,
    is_tie                  BOOLEAN NOT NULL DEFAULT FALSE
        COMMENT 'Exhibition games only -- bracket games always force a decisive winner via resolve_playoff_game()',
    decided_by              ENUM('total_points', 'pick_record', 'manual') NULL
        COMMENT 'Audit only, mirrors lib.bracket_engine.GameResult.decided_by',

    winner_place             INT NULL COMMENT 'Final placement the winner earns, NULL for exhibition + non-deciding bracket games',
    loser_place              INT NULL,

    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_playoff_game (season_id, scoring_context_id, game_number),
    KEY idx_playoff_games_week (season_id, scoring_context_id, week),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    FOREIGN KEY (player_a_id) REFERENCES players(id),
    FOREIGN KEY (player_b_id) REFERENCES players(id),
    FOREIGN KEY (winner_id) REFERENCES players(id),
    FOREIGN KEY (loser_id) REFERENCES players(id)
);

-- General, multi-dimensional standings -- one row per player per
-- (season, scoring_context). Deliberately NOT named after playoffs or PVP:
-- match_play_standing and total_points_standing are reserved placeholder
-- columns for future work (final match-play ranking approach still TBD;
-- total-points ranking is already computable from weekly_results via
-- lib.metagame.build_season_leaderboard() but isn't populated here).
-- Distinct from standings_snapshots above, which is a per-week time series
-- of cumulative totals, not a single end-of-season multi-facet row.
CREATE TABLE IF NOT EXISTS season_standings (
    id                          INT AUTO_INCREMENT PRIMARY KEY,
    season_id                   INT NOT NULL,
    scoring_context_id          INT NOT NULL,
    player_id                   INT NOT NULL,

    pvp_rank                    INT NULL COMMENT 'Regular-season finish position, 1-N, from compute_seeding()',
    pvp_wins                    INT NULL,
    pvp_losses                  INT NULL,
    pvp_ties                    INT NULL,
    pvp_win_pct                 DECIMAL(5,4) NULL,
    pvp_net_margin              INT NULL,

    playoff_seed                INT NULL
        COMMENT 'Bracket seed actually assigned (post pod-adjustment for eight_team), equals pvp_rank for four_team',
    playoff_placement           INT NULL COMMENT 'Final place, 1-N, once the bracket is fully decided',

    match_play_standing         INT NULL COMMENT 'Placeholder -- final season-end match-play ranking approach still undecided',
    total_points_standing       INT NULL COMMENT 'Placeholder -- computable via lib.metagame.build_season_leaderboard(), not populated here',

    computed_at                 DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uq_season_standings (season_id, scoring_context_id, player_id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (scoring_context_id) REFERENCES scoring_contexts(id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);

-- -------------------------------------------------------------
-- RECORDS / AWARDS
-- -------------------------------------------------------------

-- The known set of award/record categories (seeded once — see
-- migration/records_2026.sql for the 15 categories extracted from the
-- live "Records and Awards" page). better_direction lets an automated
-- "does this beat the record" check avoid hardcoding per-category logic.
CREATE TABLE IF NOT EXISTS record_types (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    name                VARCHAR(100) NOT NULL UNIQUE COMMENT 'e.g. "Best Week", "Biggest Win"',
    description         VARCHAR(255) NOT NULL COMMENT 'e.g. "Most net points in a week"',
    scope               ENUM('week', 'game') NOT NULL COMMENT 'week = one result per player per week, game = one result per player per single game',
    better_direction    ENUM('higher', 'lower') NOT NULL COMMENT 'whether a higher or lower value is more record-worthy',
    value_type          ENUM('points', 'percentage') NOT NULL DEFAULT 'points',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One row per instance a record was set/broken (log-style, not
-- "current holder only", so history is preserved as records get beaten).
CREATE TABLE IF NOT EXISTS records (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    record_type_id  INT NOT NULL,
    season_id       INT COMMENT 'NULL for legacy entries with no matching season row in this DB',
    week            INT COMMENT 'week the record occurred in, when known',
    legacy_period   VARCHAR(50) COMMENT 'free-text period label for entries with no season_id, e.g. "2023 Week 1", "2019"',
    player_id       INT COMMENT 'NULL for league-wide records (e.g. Shootout, Defensive Battle) with no single player holder',
    value           DECIMAL(10,2) NOT NULL COMMENT 'the recorded value, units per record_types.value_type',
    context         VARCHAR(255) COMMENT 'additional detail where applicable, e.g. the team that scored the points for Biggest Win/Loss',
    achieved_at     DATE COMMENT 'calendar date achieved, when known',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'when this row was inserted (not necessarily when the record itself occurred)',
    FOREIGN KEY (record_type_id) REFERENCES record_types(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    INDEX idx_records_type (record_type_id)
);

-- -------------------------------------------------------------
-- ROSTER MANAGEMENT
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS dropadd_requests (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL COMMENT 'requesting player',
    season_id       INT NOT NULL,
    week            INT NOT NULL COMMENT 'week the drop/add is requested to take effect',
    dropped_team_id INT COMMENT 'NULL for add-only requests',
    added_team_id   INT COMMENT 'NULL for drop-only requests',
    status          ENUM('pending', 'approved', 'denied') NOT NULL DEFAULT 'pending',
    requested_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at      DATETIME COMMENT 'NULL while pending',
    decided_by      INT COMMENT 'player_id of the commissioner who approved/denied; NULL while pending',
    notes           TEXT,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (dropped_team_id) REFERENCES teams(id),
    FOREIGN KEY (added_team_id) REFERENCES teams(id),
    FOREIGN KEY (decided_by) REFERENCES players(id),
    INDEX idx_dropadd_requests_status (season_id, status)
);

-- Completed transactions only (pending workflow lives in dropadd_requests
-- above). On approval, application code writes the applied change here.
CREATE TABLE IF NOT EXISTS drop_add_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT NOT NULL,
    season_id       INT NOT NULL,
    week            INT NOT NULL,
    dropped_team_id INT COMMENT 'NULL for add-only transactions',
    added_team_id   INT COMMENT 'NULL for drop-only transactions',
    actioned_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actioned_by     INT COMMENT 'player_id of who submitted (player or commissioner)',
    notes           TEXT,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (season_id) REFERENCES seasons(id),
    FOREIGN KEY (dropped_team_id) REFERENCES teams(id),
    FOREIGN KEY (added_team_id) REFERENCES teams(id),
    FOREIGN KEY (actioned_by) REFERENCES players(id)
);

-- -------------------------------------------------------------
-- AUDIT LOG
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    player_id       INT COMMENT 'NULL for system actions',
    action_type     VARCHAR(50) NOT NULL COMMENT 'e.g. lineup_edit, score_correction, commissioner_override',
    target_table    VARCHAR(50),
    target_id       INT,
    description     TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (player_id) REFERENCES players(id)
);
