"""Local web dashboard: JSON API + a single static page."""

import asyncio
import threading
import time
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field

from . import analysis, auth, coach, config, db, form, garmin_workouts, plan, races, source, status, strava, strength, sync, workouts, zones

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Running app")
# The data and app code are text that shrinks 4-7x, which matters on a phone connection.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


class AppFiles(StaticFiles):
    """The app's own code and styles: browsers re-check them on every load (a cheap "not modified"),
    so an update shows up right away. Without this they guess how long a copy stays good, and a
    phone can run old code for hours. Vendor libraries carry their version in the file name."""

    async def get_response(self, path, scope):
        res = await super().get_response(path, scope)
        if not path.startswith("vendor/"):
            res.headers["Cache-Control"] = "no-cache"
        return res


app.mount("/static", AppFiles(directory=STATIC), name="static")

# Reachable without logging in: the login page and what it (and the home-screen icon) needs.
PUBLIC_PATHS = {"/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/static/style.css", "/static/login.js", "/static/theme.js"}


def _cross_site(request: Request) -> bool:
    """A browser request started by another website rather than by this app's own page."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if not origin:
        return False  # not a browser
    ours = {request.url.hostname, (request.headers.get("x-forwarded-host") or "").split(":")[0]}
    return urlsplit(origin).hostname not in ours


@app.middleware("http")
async def require_login(request: Request, call_next):
    # The app holds health data and can start syncs and coach runs, so other websites open in the
    # same browser must not be able to reach it: only answer to our own names, and only let our
    # own page change things.
    host = request.url.hostname or ""
    if host not in config.ALLOWED_HOSTS and not host.endswith(".ts.net"):
        return JSONResponse(
            {"detail": f"Host {host!r} isn't allowed. To allow it, start the app with RUNAPP_ALLOWED_HOSTS={host}"},
            status_code=403,
        )
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
        return JSONResponse({"detail": "Requests from other websites aren't accepted."}, status_code=403)
    path = request.url.path
    if not auth.enabled() or path in PUBLIC_PATHS or path.startswith("/static/icons/"):
        return await call_next(request)
    conn = db.connect()
    try:
        ok = auth.valid_session(conn, request.cookies.get(auth.COOKIE))
    finally:
        conn.close()
    if ok:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Log in first"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # Served from the root so it can control the whole app; never cached so updates land right away.
    return FileResponse(
        STATIC / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


def _real_date(value: str) -> str:
    date.fromisoformat(value)  # 2026-02-30 matches the pattern but would break every page that reads it
    return value


IsoDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_real_date)]

# Wrong passwords in a row before logins are refused for a while (the app is only reachable from
# your own devices, so this is about a stolen or shared device guessing, not the open internet).
WRONG_PASSWORD_DELAY_S = 1
MAX_LOGIN_FAILURES = 5
LOGIN_LOCKOUT_S = 60
_login = {"failures": 0, "locked_until": 0.0}


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    if not auth.enabled():
        return {"ok": True}
    if time.monotonic() < _login["locked_until"]:
        raise HTTPException(429, "Too many wrong passwords. Wait a minute and try again.")
    # In a thread: the hash is deliberately slow and would otherwise hold up every other request.
    if not await asyncio.to_thread(auth.check_password, body.password):
        _login["failures"] += 1
        if _login["failures"] >= MAX_LOGIN_FAILURES:
            _login.update(failures=0, locked_until=time.monotonic() + LOGIN_LOCKOUT_S)
        await asyncio.sleep(WRONG_PASSWORD_DELAY_S)  # slow down guessing
        raise HTTPException(401, "Wrong password")
    _login["failures"] = 0
    conn = db.connect()
    try:
        token = auth.new_session(conn)
    finally:
        conn.close()
    res = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    res.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=https)
    return res


@app.post("/api/logout")
def logout(request: Request):
    conn = db.connect()
    try:
        auth.end_session(conn, request.cookies.get(auth.COOKIE))
    finally:
        conn.close()
    res = JSONResponse({"ok": True})
    res.delete_cookie(auth.COOKIE)
    return res


STATUS_SYNC_WAIT_S = 120
_sync_state = {"running": False, "message": None}
_sync_lock = threading.Lock()


@app.get("/")
def index():
    # First run: nothing is connected yet, so start at the page where you choose Garmin or Strava and sign in.
    if not config.DEMO and not source.current():
        return RedirectResponse("/setup", status_code=303)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ---------- setup: choose Garmin or Strava and sign in, all in the browser ----------
@app.get("/setup")
def setup_page():
    return FileResponse(STATIC / "setup.html", headers={"Cache-Control": "no-cache"})


def _setup_state() -> dict:
    conn = db.connect()
    try:
        activities = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    finally:
        conn.close()
    return {
        "source": source.info(),
        "connected": source.connected(),
        "sources": {k: {"label": v["label"]} for k, v in source.SOURCES.items()},
        "activities": activities,
        "demo": config.DEMO,
        "coach_ready": coach.available(),
        "syncing": _sync_state["running"],
    }


@app.get("/api/setup")
def setup_state():
    return _setup_state()


def _allow_source(key: str) -> None:
    """One source or the other: connecting the second one is refused until the first is disconnected."""
    if config.DEMO:
        raise HTTPException(400, "This is the demo. Start the app without --demo to connect your own account.")
    now = source.current()
    if now and now != key:
        raise HTTPException(409, f"This app is connected to {source.SOURCES[now]['label']}. Disconnect it first to use {source.SOURCES[key]['label']}.")


def _start_sync(full: bool = False) -> dict:
    with _sync_lock:
        if not _sync_state["running"]:
            _sync_state.update(running=True, message="Starting sync…")
            threading.Thread(target=_sync_worker, args=(full,), daemon=True).start()
    return _sync_state


class GarminLoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class MfaIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


@app.post("/api/setup/garmin")
def setup_garmin(body: GarminLoginIn):
    """Sign in to Garmin. The password goes to Garmin and nowhere else; only the session it issues is saved."""
    _allow_source("garmin")
    try:
        done = sync.web_login(body.email, body.password)
    except sync.NotLoggedIn as e:
        raise HTTPException(401, str(e)) from e
    if done:
        _start_sync()
    return {"connected": done, "mfa": not done}


@app.post("/api/setup/garmin/mfa")
def setup_garmin_mfa(body: MfaIn):
    _allow_source("garmin")
    try:
        sync.web_login_mfa(body.code)
    except sync.NotLoggedIn as e:
        raise HTTPException(401, str(e)) from e
    _start_sync()
    return {"connected": True}


class StravaAppIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=20)
    client_secret: str = Field(min_length=1, max_length=100)


def _own_address(request: Request) -> str:
    """This app's address as the browser sees it (behind Tailscale that's the https name, not 127.0.0.1)."""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


@app.post("/api/setup/strava")
def setup_strava(body: StravaAppIn, request: Request):
    """Take your Strava app's keys and answer with the Strava page where you approve access."""
    _allow_source("strava")
    try:
        url = strava.authorize_url(body.client_id, body.client_secret, f"{_own_address(request)}/setup/strava/callback")
    except strava.StravaError as e:
        raise HTTPException(400, str(e)) from e
    return {"authorize_url": url}


