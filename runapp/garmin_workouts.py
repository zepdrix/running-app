"""Send planned runs to Garmin as structured workouts the watch can follow.

A planned session is free text ("5x6 min sub-threshold (HR <=160), 1 min jog"),
and the watch needs steps. Claude turns the text into steps (build); you see
them before anything is sent; send uploads the workout to Garmin Connect and
puts it on your Garmin calendar for that day, so the watch picks it up on its
next sync.

Two rules are enforced here, whatever the conversion says:
- Warm-ups and cool-downs never run on a timer. They end when you press Lap, so
  you decide how long they are; the planned duration is kept as the step's note.
- Every pace is per mile.

The Garmin step format was read off workouts made in Garmin Connect itself
(lap.button = 1, time = 2, distance = 3, iterations = 7; heart-rate targets as a
custom bpm range; pace targets in m/s, fast end first).
"""

import json
import re
import sqlite3
import threading
from datetime import date

from . import coach, zones

MILE = 1609.344
SENDABLE = ("easy", "long", "threshold", "intervals")  # kinds of session that make sense on the watch
STEP_TYPES = ("warmup", "run", "recover", "cooldown")
MAX_NOTE = 200
_lock = threading.Lock()

_STEP = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "duration_s", "distance_m", "hr_low", "hr_high", "pace_fast", "pace_slow", "note"],
    "properties": {
        "type": {"type": "string", "enum": list(STEP_TYPES)},
        "duration_s": {"type": "integer"},
        "distance_m": {"type": "integer"},
        "hr_low": {"type": "integer"},
        "hr_high": {"type": "integer"},
        "pace_fast": {"type": "string"},
        "pace_slow": {"type": "string"},
        "note": {"type": "string"},
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["steps"],
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [*_STEP["required"], "times", "steps"],
                "properties": {
                    **_STEP["properties"],
                    "type": {"type": "string", "enum": [*STEP_TYPES, "repeat"]},
                    "times": {"type": "integer"},
                    "steps": {"type": "array", "items": _STEP},
                },
            },
        },
    },
}

PROMPT = """You turn one planned running session into the steps of a structured workout for a Garmin watch.

{zones}

Step types: warmup, run (the work: a rep, or the whole run on an easy day), recover (the jog or rest between \
reps), cooldown, and repeat (a group of steps done `times` times, listed in its `steps`; never put a repeat \
inside a repeat).

For every step fill in every field, using 0 or "" for "none":
- duration_s or distance_m: how the step ends (seconds, or meters). Use one of them, never both. Both 0 means \
"until I press Lap".
- hr_low and hr_high: a heart-rate target in bpm, when the session is run by heart rate. A cap like "HR <= 160" \
becomes hr_high 160 with hr_low about 10 bpm lower. For an easy run use hr_low 100 and hr_high at the top of what \
the plan allows for easy running.
- pace_fast and pace_slow: a pace target as m:ss per mile (e.g. "7:06" and "7:15"), when the session is run by \
pace. Convert any per-km pace to per mile (1 mile = 1.609344 km). Give a heart-rate target or a pace target, \
not both; when the plan gives both, heart rate wins and the pace goes in the note.
- note: a few words the watch shows on that step (at most 100 characters), or "".
- times and steps: only for a repeat; otherwise 0 and [].

Rules:
- A session with reps starts with one warmup step and ends with one cooldown step. Put the planned warm-up or \
cool-down ("15 min easy before and after") in that step's note; leave its duration 0, because the athlete ends \
warm-ups and cool-downs with the Lap button.
- A plain easy or long run is a single run step with its duration or distance, no warmup or cooldown.
- Strides after an easy run are a repeat of a short run step with no target and a recover step.
- Take numbers from the session text; where it gives a range for a duration ("25-30 min") use the top of it. \
Use the plan notes below for heart-rate caps and paces the session doesn't spell out. Don't invent reps.

Plan notes:
{notes}

The session, planned for {day}:
kind: {kind}
title: {title}
details: {details}"""


class GarminWorkoutError(Exception):
    pass


# ---------- steps: cleaning what the conversion returns ----------
def parse_pace(text) -> float | None:
    """'7:06' (per mile) -> seconds per mile."""
    m = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)(?:\s*/\s*mi)?\s*", str(text or ""))
    if not m:
        return None
    s = int(m.group(1)) * 60 + int(m.group(2))
    return float(s) if 180 <= s <= 1500 else None


