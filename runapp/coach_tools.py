"""MCP server exposing the coach's only tools.

Claude Code launches this over stdio for each coach reply. The coach gets no
shell or file access. Everything here is read-only except propose_plan_changes,
which only stores a proposal: the athlete has to click Apply before the plan changes.
"""

import json
from datetime import date

from mcp.server.mcpserver import MCPServer

from . import analysis, db, form, plan, query, races, strength, wellness, workouts

server = MCPServer("running", instructions="Read-only access to the user's running database.")


@server.tool()
def training_snapshot() -> str:
    """Current training picture: weekly volume (12 weeks), load ratio, HR-zone share,
    VO2max, goals with progress, personal records, race predictions, the 15 most
    recent activities, and body data (sleep, HRV, resting HR, Body Battery, readiness)
    for the last 5 days, the race log (official results, effort, courses, upcoming races),
    recent threshold/interval sessions rep by rep (paces in seconds per mile), running form
    (cadence, ground contact, vertical ratio and stride at matched pace bands; form fatigue within
    runs; left/right balance), strength training (phase, sessions done, clashes with runs), the plan for the last 3 weeks next to
    what was actually done each day (state: done, harder, missed, rested, ran, today), and any plan
    changes still waiting for the athlete's approval. Distances in meters, times in seconds.
    Call this first."""
    conn = db.connect()
    try:
        data = {
            "today": date.today().isoformat(),
            "overview": analysis.overview(conn),
            "weekly": analysis.weekly(conn, 12),
            "goals": analysis.goal_progress(conn),
            "records": analysis.personal_records(conn),
            "predictions": analysis.predictions(conn),
            "recent_activities": analysis.recent_activities(conn, 15),
            "body": wellness.summarize(conn, days=5),
            "races": races.prompt_summary(conn),
            "recent_quality_sessions": workouts.brief(conn, 5),
            "running_form": form.brief(conn),
            "strength": strength.brief(conn),
            "plan_vs_actual": plan.recent_with_actuals(conn),
            "pending_plan_proposals": plan.proposals(conn),
        }
    finally:
        conn.close()
    return json.dumps(data, default=str)


@server.tool()
def query_sql(sql: str) -> str:
    """Run one read-only SQLite SELECT against the database (max 200 rows, 60,000 characters
    and 10 seconds). The tables are listed in your instructions. Aggregate the samples table
    rather than selecting it raw (it has a row every second or so), and leave out raw_json
    columns unless you need them."""
    return query.run(sql)


@server.tool()
def propose_plan_changes(summary: str, changes: list[dict]) -> str:
    """Propose changes to the athlete's training plan. Nothing changes until they click Apply.

    summary: one or two sentences explaining why.
    changes: dated operations, each one of
      {"op": "set", "date": "YYYY-MM-DD", "kind": K, "title": "...", "details": "..."}  replace that day's session
      {"op": "add", "date": "YYYY-MM-DD", "kind": K, "title": "...", "details": "..."}  add a second session that day
      {"op": "remove", "date": "YYYY-MM-DD"}                                          clear that day's run
    K is one of easy, long, threshold, intervals, race, rest, cross, strength. Give paces in min:sec per mile.
    Runs and strength are separate: with kind "strength", set replaces only that day's strength session, and
    {"op": "remove", "date": ..., "kind": "strength"} removes only it; any other kind leaves the strength session alone.
    """
    conn = db.connect()
    try:
        proposal_id = plan.propose(conn, summary, changes)
    except ValueError as e:
        return f"Not saved: {e}. Fix the changes and call this tool again."
    finally:
        conn.close()
    return f"Saved as proposal #{proposal_id} with {len(changes)} change(s). The athlete will see it with Apply / Dismiss buttons."


if __name__ == "__main__":
    server.run("stdio")
