"""Read-only SQL access to the running database, used by the AI coach.

The connection is opened read-only and an authorizer rejects anything but
reads, so the coach can explore your data but never change it.
"""

import json
import sqlite3
import sys
import time

from . import config

MAX_ROWS = 200
MAX_CHARS = 60_000
TIMEOUT_S = 10
ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action == sqlite3.SQLITE_FUNCTION and arg2 in ("load_extension", "readfile", "writefile"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in ALLOWED else sqlite3.SQLITE_DENY


def run(sql: str) -> str:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    conn.set_authorizer(_authorizer)
    # Stop a runaway query (e.g. an unfiltered join over the per-second samples) instead of hanging the coach.
    deadline = time.monotonic() + TIMEOUT_S
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        if time.monotonic() > deadline:
            return f"SQL error: the query ran for more than {TIMEOUT_S} seconds. Filter by activity or date, or aggregate."
        return f"SQL error: {e}"
    finally:
        conn.close()
    out = [dict(zip(cols, r)) for r in rows[:MAX_ROWS]]
    text = json.dumps(out, default=str)
    if len(text) > MAX_CHARS:
        return (f"Result too large ({len(text):,} characters; the limit is {MAX_CHARS:,}). "
                "Select only the columns you need (not raw_json) or fewer rows.")
    if len(rows) > MAX_ROWS:
        text += f"\n(truncated to {MAX_ROWS} rows; aggregate or add LIMIT)"
    return text


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        sys.exit('usage: python -m runapp query "SELECT ..."')
    print(run(argv[0]))
