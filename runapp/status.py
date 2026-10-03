"""AI status card: how your body is doing, what you did in the last 12 hours, and your next workout.

Unlike the chat coach, the status is generated from a snapshot we assemble here
and pass in the prompt, so Claude needs no tools and answers in one turn. The
answer is constrained to a JSON schema so the page can lay it out.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta

from . import analysis, coach, form, races, weather, wellness, workouts, zones

_lock = threading.Lock()

REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["body", "last_12_hours", "next_workout"],
    "properties": {
        "body": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "headline", "summary"],
            "properties": {
                "level": {"type": "string", "enum": ["ready", "moderate", "low"]},
                "headline": {"type": "string"},
                "summary": {"type": "string"},
            },
        },
        "last_12_hours": {
            "type": "object",
            "additionalProperties": False,
            "required": ["headline", "summary"],
            "properties": {"headline": {"type": "string"}, "summary": {"type": "string"}},
        },
        "next_workout": {
            "type": "object",
            "additionalProperties": False,
            "required": ["when", "kind", "title", "details", "adjustment"],
            "properties": {
                "when": {"type": "string"},
                "kind": {"type": "string", "enum": ["easy", "long", "threshold", "intervals", "race", "rest", "cross"]},
                "title": {"type": "string"},
                "details": {"type": "string"},
                "adjustment": {"type": "string"},
            },
        },
    },
}

STATUS_PROMPT = """You write a short status card for a runner's training dashboard. It is now {now}. \
Use {units} for distances. ALWAYS give every pace in min:sec per mile (e.g. 11:30/mi), never per km \
(1 mile = 1.609344 km).

{zones}

Write three parts:
1. body: their physical state right now. Weigh Garmin training readiness, sleep, HRV, resting HR, \
Body Battery, Garmin's training status, recent training load and anything in their notes (illness, \
lifestyle; heat_cost_pct on a run is how much the temperature + dew point slowed it). level is "ready" (good to train hard), "moderate" (train, but keep it controlled) or "low" \
(rest or very easy only). If last night has no sleep/HRV data, say the watch didn't record it rather than guessing.
2. last_12_hours: what they did in the last 12 hours (activities, steps, stress if notable). If they \
did nothing notable, say so briefly and mention their most recent activity and how long ago it was.
3. next_workout: the next session from their training plan that they haven't done yet (match plan \
entries to completed activities by date and type). If today's session is still to do and there's time \
left today, it's today's; otherwise the next one. If their body status calls for it, adjust the session \
and explain why in adjustment (otherwise adjustment is ""). With no plan, recommend one based on their \
data. kind classifies the session (easy, long, threshold, intervals for anything faster than threshold, \
race, rest, cross for other sports). details gives the concrete structure: warm-up, main set with paces \
and HR caps, cool-down.

Style: headline at most 8 words. summary at most 2 short sentences, quoting real numbers. details at \
most 3 short lines separated by newlines. Plain text, no Markdown. Speak to them directly ("you"). \
Never diagnose illness; for pain or chest symptoms, tell them to rest and see a professional.
{context}

Data snapshot (distances in meters, durations in seconds, times are local):
{data}"""


class StatusError(Exception):
    pass


def _activity_brief(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    start = datetime.fromisoformat(row["start_local"][:19])
    duration = row["duration_s"] or 0
    return {
        "start": row["start_local"][:16],
        "end": (start + timedelta(seconds=duration)).isoformat(" ", "minutes"),
        "name": row["name"],
        "type": row["type"],
        "distance_m": round(row["distance_m"] or 0),
        "duration_s": round(duration),
        "avg_hr": row["avg_hr"],
        "max_hr": row["max_hr"],
        "hr_zone_minutes": [round((row[f"z{z}_s"] or 0) / 60) for z in range(1, 6)],  # Norwegian zones
        "aerobic_te": row["aerobic_te"],
        "anaerobic_te": row["anaerobic_te"],
        "training_load": row["training_load"],
        "kind": analysis.workout_kind(row),
        "weather": weather.for_activity(conn, row["id"]),
    }


def build_snapshot(conn: sqlite3.Connection, now: datetime) -> dict:
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM activities WHERE start_local >= ? ORDER BY start_local", (since,)
    ).fetchall()
    cutoff_12h = now - timedelta(hours=12)
    recent = []
    for r in rows:
        brief = _activity_brief(conn, r)
        brief["in_last_12_hours"] = datetime.fromisoformat(brief["end"]) >= cutoff_12h
        recent.append(brief)
    last = conn.execute("SELECT start_local, name FROM activities ORDER BY start_local DESC LIMIT 1").fetchone()
    return {
        "body": wellness.summarize(conn, now.date(), days=3),
        "activities_last_7_days": recent,
        "most_recent_activity": dict(last) if last else None,
        "load": analysis.load_ratio(conn, now.date()),
        "vo2max": analysis.overview(conn, now.date())["vo2max"],
        "goals": analysis.goal_progress(conn, now.date()),
        "races": races.prompt_summary(conn, now.date()),
        "recent_quality_sessions": workouts.brief(conn, 3, now.date()),
        "form": {k: v for k, v in form.brief(conn, now.date()).items() if k in ("recent_fatigue", "balance_alerts")},
    }


def generate(conn: sqlite3.Connection, units: str = "km", now: datetime | None = None, runner=coach.run_claude) -> dict:
    now = now or datetime.now()
    if not _lock.acquire(blocking=False):
        raise StatusError("The status is already being updated.")
    try:
        prompt = STATUS_PROMPT.format(
            now=now.strftime("%A %Y-%m-%d %H:%M"),
            units="kilometers" if units == "km" else "miles",
            context=coach.context_sections(coach.get_notes(conn), coach.get_plan(conn)),
            zones=zones.prompt_text(conn),
            data=json.dumps(build_snapshot(conn, now), default=str),
        )
        try:
            # One-off: no need to keep a transcript of it among your Claude Code sessions.
            args = ["--json-schema", json.dumps(REPORT_SCHEMA), "--no-session-persistence"]
            result = runner("Write the status card.", prompt, args, 180)
        except coach.CoachError as e:
            raise StatusError(str(e)) from e
        report = result.get("structured_output")
        if not isinstance(report, dict):
            raise StatusError("Claude didn't return a status in the expected format. Try again.")
        conn.execute(
            "INSERT INTO status_reports (generated_at, report_json) VALUES (?, ?)",
            (now.isoformat(" ", "seconds"), json.dumps(report)),
        )
        conn.commit()
        return report
    finally:
        _lock.release()


def latest(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT generated_at, report_json FROM status_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "report": json.loads(row["report_json"]) if row else None,
        "generated_at": row["generated_at"] if row else None,
        "metrics": wellness.headline_metrics(wellness.summarize(conn)),
    }