def _step(raw: dict) -> dict | None:
    kind = raw.get("type")
    if kind not in STEP_TYPES:
        return None
    step = {"type": kind}
    note = str(raw.get("note") or "").strip()
    duration, distance = int(raw.get("duration_s") or 0), int(raw.get("distance_m") or 0)
    if kind in ("warmup", "cooldown"):
        # Never timed: you end these with the Lap button. Keep what was planned as the note.
        if not note and duration > 0:
            note = f"Plan: about {round(duration / 60)} min easy"
        elif not note and distance > 0:
            note = f"Plan: about {distance / MILE:.1f} mi easy"
    elif 0 < duration <= 6 * 3600:
        step["duration_s"] = duration
    elif 0 < distance <= 100_000:
        step["distance_m"] = distance
    low, high = int(raw.get("hr_low") or 0), int(raw.get("hr_high") or 0)
    fast, slow = parse_pace(raw.get("pace_fast")), parse_pace(raw.get("pace_slow"))
    if low or high:
        low, high = (low or high - 10), (high or low + 10)
        low, high = sorted((low, high))
        if 60 <= low < high <= 230:
            step.update(hr_low=low, hr_high=high)
    elif fast or slow:
        fast, slow = (fast or slow - 10), (slow or fast + 10)
        step.update(zip(("pace_fast", "pace_slow"), sorted((fast, slow))))
    if note:
        step["note"] = note[:MAX_NOTE]
    return step


def normalize(raw_steps: list) -> list[dict]:
    """Clean up converted steps: known types only, sane numbers, and Lap-button warm-ups and cool-downs."""
    out = []
    for raw in raw_steps or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "repeat":
            inner = [s for s in (_step(x) for x in raw.get("steps") or [] if isinstance(x, dict)) if s]
            times = int(raw.get("times") or 0)
            if inner and 2 <= times <= 99:
                out.append({"type": "repeat", "times": times, "steps": inner})
            elif inner:  # "1 x": just the steps
                out.extend(inner)
        else:
            step = _step(raw)
            if step:
                out.append(step)
    if not any(s["type"] in ("run", "repeat") for s in out):
        raise GarminWorkoutError("Couldn't find any running in that session to send to the watch.")
    return out


# ---------- steps -> Garmin's workout format ----------
_STEP_TYPE = {"warmup": (1, 1), "cooldown": (2, 2), "run": (3, 3), "recover": (4, 4)}  # id, display order
_STEP_KEY = {"warmup": "warmup", "cooldown": "cooldown", "run": "interval", "recover": "recovery"}
_RUNNING = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}
_NONE = {"strokeType": {"strokeTypeId": 0, "displayOrder": 0}, "equipmentType": {"equipmentTypeId": 0, "displayOrder": 0}}