@app.get("/setup/strava/callback")
def setup_strava_callback(state: str = "", code: str = "", scope: str = "", error: str = ""):
    """Strava sends the browser back here after you approve (or refuse)."""
    from urllib.parse import quote

    try:
        _allow_source("strava")
        if error or not code:
            raise strava.StravaError("Strava access wasn't approved, so nothing was connected.")
        strava.finish_sign_in(state, code, scope)
        source.choose("strava")
    except (strava.StravaError, strava.NotConnected, strava.RateLimited, source.SourceError, HTTPException) as e:
        return RedirectResponse(f"/setup?error={quote(str(getattr(e, 'detail', e)))}", status_code=303)
    _start_sync()
    return RedirectResponse("/", status_code=303)


@app.post("/api/source/disconnect")
def disconnect_source():
    """Forget the connected service and remove what was downloaded from it (plan, races, goals and notes stay)."""
    if config.DEMO:
        raise HTTPException(400, "Nothing is connected in the demo.")
    if _sync_state["running"]:
        raise HTTPException(409, "A sync is running. Wait for it to finish, then try again.")
    conn = db.connect()
    try:
        source.disconnect(conn)
    finally:
        conn.close()
    _sync_state.update(running=False, message=None)
    return _setup_state()


@app.get("/api/dashboard")
def dashboard(weeks: int = 26):
    conn = db.connect()
    try:
        return {
            "demo": config.DEMO,
            "auth": auth.enabled(),
            "source": source.info(),
            "overview": analysis.overview(conn),
            "weekly": analysis.weekly(conn, weeks),
            "monthly": analysis.monthly(conn, 12),
            "efficiency": analysis.efficiency(conn),
            "records": analysis.personal_records(conn),
            "predictions": analysis.predictions(conn),
            "goals": analysis.goal_progress(conn),
            "activities": analysis.recent_activities(conn),
        }
    finally:
        conn.close()


