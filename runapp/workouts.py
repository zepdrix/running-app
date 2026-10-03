"""Threshold and interval session check: find the reps, judge each against the zones.

Reps come from lap data (structured workouts and manual lap presses both create
laps). A lap counts as a rep when it lasts 45+ seconds and is within 15% of the
session's fastest lap speed, which separates reps from warm-ups, cool-downs and
recoveries.

Verdicts use the Norwegian zones: zone 3 (or below) is controlled sub-threshold
work, zone 4 is running hot, zone 5 is too hard for threshold work.
"""

import sqlite3
from datetime import date, timedelta

from . import analysis, weather, zones

MILE = 1609.344
MIN_REP_S = 45
REP_SPEED_SHARE = 0.85
REFERENCE_HR = 160  # "pace at 160 bpm": the middle of your sub-threshold range
SHORT_REP_S = 150   # heart rate lags on short reps, so short and long reps are trended separately


def _zone_of(hr: float, bounds: list[int]) -> int:
    return sum(hr >= b for b in bounds[1:]) + 1  # 1..5; below zone 1 counts as zone 1


def reps(conn: sqlite3.Connection, activity_id: int) -> list[dict]:
    laps = [dict(r) for r in conn.execute(
        "SELECT idx, distance_m, duration_s, avg_hr, avg_speed_mps, cadence_spm, gct_ms, balance_pct FROM laps WHERE activity_id = ? ORDER BY idx",
        (activity_id,),
    )]
    usable = [l for l in laps if (l["duration_s"] or 0) >= MIN_REP_S and l["avg_speed_mps"]]
    if not usable:
        return []
    fastest = max(l["avg_speed_mps"] for l in usable)
    bounds = zones.bounds(zones.get_max_hr(conn))
    out = []
    for l in usable:
        if l["avg_speed_mps"] < REP_SPEED_SHARE * fastest:
            continue
        zone = _zone_of(l["avg_hr"], bounds) if l["avg_hr"] else None
        out.append({
            "n": len(out) + 1,
            "lap": l["idx"],
            "duration_s": l["duration_s"],
            "distance_m": l["distance_m"],
            "pace_s_per_mi": MILE / l["avg_speed_mps"],
            "avg_hr": l["avg_hr"],
            "zone": zone,
            "verdict": None if zone is None else "controlled" if zone <= 3 else "hot" if zone == 4 else "too_hard",
            "cadence": l["cadence_spm"],
            "gct_ms": l["gct_ms"],
            "balance": l["balance_pct"],
        })
    return out


def _pattern(rs: list[dict]) -> str:
    """'5 × 6:00' when reps share a duration, '4 × 1.0 mi' when they share a distance."""
    n = len(rs)
    durs = [r["duration_s"] for r in rs]
    dists = [r["distance_m"] or 0 for r in rs]
    if max(durs) - min(durs) <= max(10, 0.08 * min(durs)):
        avg = round(sum(durs) / n)
        return f"{n} × {avg // 60}:{avg % 60:02d}"
    if dists and min(dists) and max(dists) - min(dists) <= 0.08 * min(dists):
        avg = sum(dists) / n
        return f"{n} × {avg / MILE:.2f} mi" if avg >= 800 else f"{n} × {round(avg, -1):.0f} m"
    return f"{n} reps"


def summarize(conn: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    rs = reps(conn, row["id"])
    if not rs:
        return None
    with_hr = [r for r in rs if r["avg_hr"]]
    work_s = sum(r["duration_s"] for r in rs)
    work_m = sum(r["distance_m"] or 0 for r in rs)
    # Pace you'd hold at 160 bpm, scaling each rep's speed by HR. A rough model, but
    # comparable from session to session: falling = fitter.
    usable = [r for r in with_hr if 140 <= r["avg_hr"] <= 185]
    pace_at_ref = None
    if usable:
        speed = sum((MILE / r["pace_s_per_mi"]) * REFERENCE_HR / r["avg_hr"] for r in usable) / len(usable)
        pace_at_ref = MILE / speed
    wx = weather.for_activity(conn, row["id"])
    cost = wx["heat_cost_pct"] if wx else None
    pace_at_ref_adj = MILE / weather.adjust_speed(MILE / pace_at_ref, cost) if pace_at_ref else None
    counts = {v: sum(r["verdict"] == v for r in rs) for v in ("controlled", "hot", "too_hard")}
    if counts["too_hard"]:
        verdict = "too_hard"
    elif counts["hot"] > len(rs) / 3:
        verdict = "hot"
    else:
        verdict = "controlled"
    return {
        "activity_id": row["id"],
        "date": row["start_local"][:10],
        "name": row["name"],
        "kind": analysis.workout_kind(row),
        "pattern": _pattern(rs),
        "reps": rs,
        "rep_count": len(rs),
        "avg_rep_pace_s_per_mi": work_s / (work_m / MILE) if work_m else None,
        "avg_rep_hr": sum(r["avg_hr"] for r in with_hr) / len(with_hr) if with_hr else None,
        "hr_drift": (with_hr[-1]["avg_hr"] - with_hr[0]["avg_hr"]) if len(with_hr) >= 2 else None,
        # Positive = the last rep was slower than the first (usually: started too fast).
        "pace_fade_s": rs[-1]["pace_s_per_mi"] - rs[0]["pace_s_per_mi"] if len(rs) >= 2 else None,
        "rep_length": "short" if sorted(r["duration_s"] for r in rs)[len(rs) // 2] < SHORT_REP_S else "long",
        "pace_at_160_s_per_mi": pace_at_ref,
        "pace_at_160_adj_s_per_mi": pace_at_ref_adj,  # heat removed
        "weather": wx,
        "verdict_counts": counts,
        "verdict": verdict,
    }


def sessions(conn: sqlite3.Connection, days: int = 365, today: date | None = None) -> list[dict]:
    """Threshold and interval sessions with their reps, newest first."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    out = []
    for row in conn.execute("SELECT * FROM activities WHERE is_run = 1 AND start_local >= ? ORDER BY start_local DESC", (since,)):
        if analysis.workout_kind(row) not in ("threshold", "intervals"):
            continue
        s = summarize(conn, row)
        if s:
            out.append(s)
    return out


def brief(conn: sqlite3.Connection, limit: int = 5, today: date | None = None) -> list[dict]:
    """Recent quality sessions for the coach and status card (paces in s/mile)."""
    keep = ("date", "name", "kind", "pattern", "rep_length", "avg_rep_hr", "hr_drift", "pace_fade_s", "pace_at_160_s_per_mi",
            "pace_at_160_adj_s_per_mi", "weather", "verdict", "verdict_counts")
    out = []
    for s in sessions(conn, 120, today)[:limit]:
        item = {k: s[k] for k in keep}
        item["reps"] = [{k: round(r[k]) if isinstance(r[k], float) else r[k] for k in ("n", "duration_s", "pace_s_per_mi", "avg_hr", "zone")}
                        for r in s["reps"]]
        out.append(item)
    return out
