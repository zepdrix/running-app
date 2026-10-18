"""Strava as the source of your runs: sign-in (OAuth), and a sync that mirrors the Garmin one.

Strava only lets an app you register yourself read your account, so setup asks
for your own app's Client ID and Client Secret (strava.com/settings/api). They
and the tokens Strava issues are kept in data/strava_tokens.json.

Each run costs two requests (its streams and its laps) and Strava allows 100
requests per 15 minutes and 1,000 per day, so a long history downloads in
stages: the sync waits for the next 15-minute window by itself and stops for
the day when the daily allowance is used up. Newest runs come first.

What Strava doesn't provide (see source.py): body data, weather, VO2max and
running dynamics.
"""

import json
import logging
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime

from . import races, source, store, zones
from .streams import Lap, Sample

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
SCOPE = "read,activity:read_all"
PAGE_SIZE = 100
STREAM_KEYS = "time,distance,heartrate,cadence,altitude,latlng,velocity_smooth"
HEADROOM = 2          # stop this many requests short of a limit
WINDOW_S = 15 * 60    # the short limit resets on the quarter hour
FAILED = "Sync failed"

# Strava sport types -> the activity types the rest of the app knows (Garmin's names).
SPORT_TYPES = {
    "Run": "running", "TrailRun": "trail_running", "VirtualRun": "virtual_run",
    "Ride": "cycling", "VirtualRide": "virtual_ride", "MountainBikeRide": "mountain_biking", "GravelRide": "gravel_cycling",
    "Swim": "lap_swimming", "Walk": "walking", "Hike": "hiking", "WeightTraining": "strength_training",
    "Workout": "indoor_cardio", "Yoga": "yoga", "Elliptical": "elliptical", "Rowing": "rowing",
}


class NotConnected(Exception):
    pass


class StravaError(Exception):
    pass


class RateLimited(Exception):
    """A limit is used up. `daily` tells the two apart: the short one resets within 15 minutes."""

    def __init__(self, daily: bool):
        super().__init__("Strava's daily request limit is used up" if daily else "Strava's 15-minute request limit is used up")
        self.daily = daily


