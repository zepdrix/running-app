"""Norwegian (Olympiatoppen) heart-rate zones, computed from your second-by-second HR.

Zones are percentages of max heart rate:
  Z1 60-72% (easy) · Z2 72-82% (easy) · Z3 82-87% (sub-threshold) · Z4 87-92% · Z5 92%+
Time below zone 1 (warm-up walking etc.) counts toward zone 1.

Garmin's own zone times stay in the hr_zone_N_s columns; the zN_s columns hold
these zones and are what the analysis uses. Activities without HR samples fall
back to Garmin's numbers.
"""

import sqlite3

ZONE_PCTS = [0.60, 0.72, 0.82, 0.87, 0.92]  # lower bound of zones 1-5, as a share of max HR
ZONE_NAMES = ["Zone 1", "Zone 2", "Zone 3", "Zone 4", "Zone 5"]
DEFAULT_MAX_HR = 190
MAX_GAP_S = 10  # longer gaps between samples are pauses, not time spent at that HR


def get_max_hr(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM coach_state WHERE key = 'max_hr'").fetchone()
    return int(row["value"]) if row and row["value"] else DEFAULT_MAX_HR


def bounds(max_hr: int) -> list[int]:
    """Lower bound (bpm) of each zone."""
    return [round(max_hr * p) for p in ZONE_PCTS]


def describe(conn: sqlite3.Connection) -> dict:
    max_hr = get_max_hr(conn)
    b = bounds(max_hr)
    ranges = [f"{b[i]}-{b[i + 1] - 1}" for i in range(4)] + [f"{b[4]}+"]
    return {"max_hr": max_hr, "bounds": b, "ranges": ranges}


def set_max_hr(conn: sqlite3.Connection, max_hr: int) -> None:
    conn.execute("INSERT OR REPLACE INTO coach_state VALUES ('max_hr', ?)", (str(int(max_hr)),))
    recompute(conn)


def recompute(conn: sqlite3.Connection, activity_ids: list[int] | None = None) -> None:
    b = bounds(get_max_hr(conn))
    where, params = "", []
    if activity_ids is not None:
        if not activity_ids:
            return
        where = f"AND activity_id IN ({','.join('?' * len(activity_ids))})"
        params = list(activity_ids)
    rows = conn.execute(
        f"""WITH s AS (
              SELECT activity_id, hr,
                     MIN(COALESCE(LEAD(t) OVER (PARTITION BY activity_id ORDER BY t) - t, 1), {MAX_GAP_S}) AS dt
              FROM samples WHERE hr IS NOT NULL {where})
            SELECT activity_id,
              SUM(CASE WHEN hr < {b[1]} THEN dt ELSE 0 END),
              SUM(CASE WHEN hr >= {b[1]} AND hr < {b[2]} THEN dt ELSE 0 END),
              SUM(CASE WHEN hr >= {b[2]} AND hr < {b[3]} THEN dt ELSE 0 END),
              SUM(CASE WHEN hr >= {b[3]} AND hr < {b[4]} THEN dt ELSE 0 END),
              SUM(CASE WHEN hr >= {b[4]} THEN dt ELSE 0 END)
            FROM s GROUP BY activity_id""",
        params,
    ).fetchall()
    conn.executemany(
        "UPDATE activities SET z1_s = ?, z2_s = ?, z3_s = ?, z4_s = ?, z5_s = ? WHERE id = ?",
        [(*r[1:], r[0]) for r in rows],
    )
    # No HR stream: use Garmin's zone times rather than nothing.
    computed = {r[0] for r in rows}
    target = activity_ids if activity_ids is not None else [r[0] for r in conn.execute("SELECT id FROM activities")]
    conn.executemany(
        """UPDATE activities SET z1_s = hr_zone_1_s, z2_s = hr_zone_2_s, z3_s = hr_zone_3_s,
           z4_s = hr_zone_4_s, z5_s = hr_zone_5_s WHERE id = ?""",
        [(i,) for i in target if i not in computed],
    )
    conn.commit()


def backfill(conn: sqlite3.Connection) -> None:
    """Fill zone times for activities that don't have them yet (e.g. right after upgrading)."""
    # Only activities with something to fill from; ones with no HR data at all stay empty and are skipped.
    missing = [r[0] for r in conn.execute(
        """SELECT id FROM activities WHERE z1_s IS NULL AND (hr_zone_1_s IS NOT NULL OR EXISTS (
               SELECT 1 FROM samples WHERE activity_id = activities.id AND hr IS NOT NULL))"""
    )]
    if missing:
        recompute(conn, missing)


def seconds(row) -> list[float]:
    return [row[f"z{z}_s"] or 0 for z in range(1, 6)]


def prompt_text(conn: sqlite3.Connection) -> str:
    """How the AI should talk about intensity."""
    d = describe(conn)
    r = d["ranges"]
    return (
        f"The athlete trains by Norwegian (Olympiatoppen) heart-rate zones based on max HR {d['max_hr']}: "
        f"zone 1 {r[0]} bpm and zone 2 {r[1]} bpm are easy/low intensity, zone 3 {r[2]} bpm is "
        f"sub-threshold (Norwegian threshold work), zone 4 {r[3]} bpm, zone 5 {r[4]} bpm. Always use these "
        "zones when you talk about zones or whether a run was easy. In the data, z1_s..z5_s (and "
        "hr_zone_minutes) are time in these zones; hr_zone_1_s..hr_zone_5_s are Garmin's own, differently "
        "set zones: never quote those."
    )
