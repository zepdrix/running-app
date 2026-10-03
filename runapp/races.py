"""Race log: official results, effort, courses and upcoming races.

Official times beat watch times (watches start early, stop late and measure
courses long). Races run easy on purpose are kept in the log but never used
for predictions or PRs.
"""

import sqlite3
from datetime import date, timedelta

from . import weather

EFFORTS = ["all_out", "controlled", "easy"]
MILE = 1609.344


def _activity(conn: sqlite3.Connection, activity_id: int | None) -> dict | None:
    if not activity_id:
        return None
    r = conn.execute(
        "SELECT id, name, start_local, distance_m, duration_s, avg_hr FROM activities WHERE id = ?", (activity_id,)
    ).fetchone()
    return dict(r) if r else None


def _result(race: dict) -> tuple[float | None, str | None]:
    """The time to use for a past race: official if known, else the watch."""
    if race.get("official_time_s"):
        return race["official_time_s"], "official"
    act = race.get("activity")
    if act and act.get("duration_s"):
        return act["duration_s"], "watch"
    return None, None


def all_races(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = (today or date.today()).isoformat()
    rows = [dict(r) for r in conn.execute("SELECT * FROM races ORDER BY date")]
    for r in rows:
        r["activity"] = _activity(conn, r["activity_id"])
        r["time_s"], r["time_source"] = _result(r)
        r["pace_s_per_mi"] = r["time_s"] / (r["distance_m"] / MILE) if r["time_s"] else None
        act = r["activity"]
        # How long this course measured on the watch (e.g. 1.05 = 5% long): pace off the mile markers.
        r["watch_distance_factor"] = act["distance_m"] / r["distance_m"] if act and act.get("distance_m") else None
        # Cool-weather equivalent: the same effort without the heat's cost.
        r["weather"] = weather.for_activity(conn, r["activity_id"]) if r["activity_id"] else None
        cost = r["weather"]["heat_cost_pct"] if r["weather"] else None
        r["adjusted_time_s"] = r["time_s"] / (1 + cost / 100) if r["time_s"] and cost else r["time_s"]

    past = [r for r in rows if r["date"] < today]
    upcoming = [r for r in rows if r["date"] >= today]

    # PR flags: best all-out result per distance, and per course + distance.
    def best_by(key):
        best = {}
        for r in past:
            if r["effort"] != "all_out" or not r["time_s"]:
                continue
            k = key(r)
            if k not in best or r["time_s"] < best[k]["time_s"]:
                best[k] = r
        return best

    dist_key = lambda r: round(r["distance_m"])  # noqa: E731
    course_key = lambda r: ((r["course"] or "").strip().lower(), round(r["distance_m"]))  # noqa: E731
    pr, course_pr = best_by(dist_key), best_by(course_key)
    for r in past:
        r["is_pr"] = pr.get(dist_key(r)) is r
        r["is_course_pr"] = bool(r["course"]) and course_pr.get(course_key(r)) is r

    for r in upcoming:
        r["days_to_go"] = (date.fromisoformat(r["date"]) - date.fromisoformat(today)).days
        same = [p for p in past if r["course"] and course_key(p) == course_key(r)]
        last = same[-1] if same else None
        r["course_history"] = (
            {"date": last["date"], "name": last["name"], "time_s": last["time_s"], "time_source": last["time_source"],
             "watch_distance_factor": last["watch_distance_factor"]}
            if last else None
        )
        r["course_pr_s"] = course_pr.get(course_key(r), {}).get("time_s") if r["course"] else None
    return {"past": list(reversed(past)), "upcoming": upcoming, "unlogged": unlogged_garmin_races(conn)}


def unlogged_garmin_races(conn: sqlite3.Connection) -> list[dict]:
    """Activities Garmin has marked as races that aren't in the log yet."""
    logged = {r[0] for r in conn.execute("SELECT activity_id FROM races WHERE activity_id IS NOT NULL")}
    out = []
    for r in conn.execute(
        "SELECT id, name, start_local, distance_m, duration_s FROM activities WHERE is_run = 1 AND event_type = 'race'"
    ):
        if r["id"] in logged:
            continue
        out.append({"activity_id": r["id"], "name": r["name"], "date": r["start_local"][:10],
                    "distance_m": r["distance_m"], "duration_s": r["duration_s"]})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def match_activity(conn: sqlite3.Connection, day: str, distance_m: float) -> int | None:
    """The run on that day closest to the race distance."""
    row = conn.execute(
        "SELECT id FROM activities WHERE is_run = 1 AND substr(start_local, 1, 10) = ? ORDER BY ABS(distance_m - ?) LIMIT 1",
        (day, distance_m),
    ).fetchone()
    return row["id"] if row else None


def save(conn: sqlite3.Connection, race: dict, race_id: int | None = None) -> int:
    fields = ("date", "name", "distance_m", "course", "official_time_s", "target_time_s", "effort", "activity_id", "notes")
    race = dict(race)
    if not race.get("activity_id"):
        race["activity_id"] = match_activity(conn, race["date"], race["distance_m"])
    values = [race.get(f) for f in fields]
    if race_id is None:
        cur = conn.execute(f"INSERT INTO races ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", values)
        race_id = cur.lastrowid
    else:
        conn.execute(f"UPDATE races SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?", values + [race_id])
    conn.commit()
    return race_id


def delete(conn: sqlite3.Connection, race_id: int) -> None:
    conn.execute("DELETE FROM races WHERE id = ?", (race_id,))
    conn.commit()


def all_out_results(conn: sqlite3.Connection, today: date, days: int = 365, heat_adjusted: bool = True) -> list[dict]:
    """Past all-out races in the window, for predictions: [{label, distance_m, time_s, date}].

    By default times are heat-adjusted (cool-weather equivalents), so a humid summer race
    isn't mistaken for lower fitness.
    """
    since = (today - timedelta(days=days)).isoformat()
    out = []
    for r in all_races(conn, today)["past"]:
        if r["effort"] == "all_out" and r["time_s"] and r["date"] >= since:
            t = r["adjusted_time_s"] if heat_adjusted else r["time_s"]
            adjusted = heat_adjusted and r["adjusted_time_s"] and r["adjusted_time_s"] < r["time_s"] - 0.5
            label = f"{r['name']} ({date.fromisoformat(r['date']).strftime('%b %Y')}{', heat-adjusted' if adjusted else ''})"
            out.append({"label": label, "distance_m": r["distance_m"], "time_s": t, "date": r["date"]})
    return out


def prompt_summary(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Compact race history for the coach and status card."""
    data = all_races(conn, today)
    keep = ("date", "name", "distance_m", "course", "time_s", "time_source", "adjusted_time_s", "weather", "effort", "is_pr",
            "is_course_pr", "watch_distance_factor", "notes")
    return {
        "past": [{k: r.get(k) for k in keep} for r in data["past"]],
        "upcoming": [{k: r.get(k) for k in ("date", "name", "distance_m", "course", "target_time_s", "days_to_go",
                                              "course_history", "course_pr_s", "notes")} for r in data["upcoming"]],
    }
