import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id                INTEGER PRIMARY KEY,   -- Garmin activityId
    name              TEXT,
    type              TEXT,                  -- Garmin typeKey, e.g. running, trail_running
    is_run            INTEGER NOT NULL,
    event_type        TEXT,                  -- Garmin eventType typeKey; 'race' when marked as a race
    start_local      TEXT NOT NULL,         -- 'YYYY-MM-DD HH:MM:SS' local time
    start_gmt         TEXT,
    distance_m        REAL,
    duration_s        REAL,
    moving_s          REAL,
    elevation_gain_m  REAL,
    avg_speed_mps     REAL,
    avg_hr            REAL,
    max_hr            REAL,
    avg_cadence       REAL,
    calories          REAL,
    training_load     REAL,
    aerobic_te        REAL,
    anaerobic_te      REAL,
    vo2max            REAL,
    hr_zone_1_s       REAL,
    hr_zone_2_s       REAL,
    hr_zone_3_s       REAL,
    hr_zone_4_s       REAL,
    hr_zone_5_s       REAL,              -- hr_zone_*: Garmin's own zones, as recorded
    z1_s REAL, z2_s REAL, z3_s REAL, z4_s REAL, z5_s REAL,  -- Norwegian zones (zones.py), used for analysis
    has_samples       INTEGER NOT NULL DEFAULT 0,
    detail_synced     INTEGER NOT NULL DEFAULT 0,  -- 1 once FIT/details/splits are saved
    raw_json          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_local);

-- Per-second (or per-record) stream for runs, from the original FIT file.
CREATE TABLE IF NOT EXISTS samples (
    activity_id  INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    t            REAL NOT NULL,    -- seconds since activity start
    distance_m   REAL,
    hr           REAL,
    speed_mps    REAL,
    cadence      REAL,
    altitude_m   REAL,
    lat          REAL,
    lon          REAL,
    gct_ms REAL, vo_mm REAL, vr_pct REAL, step_mm REAL, balance_pct REAL, power_w REAL  -- running dynamics
);
CREATE INDEX IF NOT EXISTS idx_samples_activity ON samples(activity_id, t);

CREATE TABLE IF NOT EXISTS laps (
    activity_id  INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    distance_m   REAL,
    duration_s   REAL,
    avg_hr       REAL,
    avg_speed_mps REAL,
    cadence_spm REAL, gct_ms REAL, vo_mm REAL, vr_pct REAL, step_mm REAL, balance_pct REAL, power_w REAL,
    PRIMARY KEY (activity_id, idx)
);

-- Fastest time over each standard distance within each run.
CREATE TABLE IF NOT EXISTS best_efforts (
    activity_id  INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    distance_m   REAL NOT NULL,
    time_s       REAL NOT NULL,
    PRIMARY KEY (activity_id, label)
);

-- Point-in-time snapshots of account-level data (race predictions, PRs, ...).
CREATE TABLE IF NOT EXISTS snapshots (
    kind       TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    raw_json   TEXT NOT NULL,
    PRIMARY KEY (kind, taken_at)
);

-- Garmin's daily VO2max estimate; only days where it was updated. The activity
-- summaries round it to a whole number, this keeps the decimal.
CREATE TABLE IF NOT EXISTS vo2max_daily (
    date         TEXT PRIMARY KEY,   -- YYYY-MM-DD
    precise      REAL,               -- e.g. 50.2
    rounded      REAL,               -- the whole number Garmin displays
    fitness_age  REAL,
    raw_json     TEXT NOT NULL
);

-- Raw daily body data from Garmin (sleep, hrv, stats, readiness, training_status).
CREATE TABLE IF NOT EXISTS wellness (
    date        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    raw_json    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (date, kind)
);

-- AI-written status cards (physical state, last 12 hours, next workout).
CREATE TABLE IF NOT EXISTS status_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at  TEXT NOT NULL,   -- local time
    report_json   TEXT NOT NULL
);

-- Training plan: one row per planned session. Notes (races, zones, rules) live in coach_state.
CREATE TABLE IF NOT EXISTS plan_sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT NOT NULL,   -- YYYY-MM-DD
    kind     TEXT NOT NULL,   -- easy | long | threshold | intervals | race | rest | cross
    title    TEXT NOT NULL,
    details  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS coach_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The conversation with the coach, as shown in the Coach panel.
CREATE TABLE IF NOT EXISTS coach_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,      -- user | coach
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Logged-in devices (only used when a password is set; see auth.py).
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash  TEXT PRIMARY KEY,
    expires_at  TEXT NOT NULL
);

-- Race log. Official times win over watch times; effort decides whether a race counts for PRs/predictions.
CREATE TABLE IF NOT EXISTS races (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT NOT NULL,
    name             TEXT NOT NULL,
    distance_m       REAL NOT NULL,
    course           TEXT,
    official_time_s  REAL,
    target_time_s    REAL,               -- upcoming races
    effort           TEXT NOT NULL DEFAULT 'all_out',   -- all_out | controlled | easy
    activity_id      INTEGER,
    notes            TEXT
);

-- Plan changes the coach proposed; nothing changes until the athlete applies one.
CREATE TABLE IF NOT EXISTS plan_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | applied | dismissed
    summary       TEXT NOT NULL,
    changes_json  TEXT NOT NULL
);

-- Weather at the start of each outdoor run (Garmin's nearby station). NULLs = none available.
CREATE TABLE IF NOT EXISTS activity_weather (
    activity_id   INTEGER PRIMARY KEY,
    temp_f        REAL,
    dew_point_f   REAL,
    humidity      REAL,
    wind_mph      REAL,
    wind_dir      TEXT,
    condition     TEXT,
    raw_json      TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

-- Cached per-run form analysis (form.py); recomputed when form.VERSION changes.
CREATE TABLE IF NOT EXISTS form_stats (
    activity_id  INTEGER PRIMARY KEY,
    version      INTEGER NOT NULL,
    stats_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,   -- race | weekly_distance | monthly_distance | easy_hr_share
    label       TEXT,
    distance_m  REAL,            -- race: race distance
    target      REAL NOT NULL,   -- race: seconds; distance goals: meters; easy_hr_share: 0..1
    target_date TEXT,            -- race day, optional
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    message     TEXT
);
"""


_ready: set[Path] = set()  # databases this process has already set up
_setup_lock = threading.Lock()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    # Set-up writes to the database, so it runs once per process rather than on every request:
    # a write would have to wait for a sync that is in the middle of saving.
    setup = path not in _ready or not path.exists()
    if setup:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if setup:
        with _setup_lock:
            _set_up(conn)
            _ready.add(path)
    return conn


def _set_up(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    from . import zones

    zones.backfill(conn)


LATER_COLUMNS = {
    "activities": ("z1_s", "z2_s", "z3_s", "z4_s", "z5_s"),
    "samples": ("gct_ms", "vo_mm", "vr_pct", "step_mm", "balance_pct", "power_w"),
    "laps": ("cadence_spm", "gct_ms", "vo_mm", "vr_pct", "step_mm", "balance_pct", "power_w"),
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Columns added after a database was first created."""
    for table, cols in LATER_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
        if table == "activities" and "event_type" not in have:
            conn.execute("ALTER TABLE activities ADD COLUMN event_type TEXT")
            conn.execute(
                "UPDATE activities SET event_type = json_extract(raw_json, '$.eventType.typeKey') WHERE json_valid(raw_json)"
            )
    conn.commit()
