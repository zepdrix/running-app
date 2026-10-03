"""The training plan: dated sessions plus free-form notes (races, zones, rules).

The coach and the status card read it as text (as_text); the dashboard shows
it as a table with what you actually did on each day next to the plan.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import analysis

KINDS = ["easy", "long", "threshold", "intervals", "race", "rest", "cross"]
# How hard each kind is, to spot days run harder than planned.
INTENSITY = {"rest": 0, "cross": 1, "easy": 1, "long": 2, "threshold": 3, "intervals": 4, "race": 5}


def get(conn: sqlite3.Connection) -> dict:
    _migrate_from_text(conn)
    sessions = [
        dict(r) for r in conn.execute("SELECT id, date, kind, title, details FROM plan_sessions ORDER BY date, id")
    ]
    row = conn.execute("SELECT value FROM coach_state WHERE key = 'plan_notes'").fetchone()
    return {"notes": row["value"] if row and row["value"] else "", "sessions": sessions}


def save(conn: sqlite3.Connection, notes: str, sessions: list[dict]) -> None:
    conn.execute("DELETE FROM plan_sessions")
    conn.executemany(
        "INSERT INTO plan_sessions (date, kind, title, details) VALUES (?, ?, ?, ?)",
        [
            (s["date"], s["kind"] if s["kind"] in KINDS else "easy", s["title"].strip(), (s.get("details") or "").strip())
            for s in sessions
            if s.get("date") and (s.get("title") or "").strip()
        ],
    )
    conn.execute("INSERT OR REPLACE INTO coach_state VALUES ('plan_notes', ?)", (notes.strip(),))
    # The old free-text plan is superseded once a structured plan is saved.
    conn.execute("DELETE FROM coach_state WHERE key = 'training_plan'")
    conn.commit()


def as_text(conn: sqlite3.Connection) -> str:
    p = get(conn)
    lines = [p["notes"]] if p["notes"] else []
    if p["sessions"]:
        lines.append("Sessions:")
        for s in p["sessions"]:
            d = date.fromisoformat(s["date"])
            text = f"- {d.strftime('%a %b')} {d.day} ({s['date']}) [{s['kind']}]: {s['title']}"
            if s["details"]:
                text += f". {s['details']}"
            lines.append(text)
    return "\n".join(lines)


def with_actuals(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """The plan plus, for each session, what was actually done that day."""
    today = today or date.today()
    p = get(conn)
    if not p["sessions"]:
        return p
    first = p["sessions"][0]["date"]
    by_day: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM activities WHERE start_local >= ? ORDER BY start_local", (first,)):
        by_day.setdefault(r["start_local"][:10], []).append(
            {"id": r["id"], "name": r["name"], "kind": analysis.workout_kind(r), "distance_m": r["distance_m"], "duration_s": r["duration_s"]}
        )
    for s in p["sessions"]:
        done = by_day.get(s["date"], [])
        runs = [a for a in done if a["kind"] != "cross"]
        s["actual"] = done
        if s["date"] > today.isoformat():
            s["state"] = "upcoming"
        elif s["kind"] == "rest":
            s["state"] = "rested" if not runs else "ran"
        elif runs:
            hardest = max(INTENSITY[a["kind"]] for a in runs)
            s["state"] = "harder" if hardest > INTENSITY[s["kind"]] + 1 else "done"
        else:
            s["state"] = "today" if s["date"] == today.isoformat() else "missed"
    return p


# ---------- one-time conversion of the old free-text plan ----------

LINE = re.compile(r"^\s*-\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+([A-Z][a-z]{2})\w*\s+(\d{1,2}):\s*(.+)$")


def guess_kind(text: str) -> str:
    t = text.lower()
    if re.match(r"^(rest\b|rest day)", t):
        return "rest"
    reps = re.search(r"\d+\s*x\s*\d", t)
    # "race" as an event, not "race pace"; or a race distance that isn't a rep session.
    if re.search(r"\brace\b(?!\s*-?\s*pace)", t) or (re.search(r"\b(5k|10k|half marathon|marathon)\b", t) and not reps):
        return "race"
    if "threshold" in t:
        return "threshold"
    if "strides" in t and "easy" in t:
        return "easy"  # a few strides don't make an easy day a workout
    if re.search(r"race[- ]pace", t) or reps:
        return "intervals"
    # Same cut-off as synced runs: 75+ minutes counts as a long run.
    if re.search(r"\b(7[5-9]|[89]\d|1\d\d)\s*min\b", t) and "easy" in t:
        return "long"
    return "easy"


def parse_text(text: str, year: int) -> tuple[str, list[dict]]:
    """Split a free-text plan into dated sessions and everything else (kept as notes)."""
    sessions, notes = [], []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            notes.append(line)
            continue
        _, mon, day, rest = m.groups()
        d = datetime.strptime(f"{mon} {day} {year}", "%b %d %Y").date()
        if sessions and d < date.fromisoformat(sessions[-1]["date"]) - timedelta(days=180):
            d = d.replace(year=year + 1)  # the plan ran past New Year
        title, _, details = rest.partition(". ")
        sessions.append({"date": d.isoformat(), "kind": guess_kind(rest), "title": title.strip(), "details": details.strip()})
    # Drop week headings that only labelled the session lines.
    kept = [n for n in notes if not re.match(r"^\s*Week\b", n)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip(), _merge_same_day_races(sessions)


def _merge_same_day_races(sessions: list[dict]) -> list[dict]:
    """A race listed both in a 'Races' summary and in the weekly schedule becomes one session."""
    out: list[dict] = []
    for s in sessions:
        twin = next((o for o in out if o["date"] == s["date"] and o["kind"] == s["kind"] == "race"), None)
        if not twin:
            out.append(s)
            continue
        # Keep the schedule's title; collect every distinct detail.
        title = s["title"] if len(s["details"]) >= len(twin["details"]) else twin["title"]
        parts = [p for p in (twin["details"], s["details"]) if p]
        twin.update(title=title, details=". ".join(dict.fromkeys(parts)))
    return sorted(out, key=lambda x: x["date"])


def _migrate_from_text(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM plan_sessions LIMIT 1").fetchone():
        return
    row = conn.execute("SELECT value FROM coach_state WHERE key = 'training_plan'").fetchone()
    if not row or not row["value"]:
        return
    notes, sessions = parse_text(row["value"], date.today().year)
    if sessions:
        save(conn, notes, sessions)


# ---------- coach proposals ----------
# A proposal is a list of dated operations:
#   {"op": "set", "date": ..., "kind": ..., "title": ..., "details": ...}  replace that day's session(s)
#   {"op": "add", ...same fields...}                                    add another session that day
#   {"op": "remove", "date": ...}                                       clear that day


def validate_changes(changes: list[dict]) -> list[dict]:
    clean = []
    for i, c in enumerate(changes, 1):
        op = c.get("op")
        if op not in ("set", "add", "remove"):
            raise ValueError(f"change {i}: op must be set, add or remove")
        try:
            date.fromisoformat(c.get("date", ""))
        except ValueError:
            raise ValueError(f"change {i}: date must be YYYY-MM-DD") from None
        item = {"op": op, "date": c["date"]}
        if op != "remove":
            if c.get("kind") not in KINDS:
                raise ValueError(f"change {i}: kind must be one of {', '.join(KINDS)}")
            if not (c.get("title") or "").strip():
                raise ValueError(f"change {i}: title is required")
            item.update(kind=c["kind"], title=c["title"].strip()[:300], details=(c.get("details") or "").strip()[:2000])
        clean.append(item)
    if not clean:
        raise ValueError("no changes given")
    return clean


def propose(conn: sqlite3.Connection, summary: str, changes: list[dict]) -> int:
    clean = validate_changes(changes)
    cur = conn.execute(
        "INSERT INTO plan_proposals (summary, changes_json) VALUES (?, ?)", (summary.strip()[:1000], json.dumps(clean))
    )
    conn.commit()
    return cur.lastrowid


def proposals(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM plan_proposals WHERE status = ? ORDER BY id", (status,)):
        changes = json.loads(r["changes_json"])
        for c in changes:  # show what each change replaces
            c["current"] = [dict(x) for x in conn.execute(
                "SELECT kind, title FROM plan_sessions WHERE date = ? ORDER BY id", (c["date"],))]
        out.append({"id": r["id"], "created_at": r["created_at"], "summary": r["summary"], "changes": changes})
    return out


def apply_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    row = conn.execute("SELECT * FROM plan_proposals WHERE id = ? AND status = 'pending'", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("That proposal isn't pending any more.")
    for c in json.loads(row["changes_json"]):
        if c["op"] in ("set", "remove"):
            conn.execute("DELETE FROM plan_sessions WHERE date = ?", (c["date"],))
        if c["op"] in ("set", "add"):
            conn.execute("INSERT INTO plan_sessions (date, kind, title, details) VALUES (?, ?, ?, ?)",
                         (c["date"], c["kind"], c["title"], c["details"]))
    conn.execute("UPDATE plan_proposals SET status = 'applied' WHERE id = ?", (proposal_id,))
    conn.commit()


def dismiss_proposal(conn: sqlite3.Connection, proposal_id: int) -> None:
    conn.execute("UPDATE plan_proposals SET status = 'dismissed' WHERE id = ? AND status = 'pending'", (proposal_id,))
    conn.commit()
