"""Strength training alongside the running plan.

Strength sessions are ordinary plan sessions of kind "strength", so the coach,
the status card and the plan editor all see them. This module writes them:
two alternating routines that progress over the weeks, placed around the runs.

Placement rules (RULES below is the wording shown to you and given to the coach):
after hard runs or on easy days, never the day before a hard or long run, never
on rest days, eased off before races. `refit` re-places the upcoming sessions
whenever the running plan has moved; past sessions are never touched.
"""

import itertools
import json
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

from . import analysis

KIND = "strength"
ACTIVITY_TYPES = {"strength_training"}  # Garmin activities that tick a strength session off by themselves
EQUIPMENT = {"bodyweight": "Bodyweight and a step", "dumbbells": "Dumbbells or a kettlebell", "gym": "Gym (barbell and machines)"}
DEFAULTS = {"per_week": 2, "equipment": "bodyweight"}
MAX_PER_WEEK = 3
MIN_GAP_DAYS = 2          # never on back-to-back days
HORIZON_DAYS = 27         # plan at least this far ahead, and to the end of the running plan
HARD = ("threshold", "intervals")
NO_STRENGTH_BEFORE = {"all_out": 3, "controlled": 2, "easy": 2}   # days before a race with no strength at all
NO_STRENGTH_AFTER = {"all_out": 2, "controlled": 1, "easy": 1}    # and days after it
LIGHT_BEFORE_ALL_OUT = 7  # within this many days of an all-out race, sessions are light

RULES = [
    "Hard days stay hard: strength goes after a threshold or interval run, or on an easy day.",
    "Never the day before a threshold, interval or long run, so your legs are fresh for it.",
    "Never on a rest day, and never two days in a row.",
    "All-out race: light sessions only in the week before, nothing in the last 3 days or the 2 days after.",
    "Any other race: nothing in the 2 days before or the day after.",
]

# Each exercise: one variant per equipment level, as (name, how it's counted, cue).
_CALF_CUE = "Straight knee, full range: 2 s up, 3 s down"
ROUTINES = {
    "A": {
        "name": "legs & calves",
        "exercises": [
            {"bodyweight": ("Bodyweight squat", "reps", "Sit back and down, knees over toes, 3 s on the way down"),
             "dumbbells": ("Goblet squat", "reps", "Weight at your chest, sit between your heels, 3 s on the way down"),
             "gym": ("Back squat", "main", "Thighs to parallel, drive up through the whole foot")},
            {"bodyweight": ("Reverse lunge", "side", "Step back, front shin near vertical, push through the front foot"),
             "dumbbells": ("Reverse lunge with dumbbells", "side", "Step back, front shin near vertical, push through the front foot"),
             "gym": ("Bulgarian split squat", "side", "Back foot on a bench, drop straight down, torso tall")},
            {"bodyweight": ("Single-leg calf raise off a step", "calf_side", _CALF_CUE),
             "dumbbells": ("Single-leg calf raise off a step, holding a dumbbell", "calf_side", _CALF_CUE),
             "gym": ("Single-leg calf raise off a step, holding a dumbbell", "calf_side", _CALF_CUE)},
            {"bodyweight": ("Bent-knee calf raise", "calf", "Knees bent about 30°: this trains the soleus, which takes the most load when you run"),
             "dumbbells": ("Bent-knee calf raise holding dumbbells", "calf", "Knees bent about 30°: this trains the soleus, which takes the most load when you run"),
             "gym": ("Seated calf raise", "calf", "Slow and full range: this trains the soleus, which takes the most load when you run")},
            {"all": ("Front plank", "hold", "Ribs down, glutes tight; stop when your hips sag")},
        ],
    },
    "B": {
        "name": "hips & hamstrings",
        "exercises": [
            {"bodyweight": ("Single-leg glute bridge", "side", "Drive through the heel, keep your hips level"),
             "dumbbells": ("Romanian deadlift with dumbbells", "reps", "Push your hips back with a flat back until the hamstrings load"),
             "gym": ("Romanian deadlift", "main", "Barbell; push your hips back with a flat back until the hamstrings load")},
            {"bodyweight": ("Step-up", "side", "Knee-high step; lower yourself for 3 s, don't push off the back foot"),
             "dumbbells": ("Step-up holding dumbbells", "side", "Knee-high step; lower yourself for 3 s, don't push off the back foot"),
             "gym": ("Step-up holding dumbbells", "side", "Knee-high step; lower yourself for 3 s, don't push off the back foot")},
            {"bodyweight": ("Hamstring walkout", "reps", "From a bridge, walk your heels out and back with your hips up"),
             "dumbbells": ("Single-leg Romanian deadlift", "side", "Dumbbell in the opposite hand, hips square to the floor"),
             "gym": ("Hamstring curl", "reps", "Machine; 3 s on the way down")},
            {"all": ("Side plank", "hold_side", "Straight line from ear to ankle; raise the top leg when it gets easy")},
            {"all": ("Pogo hops", "hops", "Small quick hops on the balls of your feet, stiff ankles, quiet landings")},
        ],
    },
}

