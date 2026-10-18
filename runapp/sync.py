"""Pull activities from Garmin Connect and keep a full local copy.

Raw responses and original FIT files are saved untouched under data/raw/ so
nothing is lost even if the database schema changes later; the database holds
the normalized version used by the dashboard.
"""

import getpass
import json
import logging
import socket
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from . import config, races, source, store, weather, wellness, zones
from .streams import fit_bytes_from_zip, parse_fit, samples_from_details

log = logging.getLogger(__name__)

PAGE_SIZE = 100
REQUEST_PAUSE_S = 0.5  # be gentle with Garmin between per-activity requests
FAILED = "Sync failed"  # run_sync's message starts with this when the sync as a whole failed


class NotLoggedIn(Exception):
    pass


class GarminUnreachable(Exception):
    pass


def _garmin_reachable() -> bool:
    try:
        socket.create_connection(("connect.garmin.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def login(interactive: bool = True) -> Garmin:
    """Log in using cached tokens; fall back to email/password (+MFA) if allowed.

    Your password is never stored, only the session tokens Garmin issues.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.TOKEN_PATH.exists():
        try:
            client = Garmin()
            client.login(str(config.TOKEN_PATH))
            return client
        except Exception as e:
            # The library reports a dropped connection the same way as an expired session, so check which it is.
            if not _garmin_reachable():
                raise GarminUnreachable("Couldn't reach Garmin. Check the Mac's internet connection and try again.") from e
            log.warning("Saved Garmin session no longer valid (%s)", e)
            if not interactive:
                raise NotLoggedIn("Your Garmin sign-in has expired. Open Settings and reconnect Garmin.") from e
    elif not interactive:
        raise NotLoggedIn("Not connected to Garmin. Open Settings to connect it.")

    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    client = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    try:
        client.login(str(config.TOKEN_PATH))
    except GarminConnectAuthenticationError as e:
        raise NotLoggedIn(f"Garmin login failed: {e}") from e
    config.TOKEN_PATH.chmod(0o600)
    source.choose("garmin")
    return client


# ---------- signing in from the setup page ----------
MFA_WAIT_S = 600
_web_login: dict = {}  # the sign-in waiting for its MFA code: {"client", "state", "at"}


def _login_error(e: Exception) -> NotLoggedIn:
    if not _garmin_reachable():
        return NotLoggedIn("Couldn't reach Garmin. Check the internet connection and try again.")
    if isinstance(e, GarminConnectTooManyRequestsError):
        return NotLoggedIn("Garmin is refusing sign-ins from here for now (too many attempts). Wait a while and try again.")
    if isinstance(e, GarminConnectAuthenticationError):
        return NotLoggedIn("Garmin didn't accept that email and password.")
    return NotLoggedIn(f"Garmin sign-in failed: {e}")


def _finish_web_login(client) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    client.client.dump(str(config.TOKEN_PATH))  # the session Garmin issued; the password is not kept anywhere
    config.TOKEN_PATH.chmod(0o600)
    source.choose("garmin")


def web_login(email: str, password: str) -> bool:
    """Sign in with email and password. Returns True when done, False when Garmin wants an MFA code next."""
    _web_login.clear()
    client = Garmin(email.strip(), password, return_on_mfa=True)
    try:
        status, state = client.login()
    except Exception as e:
        raise _login_error(e) from e
    if status == "needs_mfa":
        _web_login.update(client=client, state=state, at=time.monotonic())
        return False
    _finish_web_login(client)
    return True


def web_login_mfa(code: str) -> None:
    """Finish a sign-in that was waiting for the code Garmin emailed or texted."""
    pending = dict(_web_login)
    if not pending or time.monotonic() - pending["at"] > MFA_WAIT_S:
        _web_login.clear()
        raise NotLoggedIn("That sign-in has expired. Enter your email and password again.")
    try:
        pending["client"].resume_login(pending["state"], code.strip())
    except Exception as e:
        raise NotLoggedIn("Garmin didn't accept that code. Check it and try again.") from e
    _web_login.clear()
    _finish_web_login(pending["client"])


def _save_raw(activity_id: int, name: str, data) -> None:
    folder = config.RAW_DIR / "activities" / str(activity_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(data, indent=1))


def _with_backoff(fn, *args, **kwargs):
    for attempt in range(5):
        try:
            return fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError:
            wait = 30 * 2**attempt
            log.warning("Rate limited by Garmin, waiting %ss", wait)
            time.sleep(wait)
    return fn(*args, **kwargs)


def sync_summaries(client: Garmin, conn: sqlite3.Connection, full: bool, progress) -> int:
    """Page through the activity list newest-first.

    Incremental mode stops at the first page made up entirely of activities
    whose details we already have.
    """
    seen = 0
    start = 0
    while True:
        page = _with_backoff(client.get_activities, start, PAGE_SIZE)
        if not page:
            break
        known = {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM activities WHERE detail_synced = 1 AND id IN ({','.join('?' * len(page))})",
                [a["activityId"] for a in page],
            )
        }
        for summary in page:
            _save_raw(summary["activityId"], "summary.json", summary)
            store.upsert_summary(conn, summary)
        conn.commit()
        seen += len(page)
        progress(f"Fetched {seen} activity summaries")
        if not full and len(known) == len(page):
            break
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return seen


def sync_detail(client: Garmin, conn: sqlite3.Connection, activity_id: int, run: bool) -> None:
    samples, laps = [], []

    original = _with_backoff(client.download_activity, activity_id, Garmin.ActivityDownloadFormat.ORIGINAL)
    if original:
        _save_raw(activity_id, "original.zip", original)
        fit = fit_bytes_from_zip(original)
        if fit:
            try:
                samples, laps = parse_fit(fit)
            except Exception as e:  # corrupt/unsupported FIT: fall back to /details
                log.warning("Could not parse FIT for %s: %s", activity_id, e)

    splits = _with_backoff(client.get_activity_splits, activity_id)
    _save_raw(activity_id, "splits.json", splits)

    if not samples:
        details = _with_backoff(client.get_activity_details, activity_id)
        _save_raw(activity_id, "details.json", details)
        samples = samples_from_details(details or {})

    if run:
        zones = _with_backoff(client.get_activity_hr_in_timezones, activity_id)
        _save_raw(activity_id, "hr_zones.json", zones)
        store.set_hr_zones(conn, activity_id, zones)

    store.store_detail(conn, activity_id, samples, laps)
    conn.commit()


def sync_snapshots(client: Garmin, conn: sqlite3.Connection) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    for kind, fn in (
        ("race_predictions", client.get_race_predictions),
        ("personal_records", client.get_personal_record),
    ):
        try:
            data = _with_backoff(fn)
        except Exception as e:
            log.warning("Could not fetch %s: %s", kind, e)
            continue
        conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (kind, today, json.dumps(data))
        )
        conn.commit()  # before the next request, so the database isn't locked while waiting on Garmin
        (config.RAW_DIR / "snapshots").mkdir(parents=True, exist_ok=True)
        (config.RAW_DIR / "snapshots" / f"{kind}_{today}.json").write_text(json.dumps(data, indent=1))
    conn.commit()


def sync_vo2max(client: Garmin, conn: sqlite3.Connection) -> None:
    """Daily VO2max with its decimal, from Garmin's max-metrics endpoint.

    First sync walks back to your earliest activity a year at a time; later
    syncs re-check the last 30 days.
    """
    last = conn.execute("SELECT MAX(date) AS d FROM vo2max_daily").fetchone()["d"]
    if last:
        start = date.fromisoformat(last) - timedelta(days=30)
    else:
        first = conn.execute("SELECT MIN(start_local) AS d FROM activities").fetchone()["d"]
        if not first:
            return
        start = date.fromisoformat(first[:10])
    today = date.today()
    while start <= today:
        end = min(start + timedelta(days=364), today)
        entries = _with_backoff(
            client.connectapi, f"{client.garmin_connect_metrics_url}/{start.isoformat()}/{end.isoformat()}"
        )
        for entry in entries or []:
            g = (entry or {}).get("generic") or {}
            if not g.get("calendarDate") or g.get("vo2MaxPreciseValue") is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO vo2max_daily VALUES (?, ?, ?, ?, ?)",
                (g["calendarDate"], g["vo2MaxPreciseValue"], g.get("vo2MaxValue"), g.get("fitnessAge"), json.dumps(entry)),
            )
        conn.commit()
        if entries:
            folder = config.RAW_DIR / "vo2max"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{start.isoformat()}_{end.isoformat()}.json").write_text(json.dumps(entries, indent=1))
        start = end + timedelta(days=1)
    conn.commit()


def run_sync(
    conn: sqlite3.Connection,
    client: Garmin,
    full: bool = False,
    progress: Callable[[str], None] = print,
) -> str:
    log_id = conn.execute(
        "INSERT INTO sync_log (started_at, status) VALUES (datetime('now'), 'running')"
    ).lastrowid
    conn.commit()
    try:
        sync_summaries(client, conn, full, progress)
        pending = conn.execute(
            "SELECT id, is_run FROM activities WHERE detail_synced = 0 ORDER BY start_local DESC"
        ).fetchall()
        failed = 0
        for n, row in enumerate(pending, 1):
            progress(f"Downloading activity details {n}/{len(pending)}")
            try:
                sync_detail(client, conn, row["id"], bool(row["is_run"]))
            except Exception as e:
                failed += 1
                log.warning("Failed to sync activity %s: %s", row["id"], e)
                conn.rollback()
            time.sleep(REQUEST_PAUSE_S)
        zones.backfill(conn)  # activities whose details failed still get Garmin's zone times
        races.link_activities(conn)  # races entered before race day now have their run
        sync_snapshots(client, conn)
        try:
            sync_vo2max(client, conn)
        except Exception as e:
            log.warning("Could not fetch VO2max history: %s", e)
        try:
            weather.sync_weather(client, conn, progress=progress)
        except Exception as e:
            log.warning("Could not fetch weather: %s", e)
        progress("Fetching sleep, HRV and readiness")
        try:
            wellness.sync_wellness(client, conn)
        except Exception as e:
            log.warning("Could not fetch body data: %s", e)
        message = f"Synced {len(pending) - failed} new activities"
        if failed:
            message += f" ({failed} failed; they will be retried next sync)"
        status = "ok"
    except Exception as e:
        message, status = f"{FAILED}: {e}", "error"
        log.exception("Sync failed")
    conn.execute(
        "UPDATE sync_log SET finished_at = datetime('now'), status = ?, message = ? WHERE id = ?",
        (status, message, log_id),
    )
    conn.commit()
    progress(message)
    return message
