# Running log

Copies your runs from Garmin Connect or Strava to your own computer and analyzes them for running goals:
weekly and monthly volume, training load, time in heart-rate zones, aerobic efficiency, personal records
and race-time predictions, with a training plan and an AI coach.

## Quick start (no terminal needed)

1. **Download** this project: the green **Code** button on GitHub, then **Download ZIP**. Unzip it and put the
   folder somewhere permanent, like Documents.
2. **Double-click `Start Running Log`** (`.command` on a Mac, `.bat` on Windows).
   - Mac, first time only: macOS blocks files downloaded from the internet. Right-click the file, choose **Open**,
     then **Open** again. (On recent macOS: System Settings → Privacy & Security → **Open Anyway**.)
   - If Python isn't installed, a download page opens. Install it (3.11, 3.12 or 3.13) and double-click again.
   - The first start sets itself up, which takes a few minutes and needs the internet.
3. **Your browser opens the app.** Choose Garmin or Strava and sign in on that page. The first sync downloads
   your history; the page shows its progress.

Leave the window that opened running while you use the app; closing it stops the app. Double-click the file again
whenever you want to start it. Your data lives in the `data` folder inside the project: to update the app later,
download it again and move your `data` folder into the new copy.

The AI coach, the daily status and sending workouts to a watch also need [Claude Code](https://claude.com/claude-code)
installed and signed in on the same computer. Everything else works without it.

The Windows launcher is written but has not been tried on a Windows machine yet.

## Garmin or Strava

The app reads from one service at a time, chosen on the setup page (**Settings → Connection** later on). To change,
disconnect there first: the downloaded runs are removed and fetched again from the other service, while your plan,
races, goals and notes stay.

| | Garmin | Strava |
|---|---|---|
| Runs with pace, heart rate, laps, zones, best efforts, workouts | yes | yes |
| Sleep, HRV, readiness, Body Battery, VO₂ max | yes | no |
| Running form (ground contact, vertical ratio, balance) | yes | cadence and stride only |
| Weather and heat adjustment | yes | no |
| Send planned workouts to the watch | yes | no |
| Sign-in | Garmin email and password (plus the emailed code), on the setup page | your own free Strava API app (the setup page walks you through it) |

- **Garmin** uses an unofficial sign-in. Your password is sent to Garmin and never saved; only the session Garmin
  issues is kept, in `data/garmin_tokens.json`.
- **Strava** uses its official API. Strava lets an unreviewed app read only its owner's account, so each person
  registers their own (strava.com/settings/api, with `localhost` as the Authorization Callback Domain). Its keys and
  tokens are kept in `data/strava_tokens.json`. Strava allows 100 requests per 15 minutes and 1,000 a day, and each
  run costs two, so a long history downloads in stages: the sync waits for the next 15-minute window by itself and
  picks up the rest the next day. On Strava, a run's time is its moving time (a race's is start to finish).
  Read Strava's API agreement before relying on the AI coach with Strava data.

## Setup from a terminal

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

```sh
.venv/bin/python -m runapp serve --open # starts the app and opens it; sign in on the setup page
.venv/bin/python -m runapp login        # or sign in to Garmin here: email, password, MFA code (once)
.venv/bin/python -m runapp sync         # download everything new (first run fetches full history)
.venv/bin/python -m runapp serve        # dashboard at http://localhost:8000
```

To try the dashboard without a Garmin account: `.venv/bin/python -m runapp demo`, then `.venv/bin/python -m runapp serve --demo`.

(`python` on its own won't work unless you first run `source .venv/bin/activate`.)

The dashboard's **Sync Garmin** button runs the same incremental sync. `sync --full` re-checks the whole
history (e.g. after editing old activities in Garmin Connect).

Other commands: `reparse` re-reads every saved FIT file into the database without downloading anything (after an
upgrade that stores more fields), and `query "SELECT ..."` runs a read-only SQL query.

The app only answers to `localhost`, `127.0.0.1` and Tailscale (`*.ts.net`) names, and only lets its own page make
changes, so another website open in your browser can't read your data or start a sync. To reach it under another
name, start it with `RUNAPP_ALLOWED_HOSTS=name1,name2`.

## Finding your way around

The app is designed for the phone first; on a computer the same pages sit side by side with the coach in its own column.

- **Today**: today's sessions from your plan (with the coach's notes on them), how your body is doing, the last 12
  hours, then this week's numbers, the weekly chart and your goals.
- **Plan**: the next seven days at a glance (tap a day to jump to it), then the schedule week by week. Weeks already
  behind you fold away under **Earlier weeks**. On a phone, **Strength** and **Notes** are their own pages.
- **Coach** (a tab on phones, always visible on a computer).
- **Progress**: **Training** (zones, efficiency, monthly distance), **Workouts** (threshold and interval sessions;
  on a phone a session opens as its own page) and **Form**.
