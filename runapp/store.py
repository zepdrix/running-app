"""Write activities and their derived data into SQLite."""

import json
import sqlite3

from . import config, form, zones
from .streams import Lap, Sample, best_efforts

ZONE_KEYS = [f"hrTimeInZone_{i}" for i in range(1, 6)]


def is_run(type_key: str | None) -> bool:
    return bool(type_key) and "run" in type_key


def summary_row(summary: dict) -> dict:
    type_key = (summary.get("activityType") or {}).get("typeKey")
    return {
        "id": summary["activityId"],
        "name": summary.get("activityName"),
        "type": type_key,
        "is_run": int(is_run(type_key)),
        "event_type": (summary.get("eventType") or {}).get("typeKey"),
        "start_local": summary.get("startTimeLocal"),
        "start_gmt": summary.get("startTimeGMT"),
        "distance_m": summary.get("distance"),
        "duration_s": summary.get("duration"),
        "moving_s": summary.get("movingDuration"),
        "elevation_gain_m": summary.get("elevationGain"),
        "avg_speed_mps": summary.get("averageSpeed"),
        "avg_hr": summary.get("averageHR"),
        "max_hr": summary.get("maxHR"),
        "avg_cadence": summary.get("averageRunningCadenceInStepsPerMinute"),
        "calories": summary.get("calories"),
        "training_load": summary.get("activityTrainingLoad"),
        "aerobic_te": summary.get("aerobicTrainingEffect"),
        "anaerobic_te": summary.get("anaerobicTrainingEffect"),
        "vo2max": summary.get("vO2MaxValue"),
        **{f"hr_zone_{i}_s": summary.get(k) for i, k in enumerate(ZONE_KEYS, 1)},
        "raw_json": json.dumps(summary),
    }


def upsert_summary(conn: sqlite3.Connection, summary: dict) -> None:
    row = summary_row(summary)
    cols = list(row)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO activities ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def set_hr_zones(conn: sqlite3.Connection, activity_id: int, zones: list[dict]) -> None:
    """Fill zone times from the /hrTimeInZones endpoint when the summary lacked them."""
    for z in zones or []:
        n = z.get("zoneNumber")
        if n in range(1, 6):
            conn.execute(
                f"UPDATE activities SET hr_zone_{n}_s = COALESCE(hr_zone_{n}_s, ?) WHERE id = ?",
                (z.get("secsInZone"), activity_id),
            )


def store_detail(
    conn: sqlite3.Connection,
    activity_id: int,
    samples: list[Sample],
    laps: list[Lap],
) -> None:
    conn.execute("DELETE FROM samples WHERE activity_id = ?", (activity_id,))
    conn.execute("DELETE FROM laps WHERE activity_id = ?", (activity_id,))
    conn.execute("DELETE FROM best_efforts WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        """INSERT INTO samples (activity_id, t, distance_m, hr, speed_mps, cadence, altitude_m, lat, lon,
                                gct_ms, vo_mm, vr_pct, step_mm, balance_pct, power_w)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (activity_id, s.t, s.distance_m, s.hr, s.speed_mps, s.cadence, s.altitude_m, s.lat, s.lon,
             s.gct_ms, s.vo_mm, s.vr_pct, s.step_mm, s.balance_pct, s.power_w)
            for s in samples
        ],
    )
    conn.executemany(
        """INSERT INTO laps (activity_id, idx, distance_m, duration_s, avg_hr, avg_speed_mps,
                             cadence_spm, gct_ms, vo_mm, vr_pct, step_mm, balance_pct, power_w)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(activity_id, i, l.distance_m, l.duration_s, l.avg_hr, l.avg_speed_mps,
          l.cadence_spm, l.gct_ms, l.vo_mm, l.vr_pct, l.step_mm, l.balance_pct, l.power_w) for i, l in enumerate(laps)],
    )
    run = conn.execute("SELECT is_run FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if run and run["is_run"]:
        efforts = best_efforts(samples, config.BEST_EFFORT_DISTANCES)
        conn.executemany(
            "INSERT INTO best_efforts VALUES (?, ?, ?, ?)",
            [(activity_id, label, config.BEST_EFFORT_DISTANCES[label], t) for label, t in efforts.items()],
        )
    conn.execute(
        "UPDATE activities SET has_samples = ?, detail_synced = 1 WHERE id = ?",
        (int(bool(samples)), activity_id),
    )
    zones.recompute(conn, [activity_id])
    if run and run["is_run"]:
        form.ensure(conn, [activity_id])
