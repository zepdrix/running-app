"""Training analysis over the local database. Everything is metric (meters, seconds)."""

import json
import sqlite3
from datetime import date, datetime, timedelta

from . import config, races, weather, zones

RIEGEL_EXPONENT = 1.06
RACE_DISTANCES = {"5k": 5000, "10k": 10000, "Half marathon": 21097.5, "Marathon": 42195}
GARMIN_PREDICTION_KEYS = {
    "5k": "time5K",
    "10k": "time10K",
    "Half marathon": "timeHalfMarathon",
    "Marathon": "timeMarathon",
}


WORKOUT_KINDS = ["easy", "long", "threshold", "intervals", "race"]


def workout_kind(r) -> str:
    """Classify an activity row by what it did to you.

    race: marked as a race in Garmin. intervals: 5+ min in HR zone 5 or anaerobic
    training effect >= 2. threshold: 15+ min (or 25%+ of the run) in zone 3 or above,
    i.e. Norwegian sub-threshold work or harder. long: 75+ min or 12+ km. Everything
    else that's a run is easy; other sports are cross-training. Zones are the
    Norwegian ones from zones.py.
    """
    if not r["is_run"]:
        return "cross"
    if r["event_type"] == "race":
        return "race"
    z = zones.seconds(r)
    total, hard = sum(z), z[2] + z[3] + z[4]
    if z[4] >= 300 or (r["anaerobic_te"] or 0) >= 2.0:
        return "intervals"
    if hard >= 900 or (total and hard / total >= 0.25):
        return "threshold"
    if (r["duration_s"] or 0) >= 4500 or (r["distance_m"] or 0) >= 12000:
        return "long"
    return "easy"


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _day(start_local: str) -> date:
    return datetime.fromisoformat(start_local[:19]).date()


