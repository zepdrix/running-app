"""Where your runs come from: Garmin Connect or Strava. One or the other, never both.

The two services describe the same run differently (ids, laps, time moving), so
mixing them would count runs twice. The choice is made on the setup page and
kept in data/source.json; to change it you disconnect, which removes the
downloaded activities (your plan, races, goals and notes stay).
"""

import json
import sqlite3

from . import config

SOURCES = {
    "garmin": {
        "label": "Garmin",
        "activity_url": "https://connect.garmin.com/modern/activity/{id}",
        # What only Garmin provides.
        "body_data": True, "weather": True, "running_form": True, "send_to_watch": True,
    },
    "strava": {
        "label": "Strava",
        "activity_url": "https://www.strava.com/activities/{id}",
        "body_data": False, "weather": False, "running_form": False, "send_to_watch": False,
    },
}

# Everything downloaded from the service. Disconnecting empties these; what you entered yourself is kept.
SYNCED_TABLES = ("samples", "laps", "best_efforts", "form_stats", "activity_weather", "activities",
                 "snapshots", "vo2max_daily", "wellness", "status_reports", "garmin_workouts", "sync_log")


class SourceError(Exception):
    pass


def _path():
    return config.DATA_DIR / "source.json"


def strava_token_path():
    return config.DATA_DIR / "strava_tokens.json"


def current() -> str | None:
    """'garmin', 'strava', or None when nothing has been connected yet."""
    try:
        key = json.loads(_path().read_text()).get("source")
        if key in SOURCES:
            return key
    except (OSError, ValueError):
        pass
    # Set up before the choice existed: a saved Garmin session means Garmin.
    return "garmin" if config.TOKEN_PATH.exists() else None


def choose(key: str) -> None:
    """Record the choice. Refuses to change an existing one: disconnect first."""
    if key not in SOURCES:
        raise SourceError(f"Unknown source {key!r}")
    now = current()
    if now and now != key:
        raise SourceError(f"This app is connected to {SOURCES[now]['label']}. Disconnect it before connecting {SOURCES[key]['label']}.")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps({"source": key}))


def info() -> dict | None:
    """The current source as the page needs it."""
    key = current()
    return {"key": key, **SOURCES[key]} if key else None


def connected() -> bool:
    """A source is chosen and its saved sign-in is on disk (it may still have expired)."""
    key = current()
    if key == "garmin":
        return config.TOKEN_PATH.exists()
    return key == "strava" and strava_token_path().exists()


def disconnect(conn: sqlite3.Connection) -> None:
    """Forget the source and remove everything downloaded from it, so the other one can be connected."""
    for table in SYNCED_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("UPDATE races SET activity_id = NULL")  # they re-link to the new source's runs by date
    conn.commit()
    for path in (config.TOKEN_PATH, strava_token_path(), _path()):
        path.unlink(missing_ok=True)


def prompt_note() -> str:
    """What the AI should know about the data it's looking at."""
    if current() != "strava":
        return ""
    return ("The athlete's data comes from Strava, not a Garmin account: there is no sleep, HRV, resting heart rate, "
            "readiness or Body Battery, no VO2max estimate, no weather and no running-dynamics data (ground contact, "
            "vertical ratio, balance). Don't say the watch failed to record these; say they aren't available from Strava "
            "when it matters, and judge readiness from training load and what the athlete tells you.")