# Progression. weeks=None: the phase you stay in.
PHASES = [
    {"key": "intro", "label": "Intro", "weeks": 3, "sets": 2, "reps": 8, "calf": 10, "hold": 20, "hops": 10, "minutes": 20,
     "note": "Learn the movements. Stop every set with 3 or more reps left in you: you shouldn't be sore tomorrow."},
    {"key": "build", "label": "Build", "weeks": 4, "sets": 3, "reps": 10, "calf": 12, "hold": 30, "hops": 15, "minutes": 30,
     "note": "Finish each set with about 2 reps left. When the last rep looks like the first, make it harder: more load, or a slower lowering."},
    {"key": "strong", "label": "Strong", "weeks": None, "sets": 3, "reps": 6, "calf": 10, "hold": 45, "hops": 20, "minutes": 35,
     "note": "Heavy for you: a load where the last rep is hard but clean. Rest 2 minutes between sets."},
]
# Without weights, the last phase gets harder through more reps and slower lowering instead of load.
STRONG_BODYWEIGHT = {"reps": 12, "calf": 15,
                     "note": "Lower for 4 s on every rep, and move to the single-leg version of an exercise once 12 reps are easy."}
LIGHT_NOTE = "Race week: a reminder for the muscles, not training. Same load as last time or lighter, and nowhere near tired."
LIGHT_MINUTES = 12


# ---------- settings and the done log ----------
def settings(conn: sqlite3.Connection) -> dict | None:
    """{per_week, equipment, start} once a strength plan has been set up, else None."""
    row = conn.execute("SELECT value FROM coach_state WHERE key = 'strength'").fetchone()
    return json.loads(row["value"]) if row and row["value"] else None


def save_settings(conn: sqlite3.Connection, per_week: int, equipment: str, start: str) -> None:
    if equipment not in EQUIPMENT:
        raise ValueError(f"equipment must be one of {', '.join(EQUIPMENT)}")
    if not 1 <= per_week <= MAX_PER_WEEK:
        raise ValueError(f"per_week must be 1 to {MAX_PER_WEEK}")
    date.fromisoformat(start)
    conn.execute("INSERT OR REPLACE INTO coach_state VALUES ('strength', ?)",
                 (json.dumps({"per_week": per_week, "equipment": equipment, "start": start}),))
    conn.commit()


def log(conn: sqlite3.Connection, day: str, done: bool) -> None:
    """Tick a strength session off by hand (most people don't record them on the watch)."""
    if done:
        conn.execute("INSERT OR IGNORE INTO strength_log (date) VALUES (?)", (day,))
    else:
        conn.execute("DELETE FROM strength_log WHERE date = ?", (day,))
    conn.commit()


def logged_dates(conn: sqlite3.Connection) -> set[str]:
    return {r["date"] for r in conn.execute("SELECT date FROM strength_log")}


def done_dates(conn: sqlite3.Connection) -> set[str]:
    """Days with strength work: ticked off by hand, or recorded on the watch."""
    marks = ", ".join("?" * len(ACTIVITY_TYPES))
    recorded = conn.execute(f"SELECT DISTINCT substr(start_local, 1, 10) AS d FROM activities WHERE type IN ({marks})", tuple(ACTIVITY_TYPES))
    return logged_dates(conn) | {r["d"] for r in recorded}


# ---------- what a session contains ----------
def phase_on(start: date, day: date) -> dict:
    """The phase for a session on `day`, with week = which week of the programme it is (1-based)."""
    week = max(0, (day - start).days) // 7 + 1
    first = 1
    for p in PHASES:
        if p["weeks"] is None or week < first + p["weeks"]:
            break
        first += p["weeks"]
    return {**p, "week": week, "phase_week": week - first + 1}