def _runs(conn: sqlite3.Connection, since: date | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM activities WHERE is_run = 1"
    params: list = []
    if since:
        sql += " AND start_local >= ?"
        params.append(since.isoformat())
    return conn.execute(sql + " ORDER BY start_local", params).fetchall()


def weekly(conn: sqlite3.Connection, weeks: int = 26, today: date | None = None) -> list[dict]:
    today = today or date.today()
    first = _monday(today) - timedelta(weeks=weeks - 1)
    buckets = {
        first + timedelta(weeks=i): {
            "week": (first + timedelta(weeks=i)).isoformat(),
            "distance_m": 0.0,
            "duration_s": 0.0,
            "runs": 0,
            "long_run_m": 0.0,
            "load": 0.0,
            "zones_s": [0.0] * 5,
            "by_kind_m": {k: 0.0 for k in WORKOUT_KINDS},
        }
        for i in range(weeks)
    }
    for r in _runs(conn, first):
        b = buckets.get(_monday(_day(r["start_local"])))
        if not b:
            continue
        b["distance_m"] += r["distance_m"] or 0
        b["duration_s"] += r["duration_s"] or 0
        b["runs"] += 1
        b["long_run_m"] = max(b["long_run_m"], r["distance_m"] or 0)
        b["load"] += r["training_load"] or 0
        b["by_kind_m"][workout_kind(r)] += r["distance_m"] or 0
        for z in range(5):
            b["zones_s"][z] += r[f"z{z + 1}_s"] or 0
    return list(buckets.values())


def monthly(conn: sqlite3.Connection, months: int = 12, today: date | None = None) -> list[dict]:
    today = today or date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    out = {k: {"month": k, "distance_m": 0.0, "duration_s": 0.0, "runs": 0} for k in keys}
    for r in _runs(conn, date.fromisoformat(keys[0] + "-01")):
        b = out.get(r["start_local"][:7])
        if b:
            b["distance_m"] += r["distance_m"] or 0
            b["duration_s"] += r["duration_s"] or 0
            b["runs"] += 1
    return list(out.values())


def load_ratio(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Acute:chronic ratio of running distance (last 7 days vs 28-day weekly average).

    Roughly 0.8-1.3 is a steady build; well above 1.5 is a sharp jump in volume.
    """
    today = today or date.today()
    runs = _runs(conn, today - timedelta(days=27))
    acute = sum(r["distance_m"] or 0 for r in runs if _day(r["start_local"]) > today - timedelta(days=7))
    chronic = sum(r["distance_m"] or 0 for r in runs) / 4
    return {
        "acute_m": acute,
        "chronic_weekly_m": chronic,
        "ratio": acute / chronic if chronic else None,
    }


def efficiency(conn: sqlite3.Connection, days: int = 365, today: date | None = None) -> list[dict]:
    """Aerobic efficiency: meters per minute per heartbeat-per-minute.

    Rising efficiency on comparable easy runs means you're getting fitter.
    Only steady runs of 20+ minutes with HR data are included.
    """
    today = today or date.today()
    wx = weather.all_weather(conn)
    out = []
    for r in _runs(conn, today - timedelta(days=days)):
        if not (r["avg_hr"] and r["avg_speed_mps"] and (r["duration_s"] or 0) >= 1200):
            continue
        w = wx.get(r["id"])
        cost = w["heat_cost_pct"] if w else None
        out.append(
            {
                "id": r["id"],
                "date": r["start_local"][:10],
                "name": r["name"],
                "avg_hr": r["avg_hr"],
                "pace_s_per_km": 1000 / r["avg_speed_mps"],
                "ef": r["avg_speed_mps"] * 60 / r["avg_hr"],
                # Same, with the heat's cost removed: what the run says about fitness in cool weather.
                "ef_adj": weather.adjust_speed(r["avg_speed_mps"], cost) * 60 / r["avg_hr"],
                "weather": w,
                "indoor": not weather.is_outdoor(r["type"]),
                "kind": workout_kind(r),
                "easy": sum(zones.seconds(r)[:2]) >= 0.7 * sum(zones.seconds(r)),
            }
        )
    return out


# Records and predictions count outdoor runs only: a treadmill's distance is an estimate.
# (Indoor best efforts stay in the best_efforts table.)
_INDOOR = sorted(weather.INDOOR_TYPES)
_OUTDOOR_SQL = f"a.type NOT IN ({', '.join('?' * len(_INDOOR))})"


def personal_records(conn: sqlite3.Connection, recent_days: int = 90, today: date | None = None) -> list[dict]:
    today = today or date.today()
    cutoff = (today - timedelta(days=recent_days)).isoformat()
    out = []
    for label, dist in config.BEST_EFFORT_DISTANCES.items():
        row = {"label": label, "distance_m": dist, "all_time": None, "recent": None}
        for key, where in (("all_time", ""), ("recent", "AND a.start_local >= ?")):
            params = [label, *_INDOOR] + ([cutoff] if where else [])
            best = conn.execute(
                f"""SELECT b.time_s, a.id, a.name, a.start_local FROM best_efforts b
                    JOIN activities a ON a.id = b.activity_id
                    WHERE b.label = ? AND {_OUTDOOR_SQL} {where} ORDER BY b.time_s LIMIT 1""",
                params,
            ).fetchone()
            if best:
                row[key] = {
                    "time_s": best["time_s"],
                    "activity_id": best["id"],
                    "name": best["name"],
                    "date": best["start_local"][:10],
                }
        out.append(row)
    return out


def riegel(time_s: float, from_m: float, to_m: float) -> float:
    return time_s * (to_m / from_m) ** RIEGEL_EXPONENT


def _recent_efforts(conn: sqlite3.Connection, recent_days: int, today: date) -> list[sqlite3.Row]:
    # Efforts under 3k overpredict long races, so they're not used for projections.
    cutoff = (today - timedelta(days=recent_days)).isoformat()
    return conn.execute(
        f"""SELECT b.label, b.distance_m, MIN(b.time_s) AS time_s FROM best_efforts b
            JOIN activities a ON a.id = b.activity_id
            WHERE a.start_local >= ? AND b.distance_m >= 3000 AND {_OUTDOOR_SQL} GROUP BY b.label""",
        (cutoff, *_INDOOR),
    ).fetchall()


def project(efforts: list[sqlite3.Row], distance_m: float) -> dict | None:
    """The fastest Riegel projection to distance_m from any of the given efforts."""
    best = None
    for e in efforts:
        t = riegel(e["time_s"], e["distance_m"], distance_m)
        if best is None or t < best["time_s"]:
            best = {"time_s": t, "from": e["label"]}
    return best


def _race_efforts(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Official all-out race results from the last 12 months, shaped like best efforts."""
    return [{"label": r["label"], "distance_m": r["distance_m"], "time_s": r["time_s"]}
            for r in races.all_out_results(conn, today, 365)]


def predictions(conn: sqlite3.Connection, recent_days: int = 90, today: date | None = None) -> list[dict]:
    """Race-time predictions from recent training efforts, official race results, and Garmin's predictor."""
    today = today or date.today()
    efforts = _recent_efforts(conn, recent_days, today)
    race_efforts = _race_efforts(conn, today)
    garmin = latest_snapshot(conn, "race_predictions") or {}
    if isinstance(garmin, list):
        garmin = garmin[-1] if garmin else {}
    return [
        {
            "label": label,
            "distance_m": dist,
            "riegel": project(efforts, dist),
            "from_race": project(race_efforts, dist),
            "garmin_s": garmin.get(GARMIN_PREDICTION_KEYS[label]),
        }
        for label, dist in RACE_DISTANCES.items()
    ]


def latest_snapshot(conn: sqlite3.Connection, kind: str):
    row = conn.execute(
        "SELECT raw_json FROM snapshots WHERE kind = ? ORDER BY taken_at DESC LIMIT 1", (kind,)
    ).fetchone()
    return json.loads(row["raw_json"]) if row else None


def zone_share(conn: sqlite3.Connection, days: int = 28, today: date | None = None) -> dict:
    today = today or date.today()
    totals = [0.0] * 5
    for r in _runs(conn, today - timedelta(days=days - 1)):
        for z in range(5):
            totals[z] += r[f"z{z + 1}_s"] or 0
    total = sum(totals)
    return {
        "zones_s": totals,
        "easy_share": (totals[0] + totals[1]) / total if total else None,
    }


def goal_progress(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    today = today or date.today()
    efforts = _recent_efforts(conn, 90, today)
    race_efforts = _race_efforts(conn, today)
    weeks = weekly(conn, 9, today)
    this_week, past_weeks = weeks[-1], weeks[:-1]
    this_month = monthly(conn, 1, today)[0]
    out = []
    for g in conn.execute("SELECT * FROM goals ORDER BY id").fetchall():
        item = dict(g)
        if g["kind"] == "race":
            # Fastest of: recent training efforts (90 days) and official all-out races (12 months).
            candidates = [p for p in (project(efforts, g["distance_m"]), project(race_efforts, g["distance_m"])) if p]
            best = min(candidates, key=lambda p: p["time_s"]) if candidates else None
            current_s = best["time_s"] if best else None
            item["current"] = current_s
            item["basis"] = best["from"] if best else None
            item["on_track"] = current_s is not None and current_s <= g["target"]
            item["gap_s"] = current_s - g["target"] if current_s is not None else None
            if g["target_date"]:
                item["days_left"] = (date.fromisoformat(g["target_date"]) - today).days
        elif g["kind"] == "weekly_distance":
            item["current"] = this_week["distance_m"]
            item["progress"] = this_week["distance_m"] / g["target"]
            item["weeks_hit"] = sum(w["distance_m"] >= g["target"] for w in past_weeks)
            item["weeks_total"] = len(past_weeks)
        elif g["kind"] == "monthly_distance":
            item["current"] = this_month["distance_m"]
            item["progress"] = this_month["distance_m"] / g["target"]
        elif g["kind"] == "easy_hr_share":
            share = zone_share(conn, 28, today)["easy_share"]
            item["current"] = share
            item["on_track"] = share is not None and share >= g["target"]
        out.append(item)
    return out


def overview(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    weeks = weekly(conn, 5, today)
    # Prefer the daily precise value; fall back to the whole number on activities.
    vo2 = conn.execute(
        "SELECT precise AS vo2max, date AS start_local FROM vo2max_daily ORDER BY date DESC LIMIT 1"
    ).fetchone() or conn.execute(
        "SELECT vo2max, start_local FROM activities WHERE vo2max IS NOT NULL ORDER BY start_local DESC LIMIT 1"
    ).fetchone()
    last_sync = conn.execute(
        "SELECT finished_at, status, message FROM sync_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    counts = conn.execute(
        "SELECT COUNT(*) AS total, SUM(is_run) AS runs, MIN(start_local) AS first FROM activities"
    ).fetchone()
    return {
        "this_week_m": weeks[-1]["distance_m"],
        "prev_4wk_avg_m": sum(w["distance_m"] for w in weeks[:-1]) / 4,
        "load": load_ratio(conn, today),
        "vo2max": dict(vo2) if vo2 else None,
        "zones_28d": zone_share(conn, 28, today),
        "hr_zones": zones.describe(conn),
        "last_sync": dict(last_sync) if last_sync else None,
        "activity_count": counts["total"],
        "run_count": counts["runs"] or 0,
        "first_activity": counts["first"],
    }


def recent_activities(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT * FROM activities ORDER BY start_local DESC LIMIT ?", (limit,)).fetchall()
    keep = ["id", "name", "type", "is_run", "start_local", "distance_m", "duration_s", "avg_speed_mps",
            "avg_hr", "elevation_gain_m", "training_load"]
    wx = weather.all_weather(conn)
    return [{**{k: r[k] for k in keep}, "kind": workout_kind(r), "weather": wx.get(r["id"])} for r in rows]
