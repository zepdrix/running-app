"""Re-read the saved original FIT files into the database.

Used after an upgrade starts storing more fields (e.g. running dynamics). Works
entirely from data/raw/, so nothing is downloaded from Garmin.
"""

import logging
import sqlite3
import time

from . import config, store
from .streams import fit_bytes_from_zip, parse_fit

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, progress=print) -> int:
    ids = [r[0] for r in conn.execute("SELECT id FROM activities WHERE detail_synced = 1 ORDER BY start_local")]
    started, done = time.time(), 0
    for n, activity_id in enumerate(ids, 1):
        path = config.RAW_DIR / "activities" / str(activity_id) / "original.zip"
        if not path.exists():
            continue
        fit = fit_bytes_from_zip(path.read_bytes())
        if not fit:
            continue
        try:
            samples, laps = parse_fit(fit)
        except Exception as e:
            log.warning("Skipping %s: %s", activity_id, e)
            continue
        if not samples:
            continue  # nothing usable in the file: keep what the sync saved from Garmin's chart data
        store.store_detail(conn, activity_id, samples, laps)
        done += 1
        if n % 50 == 0:
            progress(f"Re-read {n}/{len(ids)} activities ({time.time() - started:.0f}s)")
    progress(f"Re-read {done} activities in {time.time() - started:.0f}s")
    return done