def _dose(how: str, p: dict) -> str:
    sets = p["sets"]
    return {
        "reps": f"{sets} × {p['reps']}",
        "main": f"{p['main_sets']} × {p['main_reps']}",
        "side": f"{sets} × {p['reps']} each side",
        "calf": f"{sets} × {p['calf']}",
        "calf_side": f"{sets} × {p['calf']} each side",
        "hold": f"{sets} × {p['hold']} s",
        "hold_side": f"{sets} × {p['hold']} s each side",
        "hops": f"{sets} × {p['hops']}",
    }[how]


def session(letter: str, phase: dict, equipment: str, light: bool = False, timing: str = "") -> dict:
    """One strength session as a plan row: a title, and details with one `exercise | dose | cue` line each."""
    p = {**phase, "main_sets": phase["sets"], "main_reps": phase["reps"]}
    if p["key"] == "strong":
        if equipment == "bodyweight":
            p.update(STRONG_BODYWEIGHT)
        elif equipment == "gym":
            p.update(main_sets=4, main_reps=5)  # the big barbell lifts go heaviest
    if light:
        p.update(sets=max(1, p["sets"] - 1), reps=min(p["reps"], 8), calf=min(p["calf"], 10), hold=20,
                 minutes=LIGHT_MINUTES, note=LIGHT_NOTE)
        p.update(main_sets=p["sets"], main_reps=min(p["main_reps"], 8))
    routine = ROUTINES[letter]
    lines = []
    for ex in routine["exercises"]:
        name, how, cue = ex.get(equipment) or ex["all"]
        if light and how == "hops":
            continue  # no jumping in race week
        lines.append(f"{name} | {_dose(how, p)} | {cue}")
    stage = "Race week" if light else f"{p['label']}, week {p['week']}"
    head = " · ".join(x for x in (stage, f"about {p['minutes']} min", timing) if x)
    return {
        "kind": KIND,
        "title": f"{'Light strength' if light else 'Strength'} {letter}: {routine['name']}",
        "details": "\n".join([head, p["note"], *lines]),
    }


def parse_details(details: str) -> dict:
    """Split a strength session's details into notes and `exercise | dose | cue` rows."""
    notes, exercises = [], []
    for line in (details or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            exercises.append({"name": parts[0], "dose": parts[1], "cue": " ".join(parts[2:])})
        elif line.strip():
            notes.append(line.strip())
    return {"notes": notes, "exercises": exercises}


def _letter(title: str) -> str | None:
    m = re.search(r"\bstrength ([AB])\b", title or "", re.I)
    return m.group(1).upper() if m else None


# ---------- where sessions go ----------
def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


class _Calendar:
    """The running side of the plan, as the placement rules see it."""

    def __init__(self, conn: sqlite3.Connection, today: date):
        intensity = {"rest": 0, "cross": 1, "easy": 1, "long": 2, "threshold": 3, "intervals": 4, "race": 5}
        self.runs: dict[date, str] = {}    # day -> the hardest thing planned that day
        self.races: dict[date, tuple[str, str]] = {}  # race day -> (effort, name)
        for r in conn.execute("SELECT date, kind, title FROM plan_sessions WHERE kind != ? ORDER BY date, id", (KIND,)):
            d = date.fromisoformat(r["date"])
            if d not in self.runs or intensity.get(r["kind"], 1) > intensity.get(self.runs[d], 1):
                self.runs[d] = r["kind"]
            if r["kind"] == "race":
                self.races[d] = ("all_out", r["title"])
        for r in conn.execute("SELECT date, effort, name FROM races"):
            self.races[date.fromisoformat(r["date"])] = (r["effort"], r["name"])
        self.planned_weeks = {_monday(d) for d in self.runs}
        self.usual_hard = self._usual_hard_weekdays(conn, today)

    @staticmethod
    def _usual_hard_weekdays(conn: sqlite3.Connection, today: date) -> set[int]:
        """The weekdays your hard runs usually fall on, for weeks the running plan doesn't cover yet."""
        since = (today - timedelta(days=42)).isoformat()
        counts: Counter[int] = Counter()
        marks = ", ".join("?" * len(HARD))
        for r in conn.execute(f"SELECT date FROM plan_sessions WHERE kind IN ({marks}) AND date >= ?", (*HARD, since)):
            counts[date.fromisoformat(r["date"]).weekday()] += 1
        for r in conn.execute("SELECT * FROM activities WHERE is_run = 1 AND start_local >= ?", (since,)):
            if analysis.workout_kind(r) in HARD:
                counts[date.fromisoformat(r["start_local"][:10]).weekday()] += 1
        days = [wd for wd, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:2]]
        return set(days) or {1, 4}  # nothing to go on: Tuesday and Friday

    def kind(self, d: date) -> str | None:
        """What's planned that day; in a week with no running plan yet, your usual hard days count as hard."""
        if d in self.runs:
            return self.runs[d]
        if _monday(d) not in self.planned_weeks and d.weekday() in self.usual_hard and d not in self.races:
            return "usual_hard"
        return None

    def blocked(self, d: date) -> str | None:
        """Why a strength session shouldn't be on this day, or None when it's fine."""
        if d in self.races:
            return "it's race day"
        for n in range(1, 4):
            days = f"{n} day{'s' if n > 1 else ''}"
            race = self.races.get(d + timedelta(days=n))
            if race and n <= NO_STRENGTH_BEFORE.get(race[0], 3):
                return f"it's {days} before {race[1]}"
            race = self.races.get(d - timedelta(days=n))
            if race and n <= NO_STRENGTH_AFTER.get(race[0], 2):
                return f"it's {days} after {race[1]}"
        if self.runs.get(d) == "rest":
            return "it's a rest day"
        tomorrow = self.kind(d + timedelta(days=1))
        if tomorrow in HARD or tomorrow == "usual_hard":
            return "the next day is a hard run"
        if tomorrow == "long":
            return "the next day is a long run"
        return None

    def light(self, d: date) -> bool:
        return any(self.races.get(d + timedelta(days=n), ("", ""))[0] == "all_out" for n in range(1, LIGHT_BEFORE_ALL_OUT + 1))

    def score(self, d: date) -> int:
        k = self.kind(d)
        return 3 if k in HARD or k == "usual_hard" else 1 if k in ("long", None) else 2

    def timing(self, d: date) -> str:
        k = self.kind(d)
        if k in HARD or k == "usual_hard":
            return "after your run, never before it"
        return "after your run or later in the day" if k else ""


