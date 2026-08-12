"""
SQLite storage for Gridiron Edge.

Opened in WAL mode with a generous busy timeout so the 06:30 daily fetch can
WRITE while the Flask app is serving READS. (The other trackers on this machine
were bitten by the default `journal_mode=delete`, which makes those mutually
exclusive and can roll back a whole day's fetch with "database is locked".)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per player we care about, merged from Sleeper + nflverse roster.
CREATE TABLE IF NOT EXISTS players (
    player_id     TEXT PRIMARY KEY,   -- sleeper id when known, else gsis
    sleeper_id    TEXT,
    gsis_id       TEXT,
    pfr_id        TEXT,
    espn_id       TEXT,
    name          TEXT,
    search_name   TEXT,
    position      TEXT,
    team          TEXT,
    age           REAL,
    birth_date    TEXT,
    years_exp     INTEGER,
    height        TEXT,
    weight        TEXT,
    college       TEXT,
    status        TEXT,
    depth_pos     TEXT,
    depth_order   INTEGER,
    injury_status TEXT,
    injury_body   TEXT,
    injury_notes  TEXT,
    practice      TEXT,
    headshot      TEXT,
    rookie_year   INTEGER,
    entry_year    INTEGER,
    draft_number  INTEGER,
    draft_club    TEXT,
    search_rank   INTEGER,
    active        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_players_gsis ON players(gsis_id);
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
CREATE INDEX IF NOT EXISTS idx_players_sname ON players(search_name);

-- Season-level production, one row per player-season.
CREATE TABLE IF NOT EXISTS player_season (
    gsis_id TEXT, season INTEGER, team TEXT, position TEXT, games INTEGER,
    completions REAL, attempts REAL, passing_yards REAL, passing_tds REAL,
    interceptions REAL, sacks REAL, passing_epa REAL, passing_cpoe REAL,
    carries REAL, rushing_yards REAL, rushing_tds REAL, rushing_epa REAL,
    rushing_first_downs REAL, rushing_20 REAL,
    targets REAL, receptions REAL, receiving_yards REAL, receiving_tds REAL,
    receiving_epa REAL, receiving_air_yards REAL, receiving_yac REAL,
    receiving_first_downs REAL, receiving_20 REAL,
    target_share REAL, air_yards_share REAL, wopr REAL, racr REAL,
    fumbles_lost REAL, two_pt REAL, special_teams_tds REAL,
    fantasy_points REAL, fantasy_points_ppr REAL,
    PRIMARY KEY (gsis_id, season)
);

-- Team-level production, drives the scoring-environment / pace model.
CREATE TABLE IF NOT EXISTS team_season (
    team TEXT, season INTEGER, games INTEGER,
    attempts REAL, completions REAL, passing_yards REAL, passing_tds REAL,
    carries REAL, rushing_yards REAL, rushing_tds REAL,
    targets REAL, receptions REAL, receiving_yards REAL,
    sacks REAL, passing_epa REAL, rushing_epa REAL,
    PRIMARY KEY (team, season)
);

CREATE TABLE IF NOT EXISTS snaps (
    pfr_id TEXT, player TEXT, season INTEGER, team TEXT, position TEXT,
    games INTEGER, off_snaps REAL, off_pct REAL,
    PRIMARY KEY (pfr_id, season, team)
);

-- Weekly injury-report rows collapsed to a per-player-season summary.
CREATE TABLE IF NOT EXISTS injury_history (
    gsis_id TEXT, season INTEGER, team TEXT,
    weeks_out INTEGER, weeks_dnp INTEGER, weeks_limited INTEGER,
    weeks_questionable INTEGER, body_parts TEXT,
    PRIMARY KEY (gsis_id, season)
);

-- Advanced rushing (yards before/after contact, broken tackles).
CREATE TABLE IF NOT EXISTS adv_rush (
    pfr_id TEXT, season INTEGER, carries REAL,
    ybc REAL, ybc_att REAL, yac REAL, yac_att REAL, broken REAL,
    PRIMARY KEY (pfr_id, season)
);

-- Next Gen Stats receiving (separation, cushion, air-yards share, YAC over expected).
CREATE TABLE IF NOT EXISTS ngs_rec (
    gsis_id TEXT, season INTEGER,
    avg_cushion REAL, avg_separation REAL, avg_intended_air_yards REAL,
    pct_share_air_yards REAL, catch_pct REAL, avg_yac REAL, yac_above_exp REAL,
    PRIMARY KEY (gsis_id, season)
);

CREATE TABLE IF NOT EXISTS schedule (
    game_id TEXT PRIMARY KEY, season INTEGER, week INTEGER, gameday TEXT,
    home_team TEXT, away_team TEXT, home_coach TEXT, away_coach TEXT,
    spread_line REAL, total_line REAL, roof TEXT, surface TEXT, div_game INTEGER,
    home_score REAL, away_score REAL
);
CREATE INDEX IF NOT EXISTS idx_sched_season ON schedule(season);

-- Average draft position, one row per (format, player).
CREATE TABLE IF NOT EXISTS adp (
    fmt TEXT, name TEXT, search_name TEXT, position TEXT, team TEXT,
    adp REAL, adp_formatted TEXT, stdev REAL, high REAL, low REAL,
    times_drafted INTEGER, bye INTEGER, player_id TEXT,
    total_drafts INTEGER, window_start TEXT, window_end TEXT,
    PRIMARY KEY (fmt, name, position)
);

-- Day-over-day ADP so the site can show who is rising and falling.
CREATE TABLE IF NOT EXISTS adp_history (
    fmt TEXT, search_name TEXT, date TEXT, adp REAL,
    PRIMARY KEY (fmt, search_name, date)
);

-- Computed: each coach's career offensive fingerprint.
CREATE TABLE IF NOT EXISTS coach_profile (
    coach TEXT PRIMARY KEY, seasons INTEGER, team_seasons TEXT,
    plays_per_game REAL, pass_rate REAL, points_per_game REAL,
    pass_att_pg REAL, rush_att_pg REAL, pass_td_rate REAL, rush_td_rate REAL
);

-- Computed: 2026 team environment after coaching + vacated-opportunity adjustments.
CREATE TABLE IF NOT EXISTS team_context (
    team TEXT PRIMARY KEY, season INTEGER,
    plays_per_game REAL, pass_rate REAL, pass_att_pg REAL, rush_att_pg REAL,
    implied_ppg REAL, implied_games INTEGER, bye_week INTEGER,
    vacated_targets REAL, vacated_carries REAL,
    coach_note TEXT, caller TEXT, caller_changed INTEGER, severity REAL,
    sos REAL
);

-- Final model output.
CREATE TABLE IF NOT EXISTS projections (
    player_id TEXT, scoring TEXT,
    name TEXT, position TEXT, team TEXT, age REAL,
    games REAL, pass_att REAL, pass_yd REAL, pass_td REAL, ints REAL,
    carries REAL, rush_yd REAL, rush_td REAL,
    targets REAL, receptions REAL, rec_yd REAL, rec_td REAL,
    points REAL, points_per_game REAL,
    floor REAL, ceiling REAL,
    availability REAL, injury_risk TEXT, injury_note TEXT,
    vorp REAL, tier INTEGER, pos_rank INTEGER, overall_rank INTEGER,
    auction_value REAL, max_bid REAL,
    adp REAL, adp_pos_rank REAL, value_vs_adp REAL,
    breakout REAL, confidence REAL, notes TEXT,
    PRIMARY KEY (player_id, scoring)
);
CREATE INDEX IF NOT EXISTS idx_proj_scoring ON projections(scoring, position);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. SQLite cannot add them via
# CREATE TABLE IF NOT EXISTS, so they are applied explicitly - otherwise an
# existing database silently keeps the old shape and the fetch fails on an
# unknown column.
MIGRATIONS = [
    ("player_season", "special_teams_tds", "REAL"),
    ("schedule", "home_score", "REAL"),
    ("schedule", "away_score", "REAL"),
]


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def bump_version(conn: sqlite3.Connection) -> None:
    """Every fetcher bumps this; the app caches keyed on it."""
    cur = get_meta(conn, "data_version", "0")
    try:
        n = int(cur)
    except (TypeError, ValueError):
        n = 0
    set_meta(conn, "data_version", n + 1)
