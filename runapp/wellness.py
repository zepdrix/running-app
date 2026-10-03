"""Daily body data from Garmin: sleep, HRV, resting HR, Body Battery, stress,
training readiness and training status.

Raw responses are stored per (date, kind); summarize() pulls out the handful
of numbers the dashboard and the AI status need. Watches aren't worn every
night, so every field may be missing.
"""

import json
import logging
import sqlite3
import time
from datetime import date, timedelta

from . import config

log = logging.getLogger(__name__)

FIRST_SYNC_DAYS = 14
LATER_SYNC_DAYS = 3


def _fetchers(client):
    return {
        "stats": client.get_stats,
        "sleep": client.get_sleep_data,
        "hrv": client.get_hrv_data,
        "readiness": client.get_training_readiness,
    }


def sync_wellness(client, conn: sqlite3.Connection, today: date | None = None) -> None:
    today = today or date.today()
    have_any = conn.execute("SELECT 1 FROM wellness LIMIT 1").fetchone()
    days = LATER_SYNC_DAYS if have_any else FIRST_SYNC_DAYS
    folder = config.RAW_DIR / "wellness"
    folder.mkdir(parents=True, exist_ok=True)

    def save(day: str, kind: str, data) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO wellness (date, kind, raw_json, fetched_at) VALUES (?, ?, ?, datetime('now'))",
            (day, kind, json.dumps(data)),
        )
        conn.commit()  # each one, so the database isn't locked while waiting on Garmin
        (folder / f"{day}_{kind}.json").write_text(json.dumps(data, indent=1))

    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        for kind, fn in _fetchers(client).items():
            try:
                save(day, kind, fn(day))
            except Exception as e:
                log.warning("Could not fetch %s for %s: %s", kind, day, e)
            time.sleep(0.2)
    try:
        save(today.isoformat(), "training_status", client.get_training_status(today.isoformat()))
    except Exception as e:
        log.warning("Could not fetch training status: %s", e)
    conn.commit()


def _raw(conn: sqlite3.Connection, day: str, kind: str):
    row = conn.execute("SELECT raw_json FROM wellness WHERE date = ? AND kind = ?", (day, kind)).fetchone()
    return json.loads(row["raw_json"]) if row else None


def _day_summary(conn: sqlite3.Connection, day: str) -> dict:
    out: dict = {"date": day}
    stats = _raw(conn, day, "stats") or {}
    if stats.get("totalSteps") is not None or stats.get("restingHeartRate") is not None:
        out.update(
            steps=stats.get("totalSteps"),
            resting_hr=stats.get("restingHeartRate"),
            body_battery_high=stats.get("bodyBatteryHighestValue"),
            body_battery_low=stats.get("bodyBatteryLowestValue"),
            body_battery_now=stats.get("bodyBatteryMostRecentValue"),
            avg_stress=stats.get("averageStressLevel") if (stats.get("averageStressLevel") or -1) >= 0 else None,
        )

    sleep = ((_raw(conn, day, "sleep") or {}).get("dailySleepDTO")) or {}
    if sleep.get("sleepTimeSeconds"):
        out["sleep"] = {
            "hours": round(sleep["sleepTimeSeconds"] / 3600, 1),
            "score": ((sleep.get("sleepScores") or {}).get("overall") or {}).get("value"),
            "deep_h": round((sleep.get("deepSleepSeconds") or 0) / 3600, 1),
            "rem_h": round((sleep.get("remSleepSeconds") or 0) / 3600, 1),
            "awake_h": round((sleep.get("awakeSleepSeconds") or 0) / 3600, 1),
        }

    hrv = ((_raw(conn, day, "hrv") or {}).get("hrvSummary")) or {}
    if hrv.get("lastNightAvg"):
        out["hrv"] = {
            "last_night_avg": hrv.get("lastNightAvg"),
            "weekly_avg": hrv.get("weeklyAvg"),
            "status": hrv.get("status"),
        }

    readiness = _raw(conn, day, "readiness")
    if isinstance(readiness, list):
        readiness = max(readiness, key=lambda r: r.get("timestamp") or "", default=None)
    if readiness and readiness.get("score") is not None:
        out["readiness"] = {
            "score": readiness.get("score"),
            "level": readiness.get("level"),
            "recovery_time_h": round((readiness.get("recoveryTime") or 0) / 60, 1),
            "as_of": readiness.get("timestampLocal"),
        }
    return out


def _training_status(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT raw_json FROM wellness WHERE kind = 'training_status' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["raw_json"]) or {}
    latest = ((data.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")) or {}
    for device in latest.values():
        acute = device.get("acuteTrainingLoadDTO") or {}
        return {
            "date": device.get("calendarDate"),
            "status": device.get("trainingStatusFeedbackPhrase"),
            "acute_load": acute.get("dailyTrainingLoadAcute"),
            "chronic_load": acute.get("dailyTrainingLoadChronic"),
            "acute_chronic_ratio": acute.get("dailyAcuteChronicWorkloadRatio"),
            "ratio_status": acute.get("acwrStatus"),
        }
    return None


def summarize(conn: sqlite3.Connection, today: date | None = None, days: int = 3) -> dict:
    """Most recent day first. Days without any watch data are listed as empty."""
    today = today or date.today()
    return {
        "days": [_day_summary(conn, (today - timedelta(days=i)).isoformat()) for i in range(days)],
        "training_status": _training_status(conn),
    }


def headline_metrics(summary: dict) -> list[dict]:
    """The numbers shown under the AI status. Takes the newest day that has each one."""
    def newest(getter):
        for d in summary["days"]:
            value = getter(d)
            if value is not None:
                return value, d["date"]
        return None, None

    metrics = []
    for label, getter, fmt in [
        ("Readiness", lambda d: (d.get("readiness") or {}).get("score"), lambda v: f"{v}"),
        ("Sleep", lambda d: (d.get("sleep") or {}).get("hours"), lambda v: f"{v} h"),
        ("Resting HR", lambda d: d.get("resting_hr"), lambda v: f"{v} bpm"),
        ("HRV", lambda d: (d.get("hrv") or {}).get("last_night_avg"), lambda v: f"{v} ms"),
        ("Body Battery", lambda d: d.get("body_battery_now"), lambda v: f"{v}"),
    ]:
        value, day = newest(getter)
        if value is not None:
            metrics.append({"label": label, "value": fmt(value), "date": day})
    status = summary.get("training_status")
    if status and status.get("status"):
        metrics.append({"label": "Garmin status", "value": status["status"].split("_")[0].title(), "date": status["date"]})
    return metrics
