"""AI running coach, powered by the local Claude Code CLI (uses your Claude login).

Each reply runs `claude -p` with every built-in tool disabled. The only tools
are the read-only database tools in coach_tools.py, so the coach can look at
your training data but can't touch files, the shell or the network.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

from . import config, db, plan, zones

TIMEOUT_S = 300
TOOLS = ["mcp__running__training_snapshot", "mcp__running__query_sql", "mcp__running__propose_plan_changes"]
_lock = threading.Lock()

SYSTEM_PROMPT = """You are an experienced, encouraging running coach chatting with one athlete inside \
their personal training dashboard. Today is {today}. The athlete prefers {units}.

Your tools:
- training_snapshot: the current picture (weekly volume, load, HR zones, goals, records, predictions, recent runs, body data, race log, recent threshold sessions rep by rep). Call it before answering anything about their training.
- query_sql: read-only SQLite SELECT queries for anything deeper (a specific run's laps or HR, trends over months, comparisons).
- propose_plan_changes: propose dated plan changes for the athlete to approve (see below).

Database schema (distances in meters, times in seconds, speeds in m/s, start_local is local time):
{schema}

Coaching guidelines:
- Ground every claim in their data; quote the actual numbers (distances in {units}).
- Paces: ALWAYS give every pace in min:sec per mile (e.g. 7:15/mi), never per km, even when distances are in kilometers. Convert any per-km pace you find in the data or in earlier messages (1 mile = 1.609344 km).
- Be concrete: specific sessions, paces, heart-rate zones, weekly distances. When building a plan, keep weekly volume increases around 10% or less and include easy days, recovery weeks and a taper before races.
- {zones}
- Most running should be easy (zones 1-2); point out when easy runs are run too hard.
- If data is missing for a question (no HR, no recent runs), say so and work with what exists.
- Never diagnose injuries or illness. For pain, persistent fatigue, chest symptoms or dizziness, advise stopping and seeing a medical professional.
- Keep answers short and scannable: a few sentences or a short list. Use plain Markdown (bold, lists, short tables); no headings bigger than ###.
- Plan changes: when they ask you to change their plan, or agree to changes you suggested, call propose_plan_changes with dated operations (look at plan_sessions or the plan below for what's there now). It only creates a proposal: tell them to review it and click Apply under your message. Don't propose changes they haven't asked for or agreed to. You can't edit goals or other data; for goals, point them to the Goals card.
- Weather: activity_weather has the conditions for outdoor runs (temp and dew point in °F). Heat costs pace and raises heart rate: heat_cost_pct (from the temperature + dew point rule) is the % slowdown at the same effort, and *_adj / adjusted_time_s values remove it. Use heat-adjusted numbers when judging fitness across seasons, and say when heat explains a slow or high-HR run.
- Form: running dynamics are compared at matched paces (form changes with pace, so never compare raw averages across different paces). In samples and laps: gct_ms = ground contact time, vo_mm = vertical oscillation, vr_pct = vertical ratio, step_mm = step length, balance_pct = % of ground contact on the left foot (only count seconds actually running: speed_mps >= 1.5 and cadence >= 130). Don't diagnose injuries from form data; for a persistent imbalance or pain, suggest seeing a physio.
- Races: the race log has official results (prefer them over watch times), how hard each race was run (easy/controlled races aren't fitness markers) and how long each course measured on the watch.
- plan_sessions holds their planned sessions (date, kind, title, details).
- The wellness table holds daily raw Garmin JSON (kinds: stats, sleep, hrv, readiness, training_status); training_snapshot already includes a summary of the last few days.
{context}"""

NOTES_SECTION = """
The athlete wrote these notes about themselves and their training. Treat them as true and current, and \
prefer them over what you'd infer from the data (e.g. an official race time over a watch measurement). \
Reconcile them with the data when relevant:
<athlete_notes>
{notes}
</athlete_notes>"""

PLAN_SECTION = """
The athlete's current training plan (they maintain it; follow it unless their data says they need to adjust, \
and say so when you deviate):
<training_plan>
{plan}
</training_plan>"""


def context_sections(notes: str, plan: str) -> str:
    return (NOTES_SECTION.format(notes=notes) if notes else "") + (PLAN_SECTION.format(plan=plan) if plan else "")


class CoachError(Exception):
    pass


class CoachTimeout(CoachError):
    pass


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coach_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            role        TEXT NOT NULL,      -- user | coach
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS coach_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )


def _claude_bin() -> str:
    found = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(found).exists():
        raise CoachError("Claude Code isn't installed or isn't on PATH. Install it from https://claude.com/claude-code and log in.")
    return found


def history(conn: sqlite3.Connection) -> list[dict]:
    _ensure_tables(conn)
    return [dict(r) for r in conn.execute("SELECT id, role, content, created_at FROM coach_messages ORDER BY id")]


def _get_state(conn: sqlite3.Connection, key: str) -> str:
    _ensure_tables(conn)
    row = conn.execute("SELECT value FROM coach_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] else ""


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    _ensure_tables(conn)
    conn.execute("INSERT OR REPLACE INTO coach_state VALUES (?, ?)", (key, value.strip()))
    conn.commit()


def get_notes(conn: sqlite3.Connection) -> str:
    return _get_state(conn, "athlete_notes")


def set_notes(conn: sqlite3.Connection, notes: str) -> None:
    _set_state(conn, "athlete_notes", notes)


def get_plan(conn: sqlite3.Connection) -> str:
    return plan.as_text(conn)


def reset(conn: sqlite3.Connection) -> None:
    _ensure_tables(conn)
    conn.execute("DELETE FROM coach_messages")
    conn.execute("DELETE FROM coach_state WHERE key = 'session_id'")
    conn.commit()


def run_claude(message: str, system_prompt: str, extra_args: list[str], timeout: int = TIMEOUT_S) -> dict:
    """Run `claude -p` with no built-in tools and none of your personal settings; return its JSON result."""
    cmd = [
        _claude_bin(), "-p",                # the message goes in on stdin: as an argument, one starting with "-" is read as an option
        "--output-format", "json",
        "--system-prompt", system_prompt,
        "--tools", "",                      # no built-in tools: no shell, files or web
        "--strict-mcp-config",              # ignore any MCP servers you have configured
        "--permission-mode", "dontAsk",     # anything not explicitly allowed is refused, never prompted
        "--setting-sources", "",            # don't load your personal Claude Code settings/hooks
        *extra_args,
    ]
    try:
        proc = subprocess.run(
            cmd, input=message, capture_output=True, text=True, timeout=timeout, cwd=config.ROOT,
            env={**os.environ, "RUNAPP_DATA_DIR": str(config.DATA_DIR)},
        )
    except subprocess.TimeoutExpired as e:
        raise CoachTimeout("Claude took too long to answer.") from e
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        raise CoachError(f"Claude Code failed: {detail or 'no output'}. Is it logged in? Run `claude` once in a terminal to check.") from e
    if result.get("is_error"):
        raise CoachError(f"Claude Code error: {result.get('result') or result.get('subtype')}")
    return result


def _coach_args(session_id: str | None) -> list[str]:
    mcp = {
        "mcpServers": {
            "running": {
                "command": sys.executable,
                "args": ["-m", "runapp.coach_tools"],
                "cwd": str(config.ROOT),
                "env": {"RUNAPP_DATA_DIR": str(config.DATA_DIR)},
            }
        }
    }
    args = ["--mcp-config", json.dumps(mcp), "--allowedTools", ",".join(TOOLS)]
    if session_id:
        args += ["--resume", session_id]
    return args


def system_prompt(units: str, notes: str, plan: str, zone_text: str = "") -> str:
    return SYSTEM_PROMPT.format(
        zones=zone_text,
        today=date.today().strftime("%A %Y-%m-%d"),
        units="kilometers" if units == "km" else "miles",
        schema=db.SCHEMA.strip(),
        context=context_sections(notes, plan),
    )


def _run(message: str, session_id: str | None, units: str, notes: str, plan_text: str, zone_text: str) -> dict:
    return run_claude(message, system_prompt(units, notes, plan_text, zone_text), _coach_args(session_id))


def ask(conn: sqlite3.Connection, message: str, units: str = "km") -> dict:
    _ensure_tables(conn)
    if not _lock.acquire(blocking=False):
        raise CoachError("The coach is still answering your previous message.")
    try:
        row = conn.execute("SELECT value FROM coach_state WHERE key = 'session_id'").fetchone()
        session_id = row["value"] if row else None
        notes, plan_text, zone_text = get_notes(conn), get_plan(conn), zones.prompt_text(conn)
        try:
            result = _run(message, session_id, units, notes, plan_text, zone_text)
        except CoachError as e:
            if not session_id or "No conversation found" not in str(e):
                raise
            # The saved conversation is gone (e.g. Claude Code cleaned it up); start a new one.
            result = _run(message, None, units, notes, plan_text, zone_text)

        reply = result.get("result", "").strip()
        conn.execute("INSERT INTO coach_messages (role, content) VALUES ('user', ?)", (message,))
        conn.execute("INSERT INTO coach_messages (role, content) VALUES ('coach', ?)", (reply,))
        conn.execute(
            "INSERT OR REPLACE INTO coach_state VALUES ('session_id', ?)", (result.get("session_id"),)
        )
        conn.commit()
        return {"reply": reply}
    finally:
        _lock.release()