def _garmin_step(step: dict, order: int, child: int | None) -> dict:
    type_id, display = _STEP_TYPE[step["type"]]
    g = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": _STEP_KEY[step["type"]], "displayOrder": display},
        "childStepId": child,
        "description": step.get("note"),
        **_NONE,
    }
    if "duration_s" in step:
        g.update(endCondition={"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True},
                 endConditionValue=float(step["duration_s"]))
    elif "distance_m" in step:
        g.update(endCondition={"conditionTypeId": 3, "conditionTypeKey": "distance", "displayOrder": 3, "displayable": True},
                 endConditionValue=float(step["distance_m"]),
                 preferredEndConditionUnit={"unitId": 1, "unitKey": "meter", "factor": 100.0})
    else:
        g.update(endCondition={"conditionTypeId": 1, "conditionTypeKey": "lap.button", "displayOrder": 1, "displayable": True})
    if "hr_low" in step:
        g.update(targetType={"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4},
                 targetValueOne=float(step["hr_low"]), targetValueTwo=float(step["hr_high"]))
    elif "pace_fast" in step:  # m/s, fast end first
        g.update(targetType={"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6},
                 targetValueOne=MILE / step["pace_fast"], targetValueTwo=MILE / step["pace_slow"])
    else:
        g.update(targetType={"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1})
    return g


def to_garmin(name: str, description: str, steps: list[dict]) -> dict:
    """The workout as Garmin Connect's workout service expects it."""
    out, order, groups = [], 0, 0
    for step in steps:
        order += 1
        if step["type"] == "repeat":
            groups += 1
            group = {
                "type": "RepeatGroupDTO", "stepOrder": order, "childStepId": groups,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": step["times"], "endConditionValue": float(step["times"]),
                "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7, "displayable": False},
                "skipLastRestStep": True, "smartRepeat": False, "workoutSteps": [],  # no jog after the last rep: straight to the cool-down
            }
            for inner in step["steps"]:
                order += 1
                group["workoutSteps"].append(_garmin_step(inner, order, groups))
            out.append(group)
        else:
            out.append(_garmin_step(step, order, None))
    return {
        "workoutName": name[:80],
        "description": (description or "")[:1000] or None,
        "sportType": _RUNNING,
        "workoutSegments": [{"segmentOrder": 1, "sportType": _RUNNING, "workoutSteps": out}],
    }


# ---------- the plan session a workout is for ----------
def _session(conn: sqlite3.Connection, day: str) -> sqlite3.Row:
    marks = ", ".join("?" * len(SENDABLE))
    row = conn.execute(f"SELECT date, kind, title, details FROM plan_sessions WHERE date = ? AND kind IN ({marks}) ORDER BY id",
                       (day, *SENDABLE)).fetchone()
    if not row:
        raise GarminWorkoutError("There's no run planned that day to send.")
    return row


def _source(session) -> str:
    """What the steps were built from, to notice when the plan has changed since."""
    return f"{session['title']}\n{session['details'] or ''}".strip()


def _saved(conn: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM garmin_workouts WHERE date = ?", (day,)).fetchone()


def build(conn: sqlite3.Connection, day: str, runner=None) -> list[dict]:
    """Convert that day's planned run into steps and keep them as a draft. Nothing is sent."""
    runner = runner or coach.run_claude  # looked up here: coach imports plan, which imports this module
    session = _session(conn, day)
    saved = _saved(conn, day)
    if saved and saved["garmin_workout_id"]:
        raise GarminWorkoutError("That workout is on Garmin already. Remove it from Garmin first to rebuild it.")
    if not _lock.acquire(blocking=False):
        raise GarminWorkoutError("Another workout is being built. Try again in a moment.")
    try:
        notes = conn.execute("SELECT value FROM coach_state WHERE key = 'plan_notes'").fetchone()
        prompt = PROMPT.format(
            zones=zones.prompt_text(conn), notes=(notes["value"] if notes and notes["value"] else "(none)"),
            day=date.fromisoformat(day).strftime("%A %Y-%m-%d"), kind=session["kind"],
            title=session["title"], details=session["details"] or "(none)",
        )
        try:
            result = runner("Write the workout steps.", prompt, ["--json-schema", json.dumps(SCHEMA), "--no-session-persistence"], 180)
        except coach.CoachError as e:
            raise GarminWorkoutError(str(e)) from e
        raw = result.get("structured_output")
        if not isinstance(raw, dict):
            raise GarminWorkoutError("Claude didn't return workout steps in the expected format. Try again.")
        steps = normalize(raw.get("steps"))
        conn.execute("INSERT OR REPLACE INTO garmin_workouts (date, source, steps_json) VALUES (?, ?, ?)",
                     (day, _source(session), json.dumps(steps)))
        conn.commit()
        return steps
    finally:
        _lock.release()


def send(conn: sqlite3.Connection, client, day: str) -> None:
    """Upload the draft to Garmin Connect and put it on the Garmin calendar for its day."""
    saved = _saved(conn, day)
    if not saved:
        raise GarminWorkoutError("Build the workout steps first.")
    if saved["garmin_workout_id"]:
        raise GarminWorkoutError("That workout is on Garmin already.")
    session = _session(conn, day)
    payload = to_garmin(session["title"], session["details"], json.loads(saved["steps_json"]))
    try:
        workout_id = client.upload_workout(payload)["workoutId"]
    except Exception as e:
        raise GarminWorkoutError(f"Garmin didn't accept the workout: {e}") from e
    # Saved straight away: if scheduling fails, the workout is still known here and can be removed.
    conn.execute("UPDATE garmin_workouts SET garmin_workout_id = ?, sent_at = datetime('now') WHERE date = ?", (workout_id, day))
    conn.commit()
    try:
        scheduled = client.schedule_workout(workout_id, day)
    except Exception as e:
        raise GarminWorkoutError(
            f"The workout is in your Garmin library, but putting it on the calendar for {day} failed: {e}") from e
    schedule_id = scheduled.get("workoutScheduleId") if isinstance(scheduled, dict) else None
    conn.execute("UPDATE garmin_workouts SET garmin_schedule_id = ? WHERE date = ?", (schedule_id, day))
    conn.commit()


def remove(conn: sqlite3.Connection, client, day: str) -> None:
    """Take the workout off Garmin (library and calendar) and forget the draft."""
    saved = _saved(conn, day)
    if not saved:
        return
    if saved["garmin_workout_id"]:
        try:
            client.delete_workout(saved["garmin_workout_id"])  # also clears it from the calendar
        except Exception as e:
            if "404" not in str(e):  # already deleted in Garmin Connect: nothing left to remove
                raise GarminWorkoutError(f"Garmin didn't remove the workout: {e}") from e
    conn.execute("DELETE FROM garmin_workouts WHERE date = ?", (day,))
    conn.commit()


def discard(conn: sqlite3.Connection, day: str) -> None:
    """Drop a draft that was never sent."""
    conn.execute("DELETE FROM garmin_workouts WHERE date = ? AND garmin_workout_id IS NULL", (day,))
    conn.commit()


def for_plan(conn: sqlite3.Connection, sessions: list[dict]) -> None:
    """Attach to each run in the plan what's been built or sent for it."""
    saved = {r["date"]: r for r in conn.execute("SELECT * FROM garmin_workouts")}
    seen = set()
    for s in sessions:
        if s["kind"] not in SENDABLE or s["date"] in seen:
            continue
        seen.add(s["date"])  # one workout per day: the day's first run
        row = saved.get(s["date"])
        s["garmin"] = {
            "state": "none" if not row else "sent" if row["garmin_workout_id"] else "draft",
            "steps": json.loads(row["steps_json"]) if row else None,
            "sent_at": row["sent_at"] if row else None,
            "scheduled": bool(row and row["garmin_schedule_id"]),
            "changed": bool(row and row["source"] != _source(s)),  # the plan text was edited after the steps were built
        }
