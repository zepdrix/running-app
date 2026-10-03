# Running log

Copies all of your Garmin Connect activities to your own machine and analyzes them for running goals:
weekly and monthly volume, training load, time in heart-rate zones, aerobic efficiency, personal records
and race-time predictions.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Use

```sh
.venv/bin/python -m runapp login        # Garmin email, password, MFA code (once)
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
| `data/runapp.db` | SQLite database. Garmin data: `activities`, per-second `samples`, `laps`, `best_efforts`, `activity_weather`, `wellness`, `vo2max_daily`, `snapshots`. Yours: `goals`, `races`, `plan_sessions`, `plan_proposals`, `coach_messages`, `coach_state` (notes, max HR). Derived: `form_stats`, `status_reports`. Housekeeping: `sync_log`, `auth_sessions`. The full schema is in `runapp/db.py`. |

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

## Workouts, races and plan changes

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
- **Plan changes from the coach**: ask the coach to change your plan and it proposes dated changes. Nothing changes
  until you click **Apply** (in the chat or on the Plan view).

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