- **History**: **Activities** and **Races & records**.

The ↻ button syncs Garmin; the sliders button holds the settings (km or miles, light or dark appearance, install, log out).

## Use it on your phone (Tailscale)

The app runs on your Mac; your phone opens it over [Tailscale](https://tailscale.com), a private network
between your own devices. Nothing is exposed to the internet and the app itself still only listens on the Mac.

1. Install Tailscale on the Mac and on your phone, and sign in to the same account on both.
2. Set a password for the app: `.venv/bin/python -m runapp password`
3. Start the app and keep the Mac awake: `.venv/bin/python -m runapp serve --keep-awake`
4. Share it with your devices over HTTPS: `tailscale serve --bg 8000`
   (with the Mac App Store version of Tailscale the command is
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8000`). It prints an address like
   `https://your-mac.your-tailnet.ts.net`. The first time, it may give you a link to enable HTTPS for your account.
5. Open that address on your phone, log in, then add it to your home screen
   (iPhone Safari: Share → Add to Home Screen; Android Chrome: ⋮ → Install app).

It's a progressive web app (PWA): on Android and desktop Chrome/Edge an **Install app** button appears in the
header, and long-pressing the installed icon offers shortcuts to the Plan, Coach and Training views. A service
worker (`runapp/static/sw.js`) keeps a copy of the app and your last-loaded data on the device, so it still opens
with an "Offline" banner when the Mac is asleep. Logging out deletes that saved data from the device.

The Mac has to be on, awake and running the app for fresh data, syncing, the coach and saving changes. `--keep-awake` stops it sleeping while plugged in, but closing the
lid usually still sleeps it. To stop sharing: `tailscale serve --https=443 off`. To remove the password:
`.venv/bin/python -m runapp password --remove`.

## What gets saved

Everything lives in `data/` (gitignored):

| Path | Contents |
|---|---|
| `data/garmin_tokens.json` | Garmin session tokens (your password is never stored). Delete it to log out. |
| `data/auth.json` | The app password, as a salted scrypt hash (only if you set one). |
| `data/raw/activities/<id>/` | Untouched Garmin responses: `summary.json`, `splits.json`, `hr_zones.json`, and `original.zip` (the FIT file from your watch) |
| `data/raw/snapshots/` | Daily copies of Garmin's race predictions and personal records |
| `data/runapp.db` | SQLite database. Garmin data: `activities`, per-second `samples`, `laps`, `best_efforts`, `activity_weather`, `wellness`, `vo2max_daily`, `snapshots`. Yours: `goals`, `races`, `plan_sessions`, `plan_proposals`, `strength_log`, `garmin_workouts`, `coach_messages`, `coach_state` (notes, max HR, strength settings). Derived: `form_stats`, `status_reports`. Housekeeping: `sync_log`, `auth_sessions`. The full schema is in `runapp/db.py`. |

All activity types are saved; only runs (any Garmin type containing "run") feed the analysis. The raw files
are the source of truth, so the database can be rebuilt if the analysis changes.

Query the database directly for your own analysis, for example in pandas:

```python
import sqlite3, pandas as pd
df = pd.read_sql("SELECT * FROM activities WHERE is_run = 1", sqlite3.connect("data/runapp.db"))
```

## AI coach

The Coach panel on the dashboard answers questions about your training ("Am I on track for my race?",
"Plan my next 4 weeks"). It runs the [Claude Code](https://claude.com/claude-code) CLI on this machine
(`claude -p`), so it uses your Claude login and subscription. No API key is needed. Requirements: `claude` is
installed and you've logged in once by running `claude` in a terminal.

- **Locked down:** the coach has no shell, file or web access. Its only tools are a training summary and
  read-only SQL against `data/runapp.db` (`runapp/coach_tools.py`), so it can't read your Garmin tokens or
  change anything.
- **Leaves your data only when you ask:** your questions, and the data the coach looks up to answer them, are sent to Anthropic.
- **Speed:** a first answer takes about 20–30 seconds. Follow-ups are faster. Replies count toward your Claude
  usage limits.
- **Conversations:** saved in the database. **New conversation** clears the current one.

## Workouts, races, strength and plan changes

- **Workouts**: every threshold and interval session split into reps (from your laps: 45+ seconds and within 15%
  of the session's fastest lap). Each rep is judged by its average HR: ✓ zone 3 or below is controlled sub-threshold
  work, ! zone 4 is running hot, ✕ zone 5 is too hard. "Pace at 160 bpm" scales each rep's speed by 160/HR so sessions
  can be compared over time; short reps (under 2½ min) trend separately because heart rate lags behind on them.
- **Races** (Records view): official times override watch times; races run easy don't count for PRs or predictions.
  A race is linked to that day's run of about the race distance (80–130% of it), when you save it or, for a race
  entered ahead of time, on the first sync after you've run it.
  Race-goal projections use official all-out results from the last 12 months alongside recent training efforts.
  Each upcoming race has a printable mile-split **pacing card**, which warns when the course measured long on your
  watch last time.
- **Form** (running dynamics): cadence, ground contact time, vertical ratio and stride are compared at matched
  paces, because form changes with pace. Four pace bands are trended by month (a month needs two runs in a band);
  the cadence curve compares the last 90 days with the year before. "Form under fatigue" compares the start and end
  of each run (first vs last rep for workouts), each against your own norm for the pace, and flags contact +10 ms,
  cadence −3 or vertical ratio +0.4. Left/right balance counts only seconds actually running, and alerts beyond 2%
  from even. After upgrading, run `python -m runapp reparse` once to load form data from the saved FIT files.
- **Weather**: each sync fetches the conditions Garmin recorded for outdoor runs (treadmill runs are skipped). Heat
  cost uses the runners' temperature + dew point rule (°F): 100 or less costs nothing, 120 about 1%, 140 about 3%,
  160 about 6%, 180 about 10%. "Heat-adjusted" numbers (efficiency, pace at 160 bpm, race times, race-based
  predictions) remove that cost, so summer and winter runs compare fairly. Wind isn't adjusted for, since it depends
  on the route. Runs whose weather report has no dew point or humidity are left unadjusted.
- **Strength** (Plan view): choose sessions a week, your equipment and a start date, and the app writes strength
  sessions into the plan next to your runs. Two routines alternate (A: legs and calves, B: hips and hamstrings) and
  progress in three phases: Intro (weeks 1-3, 2 easy sets), Build (weeks 4-7, 3 sets), Strong (week 8 on, heavier).
  Placement: after a threshold or interval run or on an easy day; never the day before a hard or long run, on a
  rest day, or two days in a row; before an all-out race only light sessions in the last week and nothing in the
  last 3 days or the 2 days after (2 days before and 1 after any other race). Past the end of your running plan it
  follows the weekdays your hard runs usually fall on. When the runs move, the card lists the sessions that no
  longer fit and **Refit to runs** re-places the upcoming ones; past sessions are never touched. Tick a session
  off with **Mark done**; a strength activity recorded on the watch counts by itself.
- **Send to Garmin** (Plan view): each upcoming run has a **Send to Garmin…** button. It converts the session text
  into watch steps (reps, recoveries, heart-rate or pace targets) and shows them first; **Send to Garmin** then puts
  the workout in your Garmin Connect library and on your Garmin calendar for that day, and the watch gets it on its
  next sync. Warm-ups and cool-downs always end when you press Lap, never on a timer; the planned duration is kept
  as the step's note. Heart-rate targets are sent as bpm ranges and pace targets per mile. **Remove from Garmin**
  deletes it there again. If you edit the session afterwards, the app says the watch has the old version. Strength
  sessions, rest days and races aren't sent.
- **Plan changes from the coach**: ask the coach to change your plan and it proposes dated changes. Nothing changes
  until you click **Apply** (in the chat or on the Plan view). Runs and strength change separately: moving a run
  leaves the strength session beside it.

## How the numbers are computed

- **Best efforts**: the fastest stretch covering each distance inside any run, searched in the
  distance stream from the FIT file. Records and predictions count outdoor runs only, because a treadmill's
  distance is an estimate; indoor efforts are still saved in `best_efforts`.
- **Race predictions**: Riegel's formula (`T2 = T1 × (D2/D1)^1.06`) from your best outdoor effort of 3k or longer
  in the last 90 days, shown next to Garmin's own predictor.
- **Acute:chronic load**: running distance in the last 7 days divided by the 4-week weekly average.
  Values well above 1.5 mean volume is rising quickly.
- **Aerobic efficiency**: average speed (m/min) divided by average heart rate, for runs of 20+ minutes. The trend line
  uses easy runs (≥70% of time in HR zones 1–2) only.
- **HR zones** are the Norwegian (Olympiatoppen) zones, as percentages of your max heart rate (set it on the
  Training view; 190 until you do): zone 1 from 60%, zone 2 from 72%, zone 3 from 82%, zone 4 from 87%, zone 5 from
  92%. Time in each zone is counted from your second-by-second heart rate; runs with no heart-rate stream fall back
  to Garmin's own zone times.

## Caveats

Garmin's official API only approves businesses, so this uses the unofficial
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, which logs in like the Garmin Connect
website does. It can break when Garmin changes its login. If that happens, upgrade it
(`pip install -U garminconnect`). The first full sync downloads every activity with a short pause between requests,
so a long history can take a while. If Garmin rate-limits the sync, it backs off and retries.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`requirements.lock` lists the exact versions this was last tested with; install from it
(`.venv/bin/pip install -r requirements.lock`) if a newer release of a dependency breaks something.
