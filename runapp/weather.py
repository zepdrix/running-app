"""Weather for outdoor runs, and how much the heat cost you.

Garmin attaches observations from a nearby weather station to each outdoor
activity. Heat cost uses the runners' "temperature + dew point" rule (both in
°F): at the same effort you run this many percent slower, and your heart rate
runs correspondingly higher at the same pace. Heat-adjusted numbers undo that
cost, so a humid August run and a cool October run can be compared fairly.

Wind isn't adjusted for: its effect depends on the route (head- vs tailwind).
"""

import json
import logging
import math
import sqlite3
import time

log = logging.getLogger(__name__)

INDOOR_TYPES = {"treadmill_running", "indoor_running", "virtual_run", "indoor_track"}
# (temp + dew point in °F, pace cost in %), interpolated linearly in between.
HEAT_TABLE = [(100, 0.0), (110, 0.5), (120, 1.0), (130, 2.0), (140, 3.0), (150, 4.5), (160, 6.0), (170, 8.0), (180, 10.0)]
MAX_COST = 12.0


def heat_cost_pct(temp_f: float | None, dew_point_f: float | None) -> float | None:
    if temp_f is None or dew_point_f is None:
        return None
    total = temp_f + dew_point_f
    if total <= HEAT_TABLE[0][0]:
        return 0.0
    for (t0, c0), (t1, c1) in zip(HEAT_TABLE, HEAT_TABLE[1:]):
        if total <= t1:
            return c0 + (c1 - c0) * (total - t0) / (t1 - t0)
    return min(MAX_COST, HEAT_TABLE[-1][1] + (total - HEAT_TABLE[-1][0]) * 0.2)


def dew_point_from_humidity(temp_f: float, humidity_pct: float) -> float:
    """Magnus formula, for observations that report humidity but not dew point."""
    t = (temp_f - 32) * 5 / 9
    a, b = 17.62, 243.12
    gamma = math.log(max(humidity_pct, 1) / 100) + a * t / (b + t)
    return b * gamma / (a - gamma) * 9 / 5 + 32


def adjust_speed(speed: float, cost_pct: float | None) -> float:
    """Speed you'd have run in cool weather at the same effort."""
    return speed * (1 + (cost_pct or 0) / 100)


def is_outdoor(type_key: str | None) -> bool:
    return bool(type_key) and type_key not in INDOOR_TYPES


def _store(conn: sqlite3.Connection, activity_id: int, w: dict | None) -> None:
    w = w or {}
    conn.execute(
        "INSERT OR REPLACE INTO activity_weather VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (activity_id, w.get("temp"), w.get("dewPoint"), w.get("relativeHumidity"), w.get("windSpeed"),
         w.get("windDirectionCompassPoint"), (w.get("weatherTypeDTO") or {}).get("desc"), json.dumps(w)),
    )


def sync_weather(client, conn: sqlite3.Connection, pause_s: float = 0.25, progress=None) -> int:
    """Fetch weather for outdoor runs that don't have it yet (empty results are stored too, so they aren't retried)."""
    todo = [
        r["id"] for r in conn.execute(
            """SELECT a.id, a.type FROM activities a LEFT JOIN activity_weather w ON w.activity_id = a.id
               WHERE a.is_run = 1 AND w.activity_id IS NULL ORDER BY a.start_local DESC"""
        )
        if is_outdoor(r["type"])
    ]
    for n, activity_id in enumerate(todo, 1):
        try:
            _store(conn, activity_id, client.get_activity_weather(activity_id))
        except Exception as e:
            log.warning("No weather for %s: %s", activity_id, e)
            continue
        conn.commit()  # each one, so the database isn't locked while waiting on Garmin
        if n % 25 == 0 and progress:
            progress(f"Fetching weather {n}/{len(todo)}")
        time.sleep(pause_s)
    conn.commit()
    return len(todo)


def _summary(r: sqlite3.Row) -> dict | None:
    if r["temp_f"] is None:
        return None
    dew = r["dew_point_f"]
    if dew is None and r["humidity"] is not None:
        dew = round(dew_point_from_humidity(r["temp_f"], r["humidity"]), 1)
    cost = heat_cost_pct(r["temp_f"], dew)
    return {
        "temp_f": r["temp_f"], "dew_point_f": dew, "humidity": r["humidity"],
        "wind_mph": r["wind_mph"], "wind_dir": r["wind_dir"], "condition": r["condition"],
        "heat_cost_pct": round(cost, 2) if cost is not None else None,
    }


def for_activity(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM activity_weather WHERE activity_id = ?", (activity_id,)).fetchone()
    return _summary(r) if r else None


def all_weather(conn: sqlite3.Connection) -> dict[int, dict]:
    """activity_id -> weather summary, for runs that have it."""
    out = {}
    for r in conn.execute("SELECT * FROM activity_weather WHERE temp_f IS NOT NULL"):
        out[r["activity_id"]] = _summary(r)
    return out