class GoalIn(BaseModel):
    kind: str = Field(pattern="^(race|weekly_distance|monthly_distance|easy_hr_share)$")
    label: str | None = None
    distance_m: float | None = Field(default=None, gt=0)
    target: float = Field(gt=0)
    target_date: IsoDate | None = None


@app.post("/api/goals")
def add_goal(goal: GoalIn):
    if goal.kind == "race" and not goal.distance_m:
        raise HTTPException(400, "Race goals need a distance")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO goals (kind, label, distance_m, target, target_date) VALUES (?, ?, ?, ?, ?)",
            (goal.kind, goal.label, goal.distance_m, goal.target, goal.target_date or None),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _sync_worker(full: bool) -> str | None:
    """Run a sync. Returns what went wrong, or None when it worked."""
    def progress(msg):
        _sync_state["message"] = msg

    conn = error = None
    try:
        conn = db.connect()
        if source.current() == "strava":
            message = strava.run_sync(conn, strava.Client(), full=full, progress=progress)
        elif source.current() == "garmin":
            message = sync.run_sync(conn, sync.login(interactive=False), full=full, progress=progress)
        else:
            raise sync.NotLoggedIn("Nothing is connected yet. Open Settings to connect Garmin or Strava.")
        if message.startswith(sync.FAILED):
            error = message
    except (sync.NotLoggedIn, sync.GarminUnreachable, strava.NotConnected) as e:
        error = str(e)
    except Exception as e:
        error = f"{sync.FAILED}: {e}"
    finally:
        if conn:
            conn.close()
        if error:
            _sync_state["message"] = error
        _sync_state["running"] = False
    return error


@app.post("/api/sync")
def start_sync(full: bool = False):
    if config.DEMO:
        return {"running": False, "message": "Syncing is disabled while viewing demo data."}
    return _start_sync(full)


@app.get("/api/sync")
def sync_status():
    return _sync_state


class CoachIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    units: str = "km"


@app.get("/api/coach")
def coach_history():
    conn = db.connect()
    try:
        return {"messages": coach.history(conn)}
    finally:
        conn.close()


@app.post("/api/coach")
def coach_ask(body: CoachIn):
    conn = db.connect()
    try:
        return coach.ask(conn, body.message.strip(), body.units)
    except coach.CoachError as e:
        raise HTTPException(503, str(e)) from e
    finally:
        conn.close()


@app.delete("/api/coach")
def coach_reset():
    conn = db.connect()
    try:
        coach.reset(conn)
    finally:
        conn.close()
    return {"ok": True}


class NotesIn(BaseModel):
    notes: str = Field(max_length=8000)


@app.get("/api/coach/notes")
def coach_notes():
    conn = db.connect()
    try:
        return {"notes": coach.get_notes(conn)}
    finally:
        conn.close()


@app.put("/api/coach/notes")
def save_coach_notes(body: NotesIn):
    conn = db.connect()
    try:
        coach.set_notes(conn, body.notes)
    finally:
        conn.close()
    return {"ok": True}