def _pick(candidates: list[tuple[date, int]], quota: int, taken: list[date], per_week: int) -> list[date]:
    """The best `quota` days of a week (fewer if they don't fit): highest score, then most spread out, then earliest."""
    for n in range(min(quota, len(candidates)), 0, -1):
        options = []
        for combo in itertools.combinations(candidates, n):
            days = [d for d, _ in combo]
            everything = sorted(taken + days)
            gaps = [(b - a).days for a, b in zip(everything, everything[1:])]
            if any(g < MIN_GAP_DAYS for g in gaps):
                continue
            if any(sum(0 <= (d - other).days < 7 for other in everything) > per_week for d in days):
                continue  # never more than a week's worth in any 7 days
            options.append((sum(s for _, s in combo), min([*gaps, 3]), [-d.toordinal() for d in days], days))
        if options:
            return max(options)[3]
    return []


def refit(conn: sqlite3.Connection, today: date | None = None) -> int:
    """Re-place the upcoming strength sessions around the current running plan. Returns how many were written.

    Sessions before today stay as they are (and count toward their week); so does today's once it's done.
    """
    cfg = settings(conn)
    if not cfg:
        return 0
    today = today or date.today()
    start = date.fromisoformat(cfg["start"])
    first = max(start, today)
    if first == today and today.isoformat() in done_dates(conn):
        first += timedelta(days=1)
    conn.execute("DELETE FROM plan_sessions WHERE kind = ? AND date >= ?", (KIND, first.isoformat()))

    kept = conn.execute("SELECT date, title FROM plan_sessions WHERE kind = ? ORDER BY date, id", (KIND,)).fetchall()
    # Strength you did without it being planned counts too, for spacing and for the week's total.
    recent = {d for d in done_dates(conn) if (first - timedelta(days=7)).isoformat() <= d < first.isoformat()}
    taken = sorted({date.fromisoformat(d) for d in recent | {r["date"] for r in kept}})
    letter = next((l for l in (_letter(r["title"]) for r in reversed(kept)) if l), "B")
    cal = _Calendar(conn, today)
    last_run = max(cal.runs, default=first)
    end = _monday(max(first + timedelta(days=HORIZON_DAYS), last_run)) + timedelta(days=6)

    written = 0
    monday = _monday(first)
    while monday <= end:
        week = [monday + timedelta(days=i) for i in range(7)]
        quota = cfg["per_week"] - sum(d in week for d in taken)
        candidates = [(d, cal.score(d)) for d in week if first <= d <= end and not cal.blocked(d)]
        for d in _pick(candidates, quota, taken, cfg["per_week"]):
            letter = "B" if letter == "A" else "A"
            s = session(letter, phase_on(start, d), cfg["equipment"], cal.light(d), cal.timing(d))
            conn.execute("INSERT INTO plan_sessions (date, kind, title, details) VALUES (?, ?, ?, ?)",
                         (d.isoformat(), KIND, s["title"], s["details"]))
            taken.append(d)
            written += 1
        monday += timedelta(days=7)
    conn.commit()
    return written