# ---------- HTTP ----------
def _request(url: str, data: dict | None = None, token: str | None = None) -> tuple[object, dict]:
    """One request to Strava; returns (parsed JSON, response headers)."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read() or b"null"), dict(res.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 429:
            raise RateLimited(daily=_used_up(dict(e.headers)) == "daily") from e
        if e.code == 401:
            raise NotConnected("Strava no longer accepts the saved sign-in. Reconnect Strava in Settings.") from e
        if e.code == 404:
            raise LookupError(url) from e
        raise StravaError(f"Strava answered {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise StravaError("Couldn't reach Strava. Check the internet connection and try again.") from e


def _used_up(headers: dict) -> str | None:
    """'short' or 'daily' when a limit is (nearly) reached, judging by the usage headers."""
    h = {k.lower(): v for k, v in headers.items()}
    for kind in ("x-readratelimit", "x-ratelimit"):
        try:
            limits = [int(x) for x in h[f"{kind}-limit"].split(",")]
            used = [int(x) for x in h[f"{kind}-usage"].split(",")]
        except (KeyError, ValueError):
            continue
        if used[1] >= limits[1] - HEADROOM:
            return "daily"
        if used[0] >= limits[0] - HEADROOM:
            return "short"
    return None


# ---------- sign-in ----------
_pending: dict[str, dict] = {}  # state -> the app keys entered on the setup page, until Strava sends you back


def authorize_url(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Remember the app's keys and return the Strava page that asks you to approve access."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.isdigit():
        raise StravaError("The Client ID is a number, shown at the top of your Strava API page.")
    if not re.fullmatch(r"[0-9a-fA-F]{20,64}", client_secret):
        raise StravaError("That doesn't look like a Client Secret. Copy it from your Strava API page (click “show”).")
    state = secrets.token_urlsafe(16)
    _pending.clear()  # one sign-in at a time
    _pending[state] = {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri}
    query = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "approval_prompt": "auto", "scope": SCOPE, "state": state}
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}"


def finish_sign_in(state: str, code: str, granted_scope: str, request=None) -> dict:
    """Trade the code Strava sent back for tokens and save them. Returns the athlete."""
    request = request or _request
    app = _pending.pop(state, None)
    if not app:
        raise StravaError("That sign-in has expired. Start again from the setup page.")
    if "activity:read" not in (granted_scope or ""):
        raise StravaError("Strava wasn't given permission to read your activities. Try again and leave that box ticked.")
    tokens, _ = request(TOKEN_URL, {"client_id": app["client_id"], "client_secret": app["client_secret"],
                                    "code": code, "grant_type": "authorization_code"})
    _save_tokens({**app, **{k: tokens[k] for k in ("access_token", "refresh_token", "expires_at")}})
    return tokens.get("athlete") or {}


def _save_tokens(data: dict) -> None:
    path = source.strava_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: data[k] for k in ("client_id", "client_secret", "access_token", "refresh_token", "expires_at")}))
    path.chmod(0o600)


class Client:
    """Reads your Strava account, refreshing the access token (they last six hours) as needed."""

    def __init__(self, request=None, now=time.time):
        self._request, self._now = request or _request, now
        try:
            self._tokens = json.loads(source.strava_token_path().read_text())
        except (OSError, ValueError) as e:
            raise NotConnected("Not connected to Strava. Open Settings to connect it.") from e

    def _token(self) -> str:
        t = self._tokens
        if t["expires_at"] - self._now() < 120:
            fresh, _ = self._request(TOKEN_URL, {"client_id": t["client_id"], "client_secret": t["client_secret"],
                                                 "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            t.update({k: fresh[k] for k in ("access_token", "refresh_token", "expires_at")})
            _save_tokens(t)
        return t["access_token"]

    limit_reached: str | None = None       # 'short' or 'daily' once the last answer said a limit is used up
    before_request = staticmethod(lambda: None)  # the sync hooks in here to wait out the 15-minute limit

    def get(self, path: str, **params):
        """GET from the API, waiting for the next 15-minute window when the limit requires it."""
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        for attempt in (1, 2):
            self.before_request()
            try:
                data, headers = self._request(f"{API}{path}{query}", None, self._token())
            except RateLimited as e:
                if e.daily or attempt == 2:
                    raise
                self.limit_reached = "short"  # Strava said no before the headers warned us: wait, then once more
                continue
            self.limit_reached = _used_up(headers)
            return data


# ---------- Strava's shapes -> the app's ----------
def _local(ts: str | None) -> str | None:
    """'2026-10-01T17:05:09Z' -> '2026-10-01 17:05:09' (start_date_local is local time despite the Z)."""
    return ts[:19].replace("T", " ") if ts else None


def to_summary(a: dict) -> dict:
    """A Strava activity as the summary the rest of the app stores (Garmin's field names)."""
    sport = a.get("sport_type") or a.get("type") or ""
    type_key = SPORT_TYPES.get(sport) or re.sub(r"(?<!^)(?=[A-Z])", "_", sport).lower() or None
    if type_key == "running" and a.get("trainer"):
        type_key = "treadmill_running"
    race = a.get("workout_type") == 1 and "run" in (type_key or "")
    cadence = a.get("average_cadence")
    return {
        "activityId": a["id"],
        "activityName": a.get("name"),
        "activityType": {"typeKey": type_key},
        "eventType": {"typeKey": "race" if race else None},
        "startTimeLocal": _local(a.get("start_date_local")),
        "startTimeGMT": _local(a.get("start_date")),
        "distance": a.get("distance"),
        # A race is timed gun to finish; any other run counts the time you were actually moving, the
        # closest thing Strava has to a watch's timer (which stops when you pause at a light).
        "duration": a.get("elapsed_time") if race else a.get("moving_time"),
        "movingDuration": a.get("moving_time"),
        "elevationGain": a.get("total_elevation_gain"),
        "averageSpeed": a.get("average_speed"),
        "averageHR": a.get("average_heartrate"),
        "maxHR": a.get("max_heartrate"),
        # Strava counts one foot's steps for runs; the app (like Garmin) counts both.
        "averageRunningCadenceInStepsPerMinute": cadence * 2 if cadence and "run" in (type_key or "") else None,
        "calories": a.get("calories"),
        "strava": a,
    }


def to_samples(streams) -> list[Sample]:
    """Strava's per-second streams as samples. Accepts both shapes the API returns."""
    if isinstance(streams, list):
        streams = {s.get("type"): s for s in streams if isinstance(s, dict)}
    col = {k: (v or {}).get("data") or [] for k, v in (streams or {}).items()}
    times = col.get("time") or []

    def at(key, i):
        values = col.get(key) or []
        return values[i] if i < len(values) else None

    out = []
    for i, t in enumerate(times):
        pos, cadence = at("latlng", i), at("cadence", i)
        out.append(Sample(
            t=float(t), distance_m=at("distance", i), hr=at("heartrate", i), speed_mps=at("velocity_smooth", i),
            cadence=cadence * 2 if cadence is not None else None, altitude_m=at("altitude", i),
            lat=pos[0] if pos else None, lon=pos[1] if pos else None,
        ))
    return out


def to_laps(laps) -> list[Lap]:
    out = []
    for lap in laps or []:
        cadence = lap.get("average_cadence")
        out.append(Lap(distance_m=lap.get("distance"), duration_s=lap.get("elapsed_time"), avg_hr=lap.get("average_heartrate"),
                       avg_speed_mps=lap.get("average_speed"), cadence_spm=cadence * 2 if cadence else None))
    return out


# ---------- sync ----------
def _pause_for_limit(client: Client, progress, sleep, now) -> None:
    """Called between requests: wait out the 15-minute limit, or give up for the day."""
    if not client.limit_reached:
        return
    if client.limit_reached == "daily":
        raise RateLimited(daily=True)
    wait = WINDOW_S - now() % WINDOW_S + 5
    resume = datetime.fromtimestamp(now() + wait).strftime("%H:%M")
    progress(f"Strava's limit of 100 requests per 15 minutes is reached. Carrying on at {resume}.")
    sleep(wait)
    client.limit_reached = None


def sync_summaries(client: Client, conn: sqlite3.Connection, full: bool, progress) -> int:
    """Page through your activities newest first; stop at the first page that's entirely known (unless `full`)."""
    seen, page_no = 0, 1
    while True:
        page = client.get("/athlete/activities", page=page_no, per_page=PAGE_SIZE)
        if not page:
            break
        marks = ",".join("?" * len(page))
        known = conn.execute(f"SELECT COUNT(*) FROM activities WHERE detail_synced = 1 AND id IN ({marks})", [a["id"] for a in page]).fetchone()[0]
        for a in page:
            store.upsert_summary(conn, to_summary(a))
        conn.commit()
        seen += len(page)
        progress(f"Fetched {seen} activity summaries")
        if (not full and known == len(page)) or len(page) < PAGE_SIZE:
            break
        page_no += 1
    return seen


def sync_detail(client: Client, conn: sqlite3.Connection, activity_id: int) -> None:
    """The streams and laps of one run."""
    try:
        streams = client.get(f"/activities/{activity_id}/streams", keys=STREAM_KEYS, key_by_type="true")
    except LookupError:
        streams = {}  # entered by hand: nothing was recorded
    try:
        laps = client.get(f"/activities/{activity_id}/laps")
    except LookupError:
        laps = []
    store.store_detail(conn, activity_id, to_samples(streams), to_laps(laps))
    conn.commit()


def run_sync(conn: sqlite3.Connection, client: Client, full: bool = False, progress: Callable[[str], None] = print,
             sleep=time.sleep, now=time.time) -> str:
    log_id = conn.execute("INSERT INTO sync_log (started_at, status) VALUES (datetime('now'), 'running')").lastrowid
    conn.commit()

    client.before_request = lambda: _pause_for_limit(client, progress, sleep, now)
    done, total, status = 0, 0, "ok"
    try:
        sync_summaries(client, conn, full, progress)
        # Only runs have anything the app uses beyond the summary.
        conn.execute("UPDATE activities SET detail_synced = 1 WHERE detail_synced = 0 AND is_run = 0")
        conn.commit()
        pending = [r["id"] for r in conn.execute("SELECT id FROM activities WHERE detail_synced = 0 ORDER BY start_local DESC")]
        total, failed = len(pending), 0
        for n, activity_id in enumerate(pending, 1):
            progress(f"Downloading runs {n}/{total}")
            try:
                sync_detail(client, conn, activity_id)
                done += 1
            except (RateLimited, NotConnected):
                raise
            except Exception as e:
                failed += 1
                log.warning("Failed to sync Strava activity %s: %s", activity_id, e)
                conn.rollback()
        zones.backfill(conn)
        races.link_activities(conn)
        message = f"Synced {done} new activities"
        if failed:
            message += f" ({failed} failed; they will be retried next sync)"
    except RateLimited as e:
        when = "after midnight UTC" if e.daily else "in 15 minutes"
        limit = "daily limit" if e.daily else "request limit"
        message = (f"Synced {done} new activities. Strava's {limit} is used up, so the other {total - done} "
                   f"will download on a sync {when}.") if total else f"Strava's {limit} is used up. Sync again {when}."
        zones.backfill(conn)
        races.link_activities(conn)
    except Exception as e:
        message, status = f"{FAILED}: {e}", "error"
        log.exception("Strava sync failed")
    conn.execute("UPDATE sync_log SET finished_at = datetime('now'), status = ?, message = ? WHERE id = ?", (status, message, log_id))
    conn.commit()
    progress(message)
    return message