class SessionIn(BaseModel):
    date: IsoDate
    kind: str = Field(pattern="^(easy|long|threshold|intervals|race|rest|cross|strength)$")
    title: str = Field(max_length=300)
    details: str = Field(default="", max_length=2000)


class PlanIn(BaseModel):
    notes: str = Field(default="", max_length=20000)
    sessions: list[SessionIn] = Field(max_length=500)


@app.get("/api/plan")
def get_plan():
    conn = db.connect()
    try:
        return plan.with_actuals(conn)
    finally:
        conn.close()


@app.put("/api/plan")
def save_plan(body: PlanIn):
    conn = db.connect()
    try:
        plan.save(conn, body.notes, [s.model_dump() for s in body.sessions])
        return plan.with_actuals(conn)
    finally:
        conn.close()


# ---------- strength plan ----------
class StrengthIn(BaseModel):
    per_week: int = Field(ge=1, le=strength.MAX_PER_WEEK)
    equipment: str = Field(pattern="^(bodyweight|dumbbells|gym)$")
    start: IsoDate


@app.put("/api/strength")
def set_up_strength(body: StrengthIn):
    """Save the settings and (re)place the upcoming strength sessions around the runs."""
    conn = db.connect()
    try:
        strength.set_up(conn, body.per_week, body.equipment, body.start)
        return plan.with_actuals(conn)
    finally:
        conn.close()


@app.post("/api/strength/refit")
def refit_strength():
    conn = db.connect()
    try:
        strength.refit(conn)
        return plan.with_actuals(conn)
    finally:
        conn.close()


@app.delete("/api/strength")
def remove_strength():
    conn = db.connect()
    try:
        strength.remove(conn)
        return plan.with_actuals(conn)
    finally:
        conn.close()


class StrengthLogIn(BaseModel):
    date: IsoDate
    done: bool


@app.post("/api/strength/log")
def log_strength(body: StrengthLogIn):
    conn = db.connect()
    try:
        strength.log(conn, body.date, body.done)
        return plan.with_actuals(conn)
    finally:
        conn.close()


# ---------- sending planned runs to Garmin ----------
def _garmin_workout(day: str, action) -> dict:
    """Run one step of build / send / remove for a day's workout and answer with the plan."""
    if config.DEMO:
        raise HTTPException(400, "Sending to Garmin is disabled while viewing demo data.")
    if source.current() != "garmin":
        raise HTTPException(400, "Workouts can only be sent to a watch when the app is connected to Garmin.")
    conn = db.connect()
    try:
        action(conn)
        return plan.with_actuals(conn)
    except garmin_workouts.GarminWorkoutError as e:
        raise HTTPException(409, str(e)) from e
    except (sync.NotLoggedIn, sync.GarminUnreachable) as e:
        raise HTTPException(503, str(e)) from e
    finally:
        conn.close()


class _GarminWhenNeeded:
    """Signs in to Garmin on first use, so a request that's refused earlier (nothing built yet) never touches Garmin."""

    def __getattr__(self, name):
        client = self.__dict__.get("_client") or sync.login(interactive=False)
        self.__dict__["_client"] = client
        return getattr(client, name)


@app.post("/api/garmin/workouts/{day}/build")
def build_garmin_workout(day: IsoDate):
    """Convert that day's planned run into watch steps. Nothing is sent yet."""
    return _garmin_workout(day, lambda conn: garmin_workouts.build(conn, day))


@app.post("/api/garmin/workouts/{day}/send")
def send_garmin_workout(day: IsoDate):
    return _garmin_workout(day, lambda conn: garmin_workouts.send(conn, _GarminWhenNeeded(), day))


@app.delete("/api/garmin/workouts/{day}")
def remove_garmin_workout(day: IsoDate, sent: bool = False):
    """sent=true takes it off Garmin as well; otherwise only an unsent draft is dropped."""
    if not sent:
        return _garmin_workout(day, lambda conn: garmin_workouts.discard(conn, day))
    return _garmin_workout(day, lambda conn: garmin_workouts.remove(conn, _GarminWhenNeeded(), day))


@app.get("/api/status")
def get_status():
    conn = db.connect()
    try:
        return status.latest(conn)
    finally:
        conn.close()