def set_up(conn: sqlite3.Connection, per_week: int, equipment: str, start: str, today: date | None = None) -> int:
    save_settings(conn, per_week, equipment, start)
    return refit(conn, today)


def remove(conn: sqlite3.Connection, today: date | None = None) -> None:
    """Turn the strength plan off: upcoming sessions go, what you've already done stays in the plan."""
    today = today or date.today()
    conn.execute("DELETE FROM plan_sessions WHERE kind = ? AND date >= ?", (KIND, today.isoformat()))
    conn.execute("DELETE FROM coach_state WHERE key = 'strength'")
    conn.commit()


def issues(conn: sqlite3.Connection, today: date | None = None) -> list[dict]:
    """Upcoming strength sessions that now break a placement rule (the running plan moved under them)."""
    today = today or date.today()
    rows = conn.execute("SELECT date, title FROM plan_sessions WHERE kind = ? AND date >= ? ORDER BY date",
                        (KIND, today.isoformat())).fetchall()
    if not rows:
        return []
    cal = _Calendar(conn, today)
    days = [date.fromisoformat(r["date"]) for r in rows]
    done = done_dates(conn)
    out = []
    for r, d in zip(rows, days):
        if r["date"] in done:
            continue  # already done: nothing left to move
        reason = cal.blocked(d)
        if not reason and d - timedelta(days=1) in days:
            reason = "it's the day after another strength session"
        if reason:
            out.append({"date": r["date"], "title": r["title"], "reason": reason})
    return out


# ---------- for the page, the coach and the status card ----------
def overview(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    cfg = settings(conn)
    out = {
        "enabled": bool(cfg),
        "settings": cfg or {**DEFAULTS, "start": today.isoformat()},
        "equipment": EQUIPMENT,
        "rules": RULES,
        "phases": [{"label": p["label"], "weeks": p["weeks"], "sets": p["sets"], "minutes": p["minutes"]} for p in PHASES],
    }
    if not cfg:
        return out
    start = date.fromisoformat(cfg["start"])
    p = phase_on(start, max(today, start))
    out["phase"] = {"key": p["key"], "label": p["label"], "week": p["week"], "phase_week": p["phase_week"], "of": p["weeks"],
                    "starts_in_days": max(0, (start - today).days)}
    done = done_dates(conn)
    week = {(_monday(today) + timedelta(days=i)).isoformat() for i in range(7)}
    planned = [r["date"] for r in conn.execute("SELECT date FROM plan_sessions WHERE kind = ?", (KIND,))]
    out["this_week"] = {"planned": sum(d in week for d in planned), "done": len(week & done)}
    nxt = conn.execute("SELECT date, title FROM plan_sessions WHERE kind = ? AND date >= ? ORDER BY date", (KIND, today.isoformat())).fetchall()
    out["next"] = next(({"date": r["date"], "title": r["title"]} for r in nxt if r["date"] not in done), None)
    out["issues"] = issues(conn, today)
    return out


def brief(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Compact strength picture for the coach."""
    today = today or date.today()
    o = overview(conn, today)
    if not o["enabled"]:
        return {"enabled": False, "placement_rules": RULES}
    since = (today - timedelta(days=28)).isoformat()
    done = done_dates(conn)
    planned = [r["date"] for r in conn.execute("SELECT date FROM plan_sessions WHERE kind = ? AND date >= ? AND date < ?",
                                               (KIND, since, today.isoformat()))]
    return {
        "enabled": True,
        "settings": o["settings"],
        "phase": o["phase"],
        "last_4_weeks": {"planned": len(planned), "done": sum(d >= since for d in done)},
        "done_dates": sorted(d for d in done if d >= since),
        "next": o["next"],
        "clashes_with_runs": o["issues"],
        "placement_rules": RULES,
    }