@app.post("/api/status")
def refresh_status(units: str = "km", sync_first: bool = True):
    # Pull the latest from Garmin first so "last 12 hours" is current, unless a sync is already running.
    sync_error = None
    if sync_first and not config.DEMO:
        with _sync_lock:
            start = not _sync_state["running"]
            if start:
                _sync_state.update(running=True, message="Syncing for status…")
        if start:
            # Wait for the sync, but not forever (a long history or Garmin's rate limit can take many
            # minutes): past the limit it carries on in the background and the status uses what's saved.
            result = {}
            worker = threading.Thread(target=lambda: result.update(error=_sync_worker(False)), daemon=True)
            worker.start()
            worker.join(STATUS_SYNC_WAIT_S)
            sync_error = (
                "The sync is taking a while, so this status uses the data saved so far."
                if worker.is_alive() else result.get("error")
            )
    conn = db.connect()
    try:
        status.generate(conn, units)
        # sync_error: the status was written from the data already saved, which may be out of date.
        return {**status.latest(conn), "sync_error": sync_error}
    except status.StatusError as e:
        raise HTTPException(503, str(e)) from e
    finally:
        conn.close()


class ZonesIn(BaseModel):
    max_hr: int = Field(ge=140, le=230)


@app.put("/api/zones")
def save_zones(body: ZonesIn):
    conn = db.connect()
    try:
        zones.set_max_hr(conn, body.max_hr)
        return zones.describe(conn)
    finally:
        conn.close()


# ---------- race log ----------
class RaceIn(BaseModel):
    date: IsoDate
    name: str = Field(min_length=1, max_length=200)
    distance_m: float = Field(gt=0, le=200000)
    course: str | None = Field(default=None, max_length=200)
    official_time_s: float | None = Field(default=None, gt=0)
    target_time_s: float | None = Field(default=None, gt=0)
    effort: str = Field(default="all_out", pattern="^(all_out|controlled|easy)$")
    activity_id: int | None = None
    notes: str | None = Field(default=None, max_length=2000)


@app.get("/api/races")
def list_races():
    conn = db.connect()
    try:
        return races.all_races(conn)
    finally:
        conn.close()


@app.post("/api/races")
def add_race(body: RaceIn):
    conn = db.connect()
    try:
        races.save(conn, body.model_dump())
        return races.all_races(conn)
    finally:
        conn.close()


@app.put("/api/races/{race_id}")
def update_race(race_id: int, body: RaceIn):
    conn = db.connect()
    try:
        races.save(conn, body.model_dump(), race_id)
        return races.all_races(conn)
    finally:
        conn.close()


@app.delete("/api/races/{race_id}")
def remove_race(race_id: int):
    conn = db.connect()
    try:
        races.delete(conn, race_id)
        return races.all_races(conn)
    finally:
        conn.close()


# ---------- threshold / interval sessions ----------
@app.get("/api/workouts")
def list_workouts(days: int = 365):
    conn = db.connect()
    try:
        return {"sessions": workouts.sessions(conn, days), "zones": zones.describe(conn), "reference_hr": workouts.REFERENCE_HR}
    finally:
        conn.close()


# ---------- coach plan proposals ----------
@app.get("/api/plan/proposals")
def plan_proposals():
    conn = db.connect()
    try:
        return {"proposals": plan.proposals(conn)}
    finally:
        conn.close()


@app.post("/api/plan/proposals/{proposal_id}/apply")
def apply_plan_proposal(proposal_id: int):
    conn = db.connect()
    try:
        plan.apply_proposal(conn, proposal_id)
        return plan.with_actuals(conn)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    finally:
        conn.close()


@app.post("/api/plan/proposals/{proposal_id}/dismiss")
def dismiss_plan_proposal(proposal_id: int):
    conn = db.connect()
    try:
        plan.dismiss_proposal(conn, proposal_id)
        return {"ok": True}
    finally:
        conn.close()


# ---------- running form ----------
@app.get("/api/form")
def form_report():
    conn = db.connect()
    try:
        return form.report(conn)
    finally:
        conn.close()
