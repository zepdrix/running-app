from datetime import date, timedelta

import pytest

from runapp import analysis, config, db, store
from runapp.streams import Sample, best_efforts, samples_from_details


def steady_run(km: float, pace_s_per_km: float, step_s: float = 1.0) -> list[Sample]:
    speed = 1000 / pace_s_per_km
    n = int(km * 1000 / speed / step_s) + 1
    return [Sample(t=i * step_s, distance_m=i * step_s * speed) for i in range(n)]


def test_best_efforts_steady_pace():
    efforts = best_efforts(steady_run(10.2, 300), {"5k": 5000, "10k": 10000, "Half": 21097.5})
    assert efforts["5k"] == pytest.approx(1500, abs=1)
    assert efforts["10k"] == pytest.approx(3000, abs=1)
    assert "Half" not in efforts  # run too short


def test_best_efforts_finds_fast_segment_mid_run():
    # 2 km easy at 6:00/km, 1 km hard at 4:00/km, 2 km easy again.
    samples, t, d = [], 0.0, 0.0
    for km_pace in [360, 360, 240, 360, 360]:
        for _ in range(int(km_pace)):
            samples.append(Sample(t=t, distance_m=d))
            t += 1
            d += 1000 / km_pace
    samples.append(Sample(t=t, distance_m=d))
    assert best_efforts(samples, {"1k": 1000})["1k"] == pytest.approx(240, abs=1)


def test_best_efforts_ignores_distance_glitches():
    samples = steady_run(2, 300)
    samples.insert(100, Sample(t=99.5, distance_m=0))  # distance jumps backwards
    assert best_efforts(samples, {"1k": 1000})["1k"] == pytest.approx(300, abs=1)


def test_samples_from_details():
    details = {
        "metricDescriptors": [
            {"key": "sumDistance", "metricsIndex": 0},
            {"key": "directTimestamp", "metricsIndex": 1},
            {"key": "directHeartRate", "metricsIndex": 2},
        ],
        "activityDetailMetrics": [
            {"metrics": [0.0, 1_700_000_000_000, 120]},
            {"metrics": [10.0, 1_700_000_005_000, 125]},
        ],
    }
    s = samples_from_details(details)
    assert [(x.t, x.distance_m, x.hr) for x in s] == [(0, 0.0, 120), (5, 10.0, 125)]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def add_run(conn, activity_id, start, km, pace=300, type_key="running", zones=(0, 1800, 300, 0, 0)):
    summary = {
        "activityId": activity_id,
        "activityName": f"Run {activity_id}",
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start,
        "distance": km * 1000,
        "duration": km * pace,
        "averageSpeed": 1000 / pace,
        "averageHR": 140,
        **{f"hrTimeInZone_{i + 1}": z for i, z in enumerate(zones)},
    }
    store.upsert_summary(conn, summary)
    store.store_detail(conn, activity_id, steady_run(km, pace, 5), [])


def test_summary_upsert_keeps_detail_flags(conn):
    add_run(conn, 1, "2026-09-01 07:00:00", 5)
    store.upsert_summary(conn, {"activityId": 1, "activityName": "Renamed", "activityType": {"typeKey": "running"},
                                "startTimeLocal": "2026-09-01 07:00:00"})
    row = conn.execute("SELECT name, detail_synced, has_samples FROM activities WHERE id = 1").fetchone()
    assert tuple(row) == ("Renamed", 1, 1)


def test_non_runs_are_stored_but_not_analyzed(conn):
    add_run(conn, 1, "2026-09-29 07:00:00", 5)
    add_run(conn, 2, "2026-09-30 07:00:00", 30, type_key="road_biking")
    weeks = analysis.weekly(conn, 1, today=date(2026, 10, 2))
    assert weeks[0]["runs"] == 1
    assert weeks[0]["distance_m"] == pytest.approx(5000)
    assert conn.execute("SELECT COUNT(*) FROM best_efforts WHERE activity_id = 2").fetchone()[0] == 0


def test_weekly_buckets_monday_start(conn):
    add_run(conn, 1, "2026-09-27 07:00:00", 10)  # Sunday -> week of Sep 21
    add_run(conn, 2, "2026-09-28 07:00:00", 5)  # Monday -> week of Sep 28
    weeks = analysis.weekly(conn, 2, today=date(2026, 10, 2))
    assert [(w["week"], w["distance_m"]) for w in weeks] == [("2026-09-21", 10000), ("2026-09-28", 5000)]


def test_race_goal_projection(conn):
    add_run(conn, 1, "2026-09-20 07:00:00", 10.1, pace=300)  # 10k in 50:00
    conn.execute("INSERT INTO goals (kind, distance_m, target) VALUES ('race', 21097.5, 6700)")
    (goal,) = analysis.goal_progress(conn, today=date(2026, 10, 2))
    expected = analysis.riegel(3000, 10000, 21097.5)
    assert goal["current"] == pytest.approx(expected, rel=0.01)
    assert goal["on_track"] is True


def test_goals_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    assert client.post("/api/goals", json={"kind": "weekly_distance", "target": 40000}).status_code == 200
    assert client.post("/api/goals", json={"kind": "race", "target": 3600}).status_code == 400
    assert client.post("/api/goals", json={"kind": "bogus", "target": 1}).status_code == 422
    goals = client.get("/api/dashboard").json()["goals"]
    assert [g["kind"] for g in goals] == ["weekly_distance"]
    assert client.delete(f"/api/goals/{goals[0]['id']}").status_code == 200
    assert client.get("/api/dashboard").json()["goals"] == []


class FakeGarmin:
    """Stands in for garminconnect.Garmin; serves N activities newest-first."""

    def __init__(self, n):
        self.activities = [
            {"activityId": 1000 + i, "activityName": f"Run {i}", "activityType": {"typeKey": "running"},
             "startTimeLocal": f"2026-09-{1 + i % 28:02d} 07:00:00", "distance": 5000, "duration": 1500}
            for i in reversed(range(n))
        ]
        self.detail_calls = 0

    def get_activities(self, start, limit):
        return self.activities[start : start + limit]

    def download_activity(self, activity_id, fmt):
        return b""  # no FIT file -> falls back to details

    def get_activity_splits(self, activity_id):
        return {"lapDTOs": []}

    def get_activity_details(self, activity_id):
        self.detail_calls += 1
        return {
            "metricDescriptors": [{"key": "directTimestamp", "metricsIndex": 0}, {"key": "sumDistance", "metricsIndex": 1}],
            "activityDetailMetrics": [{"metrics": [i * 1000, i * 3.33]} for i in range(1600)],
        }

    def get_activity_hr_in_timezones(self, activity_id):
        return [{"zoneNumber": 2, "secsInZone": 1200}]

    def get_race_predictions(self):
        return {"time5K": 1400}

    def get_personal_record(self):
        return []

    garmin_connect_metrics_url = "/metrics"

    def connectapi(self, path):
        assert path.startswith("/metrics/")
        return [
            {"generic": {"calendarDate": "2026-09-01", "vo2MaxPreciseValue": 49.6, "vo2MaxValue": 50.0}},
            {"generic": {"calendarDate": "2026-09-20", "vo2MaxPreciseValue": 50.2, "vo2MaxValue": 50.0}},
        ]


def test_sync_incremental(conn, tmp_path, monkeypatch):
    from runapp import sync

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sync, "REQUEST_PAUSE_S", 0)
    monkeypatch.setattr(sync, "PAGE_SIZE", 10)
    fake = FakeGarmin(25)
    sync.run_sync(conn, fake, progress=lambda m: None)
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE detail_synced = 1").fetchone()[0] == 25
    assert conn.execute("SELECT hr_zone_2_s FROM activities WHERE id = 1000").fetchone()[0] == 1200
    assert conn.execute("SELECT COUNT(*) FROM best_efforts WHERE label = '5k'").fetchone()[0] == 25
    assert (tmp_path / "raw" / "activities" / "1000" / "summary.json").exists()
    assert analysis.predictions(conn)[0]["garmin_s"] == 1400
    assert analysis.overview(conn)["vo2max"] == {"vo2max": 50.2, "start_local": "2026-09-20"}
    assert conn.execute("SELECT status FROM sync_log ORDER BY id DESC").fetchone()[0] == "ok"

    # Two new activities: only they get detail requests, and paging stops early.
    fake.activities[:0] = FakeGarmin(27).activities[:2]
    fake.detail_calls = 0
    sync.run_sync(conn, fake, progress=lambda m: None)
    assert fake.detail_calls == 2
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 27


def add_wellness(conn, day, kind, data):
    conn.execute("INSERT INTO wellness VALUES (?, ?, ?, datetime('now'))", (day, kind, __import__("json").dumps(data)))


def test_wellness_summary_handles_missing_nights(conn):
    from runapp import wellness

    add_wellness(conn, "2026-10-01", "stats", {"totalSteps": 8814, "restingHeartRate": 55, "bodyBatteryMostRecentValue": 60, "averageStressLevel": 25})
    add_wellness(conn, "2026-10-01", "sleep", {"dailySleepDTO": {"sleepTimeSeconds": 27360, "sleepScores": {"overall": {"value": 88}}}})
    add_wellness(conn, "2026-10-01", "hrv", {"hrvSummary": {"lastNightAvg": 33, "status": "NONE"}})
    add_wellness(conn, "2026-10-02", "sleep", {"dailySleepDTO": {"sleepTimeSeconds": None}})  # watch not worn
    add_wellness(conn, "2026-10-02", "readiness", [
        {"timestamp": "2026-10-02T10:00:00", "score": 40, "level": "MODERATE", "recoveryTime": 600},
        {"timestamp": "2026-10-03T00:27:57", "score": 25, "level": "LOW", "recoveryTime": 2090},
    ])
    s = wellness.summarize(conn, date(2026, 10, 2), days=2)
    today, yesterday = s["days"]
    assert "sleep" not in today
    assert today["readiness"] == {"score": 25, "level": "LOW", "recovery_time_h": 34.8, "as_of": None}
    assert yesterday["sleep"]["hours"] == 7.6 and yesterday["resting_hr"] == 55
    metrics = {m["label"]: (m["value"], m["date"]) for m in wellness.headline_metrics(s)}
    assert metrics["Readiness"] == ("25", "2026-10-02")
    assert metrics["Sleep"] == ("7.6 h", "2026-10-01")  # falls back to the newest night with data
    assert metrics["HRV"] == ("33 ms", "2026-10-01")


def test_status_generate(conn):
    from datetime import datetime

    from runapp import coach, status

    add_run(conn, 1, "2026-10-01 17:05:00", 7.4)  # ended ~17:42, before the 12 h window
    add_run(conn, 2, "2026-10-02 07:00:00", 5)  # this morning
    coach.set_notes(conn, "Training for a 5k PR.")
    from runapp import plan

    plan.save(conn, "", [{"date": "2026-10-03", "kind": "easy", "title": "40 min easy"}])
    seen = {}

    def fake_runner(message, prompt, args, timeout):
        seen["prompt"], seen["args"] = prompt, args
        return {"structured_output": {
            "body": {"level": "low", "headline": "Recovering", "summary": "Readiness 25."},
            "last_12_hours": {"headline": "Easy 5 km", "summary": "..."},
            "next_workout": {"when": "Sat Oct 3", "title": "40 min easy", "details": "HR under 135", "adjustment": ""},
        }}

    status.generate(conn, "km", now=datetime(2026, 10, 2, 9, 0), runner=fake_runner)
    assert "Sat Oct 3 (2026-10-03) [easy]: 40 min easy" in seen["prompt"] and "Training for a 5k PR." in seen["prompt"]
    assert "--json-schema" in seen["args"]
    snap = status.build_snapshot(conn, datetime(2026, 10, 2, 9, 0))
    assert [a["in_last_12_hours"] for a in snap["activities_last_7_days"]] == [False, True]
    latest = status.latest(conn)
    assert latest["report"]["body"]["level"] == "low" and latest["generated_at"] == "2026-10-02 09:00:00"


def test_status_rejects_unstructured_answer(conn):
    from runapp import status

    with pytest.raises(status.StatusError):
        status.generate(conn, runner=lambda *a: {"result": "plain text"})


def test_plan_text_migrates_to_sessions(conn):
    from runapp import coach, plan

    coach._set_state(conn, "training_plan", """Races
- Sun Oct 11: Prospect Park 5k. Controlled tune-up

Week 1
- Fri Oct 2: Rest, or 25-30 min very easy
- Sun Oct 4: 55-60 min easy
- Tue Oct 6: 5x6 min sub-threshold (HR <=160), 1 min jog. 15 min easy before and after
- Thu Oct 8: 4x1 km at 4:25-4:30/km (race-pace check), 2 min jog
- Sat Oct 10: 20 min easy + 4x20 s strides
- Sun Oct 11: Prospect Park 5k. Run controlled at 4:30/km
- Sun Oct 18: Croton-on-Hudson 5k, ALL OUT. 15-20 min warm-up""")
    p = plan.get(conn)
    assert [(s["date"], s["kind"]) for s in p["sessions"]] == [
        (f"{date.today().year}-10-02", "rest"), (f"{date.today().year}-10-04", "easy"),
        (f"{date.today().year}-10-06", "threshold"), (f"{date.today().year}-10-08", "intervals"),
        (f"{date.today().year}-10-10", "easy"), (f"{date.today().year}-10-11", "race"),
        (f"{date.today().year}-10-18", "race"),
    ]
    oct11 = p["sessions"][5]  # listed under Races and in the schedule: merged, both details kept
    assert oct11["title"] == "Prospect Park 5k" and oct11["details"] == "Controlled tune-up. Run controlled at 4:30/km"
    tue = p["sessions"][2]
    assert tue["title"] == "5x6 min sub-threshold (HR <=160), 1 min jog" and tue["details"] == "15 min easy before and after"
    assert p["notes"] == "Races"  # the race list line moved into sessions; "Week 1" heading dropped
    assert coach._get_state(conn, "training_plan") == ""  # superseded


def test_plan_actuals(conn):
    from runapp import plan

    add_run(conn, 1, "2026-10-03 08:00:00", 6)  # easy run on the planned easy day
    add_run(conn, 2, "2026-10-04 08:00:00", 5, zones=(0, 0, 0, 600, 600))  # intervals on an easy day
    plan.save(conn, "Zones...", [
        {"date": "2026-10-02", "kind": "rest", "title": "Rest"},
        {"date": "2026-10-03", "kind": "easy", "title": "40 min easy"},
        {"date": "2026-10-04", "kind": "easy", "title": "50 min easy"},
        {"date": "2026-10-05", "kind": "threshold", "title": "5x6 min"},
        {"date": "2026-10-06", "kind": "easy", "title": "35 min easy"},
        {"date": "2026-10-09", "kind": "easy", "title": "", "details": "blank title is dropped"},
    ])
    p = plan.with_actuals(conn, today=date(2026, 10, 5))
    assert [s["state"] for s in p["sessions"]] == ["rested", "done", "harder", "today", "upcoming"]
    assert p["sessions"][2]["actual"][0]["kind"] == "intervals"
    assert "Sessions:" in plan.as_text(conn) and plan.as_text(conn).startswith("Zones...")


def test_plan_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    empty = client.get("/api/plan").json()
    assert (empty["notes"], empty["sessions"], empty["strength"]["enabled"]) == ("", [], False)
    body = {"notes": " Zones ", "sessions": [{"date": "2026-10-06", "kind": "threshold", "title": "5x6 min", "details": ""}]}
    saved = client.put("/api/plan", json=body).json()
    assert saved["notes"] == "Zones" and saved["sessions"][0]["title"] == "5x6 min"
    bad = {"notes": "", "sessions": [{"date": "Oct 6", "kind": "threshold", "title": "x"}]}
    assert client.put("/api/plan", json=bad).status_code == 422
    assert client.get("/api/status").json()["report"] is None


def test_norwegian_zones_from_samples(conn):
    from runapp import zones

    # 1000 s at 140 bpm (zone 2), then 600 s at 160 bpm (zone 3), with max HR 190.
    samples = [Sample(t=i, distance_m=i * 3.0, hr=140 if i < 1000 else 160) for i in range(1600)]
    store.upsert_summary(conn, {"activityId": 9, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-10-01 07:00:00",
                                "distance": 4800, "duration": 1600, "hrTimeInZone_3": 1600})  # Garmin: all zone 3
    store.store_detail(conn, 9, samples, [])
    row = conn.execute("SELECT * FROM activities WHERE id = 9").fetchone()
    assert [round(x) for x in zones.seconds(row)] == [0, 1000, 600, 0, 0]
    assert analysis.workout_kind(row) == "threshold"  # 10 min in zone 3 = 37% of the run

    zones.set_max_hr(conn, 200)  # zone 2 now starts at 144, so 140 bpm becomes zone 1
    row = conn.execute("SELECT * FROM activities WHERE id = 9").fetchone()
    assert [round(x) for x in zones.seconds(row)] == [1000, 600, 0, 0, 0]
    assert analysis.workout_kind(row) == "easy"
    assert zones.describe(conn)["ranges"][1] == "144-163"


def test_zones_fall_back_to_garmin_without_hr(conn):
    from runapp import zones

    add_run(conn, 1, "2026-10-01 07:00:00", 5, zones=(0, 600, 300, 0, 0))  # samples have no HR
    row = conn.execute("SELECT * FROM activities WHERE id = 1").fetchone()
    assert zones.seconds(row) == [0, 600, 300, 0, 0]


def test_password_protection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp import auth
    from runapp.server import app

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    assert client.get("/api/dashboard").status_code == 200  # no password set: open, as before

    auth.set_password(db.connect(tmp_path / "api.db"), "correct horse")
    assert client.get("/api/dashboard").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for public in ("/login", "/manifest.webmanifest", "/static/style.css", "/static/login.js", "/static/icons/icon-192.png"):
        assert client.get(public, follow_redirects=False).status_code == 200, public
    assert client.get("/static/app.js", follow_redirects=False).status_code == 303  # the app stays behind the login

    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/api/dashboard").json()["auth"] is True
    assert client.get("/").status_code == 200

    client.post("/api/logout")
    assert client.get("/api/dashboard").status_code == 401

    client.post("/api/login", json={"password": "correct horse"})
    auth.set_password(db.connect(tmp_path / "api.db"), "a new one!")  # changing it signs everyone out
    assert client.get("/api/dashboard").status_code == 401


# ---------- threshold session check ----------
def add_laps(conn, activity_id, laps):
    """laps: (duration_s, pace_s_per_mi, avg_hr)"""
    conn.executemany(
        "INSERT INTO laps (activity_id, idx, distance_m, duration_s, avg_hr, avg_speed_mps) VALUES (?, ?, ?, ?, ?, ?)",
        [(activity_id, i, 1609.344 / p * d, d, hr, 1609.344 / p) for i, (d, p, hr) in enumerate(laps)],
    )
    conn.commit()


def test_reps_found_and_judged(conn):
    from runapp import workouts

    add_run(conn, 1, "2026-09-01 09:00:00", 9, zones=(0, 600, 1800, 600, 0))
    add_laps(conn, 1, [
        (630, 630, 124),   # warm-up mile, slow
        (360, 455, 156),   # rep 1, zone 3
        (60, 1100, 150),   # recovery jog
        (360, 470, 163),   # rep 2, zone 3
        (60, 1150, 152),
        (360, 480, 168),   # rep 3, zone 4: hot
        (60, 1200, 150),
        (360, 495, 177),   # rep 4, zone 5: too hard
        (20, 600, 150),    # lap button pressed by mistake: too short to count
        (640, 640, 110),   # cool-down
    ])
    row = conn.execute("SELECT * FROM activities WHERE id = 1").fetchone()
    s = workouts.summarize(conn, row)
    assert [r["verdict"] for r in s["reps"]] == ["controlled", "controlled", "hot", "too_hard"]
    assert [r["zone"] for r in s["reps"]] == [3, 3, 4, 5]
    assert s["pattern"] == "4 × 6:00" and s["rep_length"] == "long"
    assert s["verdict"] == "too_hard"
    assert s["pace_fade_s"] == pytest.approx(40) and s["hr_drift"] == pytest.approx(21)
    # pace at 160: each rep's speed scaled by 160/HR, averaged
    speeds = [1609.344 / p * 160 / hr for p, hr in ((455, 156), (470, 163), (480, 168), (495, 177))]
    assert s["pace_at_160_s_per_mi"] == pytest.approx(1609.344 / (sum(speeds) / 4))


def test_short_reps_pattern_by_distance(conn):
    from runapp import workouts

    add_run(conn, 2, "2026-09-03 09:00:00", 8, zones=(0, 600, 1500, 0, 0))
    add_laps(conn, 2, [(600, 640, 130)] + [(104, 419, 158), (45, 1300, 150)] * 6)
    row = conn.execute("SELECT * FROM activities WHERE id = 2").fetchone()
    s = workouts.summarize(conn, row)
    assert s["rep_count"] == 6 and s["rep_length"] == "short" and s["verdict"] == "controlled"
    assert s["pattern"] == "6 × 1:44"


# ---------- race log ----------
def test_race_log_prs_efforts_and_predictions(conn):
    from runapp import races

    add_run(conn, 11, "2025-10-12 09:18:00", 5.26, pace=265)   # watch: 5.26 km in 23:14
    add_run(conn, 12, "2026-05-03 07:25:00", 21.5, pace=400)   # an easy half
    add_run(conn, 13, "2026-02-15 10:00:00", 16.25, pace=291)
    races.save(conn, {"date": "2025-10-12", "name": "Gaza 5k", "distance_m": 5000, "course": "Prospect Park",
                      "effort": "all_out", "official_time_s": 1367.88})
    races.save(conn, {"date": "2026-05-03", "name": "Vancouver Half", "distance_m": 21097.5, "effort": "easy"})
    races.save(conn, {"date": "2026-02-15", "name": "Cherry Tree", "distance_m": 16093.44, "course": "Prospect Park", "effort": "all_out"})
    races.save(conn, {"date": "2026-10-11", "name": "Prospect Park 5k", "distance_m": 5000, "course": "prospect park ", "effort": "controlled"})

    d = races.all_races(conn, today=date(2026, 10, 2))
    by_name = {r["name"]: r for r in d["past"]}
    gaza = by_name["Gaza 5k"]
    assert gaza["activity_id"] == 11  # auto-linked to that day's run
    assert (gaza["time_s"], gaza["time_source"]) == (1367.88, "official")
    assert gaza["watch_distance_factor"] == pytest.approx(1.052)
    assert gaza["is_pr"] and gaza["is_course_pr"]
    half = by_name["Vancouver Half"]
    assert half["time_source"] == "watch" and not half["is_pr"]  # easy race: never a PR
    up = d["upcoming"][0]
    assert up["days_to_go"] == 9 and up["course_history"]["name"] == "Gaza 5k"  # course match ignores case/spaces
    assert up["course_pr_s"] == 1367.88

    results = races.all_out_results(conn, date(2026, 10, 2))
    assert {r["label"].split(" (")[0] for r in results} == {"Gaza 5k", "Cherry Tree"}  # the easy half is excluded
    assert races.all_out_results(conn, date(2026, 10, 20)) == [r for r in results if "Cherry" in r["label"]]  # Gaza ages out

    conn.execute("INSERT INTO goals (kind, label, distance_m, target, target_date) VALUES ('race', 'Frozen Penguin', 5000, 1259, '2027-01-16')")
    (goal,) = analysis.goal_progress(conn, today=date(2026, 10, 2))
    assert goal["current"] == pytest.approx(1367.88) and goal["basis"] == "Gaza 5k (Oct 2025)"
    p5k = analysis.predictions(conn, today=date(2026, 10, 2))[0]
    assert p5k["from_race"]["time_s"] == pytest.approx(1367.88)


def test_unlogged_garmin_races(conn):
    from runapp import races

    store.upsert_summary(conn, {"activityId": 21, "activityName": "Some 10k", "activityType": {"typeKey": "running"},
                                "eventType": {"typeKey": "race"}, "startTimeLocal": "2026-03-01 08:00:00", "distance": 10050, "duration": 2900})
    assert [u["activity_id"] for u in races.unlogged_garmin_races(conn)] == [21]
    races.save(conn, {"date": "2026-03-01", "name": "Some 10k", "distance_m": 10000, "effort": "all_out"})
    assert races.unlogged_garmin_races(conn) == []


def test_races_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    body = {"date": "2026-10-18", "name": "Croton-on-Hudson 5k", "distance_m": 5000, "effort": "all_out", "target_time_s": 1350}
    d = client.post("/api/races", json=body).json()
    race_id = (d["upcoming"] + d["past"])[0]["id"]
    assert client.put(f"/api/races/{race_id}", json={**body, "effort": "sideways"}).status_code == 422
    assert client.put(f"/api/races/{race_id}", json={**body, "course": "Croton"}).status_code == 200
    assert client.delete(f"/api/races/{race_id}").json()["upcoming"] == []


# ---------- coach plan proposals ----------
def test_plan_proposals_apply_and_dismiss(conn):
    from runapp import plan

    plan.save(conn, "", [{"date": "2026-10-06", "kind": "threshold", "title": "5x6 min"},
                         {"date": "2026-10-07", "kind": "easy", "title": "35 min easy"}])
    with pytest.raises(ValueError):
        plan.propose(conn, "bad", [{"op": "set", "date": "Oct 6", "kind": "easy", "title": "x"}])
    with pytest.raises(ValueError):
        plan.propose(conn, "bad", [{"op": "set", "date": "2026-10-06", "kind": "jog", "title": "x"}])
    pid = plan.propose(conn, "Move threshold a day later", [
        {"op": "set", "date": "2026-10-06", "kind": "easy", "title": "30 min easy"},
        {"op": "set", "date": "2026-10-07", "kind": "threshold", "title": "5x6 min", "details": "HR <= 160"},
        {"op": "add", "date": "2026-10-07", "kind": "cross", "title": "Easy swim"},
    ])
    (p,) = plan.proposals(conn)
    assert p["changes"][0]["current"] == [{"kind": "threshold", "title": "5x6 min"}]
    assert [s["title"] for s in plan.get(conn)["sessions"]] == ["5x6 min", "35 min easy"]  # nothing changed yet

    plan.apply_proposal(conn, pid)
    assert [(s["date"], s["kind"], s["title"]) for s in plan.get(conn)["sessions"]] == [
        ("2026-10-06", "easy", "30 min easy"), ("2026-10-07", "threshold", "5x6 min"), ("2026-10-07", "cross", "Easy swim")]
    assert plan.proposals(conn) == []
    with pytest.raises(ValueError):
        plan.apply_proposal(conn, pid)  # can't apply twice

    pid2 = plan.propose(conn, "Rest instead", [{"op": "remove", "date": "2026-10-07"}])
    plan.dismiss_proposal(conn, pid2)
    assert plan.proposals(conn) == [] and len(plan.get(conn)["sessions"]) == 3


def test_coach_tool_only_proposes(tmp_path, monkeypatch):
    from runapp import coach_tools, plan

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tool.db")
    c = db.connect(tmp_path / "tool.db")
    plan.save(c, "", [{"date": "2026-10-06", "kind": "threshold", "title": "5x6 min"}])
    msg = coach_tools.propose_plan_changes("Easier Tuesday", [{"op": "set", "date": "2026-10-06", "kind": "easy", "title": "40 min easy"}])
    assert "proposal #1" in msg
    assert "Not saved" in coach_tools.propose_plan_changes("x", [{"op": "explode", "date": "2026-10-06"}])
    assert plan.get(c)["sessions"][0]["title"] == "5x6 min"  # untouched until applied


def test_proposals_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp import plan
    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    c = db.connect(tmp_path / "api.db")
    pid = plan.propose(c, "Add a shakeout", [{"op": "add", "date": "2026-10-10", "kind": "easy", "title": "20 min easy"}])
    client = TestClient(app)
    assert [p["id"] for p in client.get("/api/plan/proposals").json()["proposals"]] == [pid]
    applied = client.post(f"/api/plan/proposals/{pid}/apply").json()
    assert applied["sessions"][0]["title"] == "20 min easy"
    assert client.post(f"/api/plan/proposals/{pid}/apply").status_code == 409


# ---------- weather / heat adjustment ----------
def test_heat_cost_rule():
    from runapp import weather

    assert weather.heat_cost_pct(60, 40) == 0.0               # 100: no cost
    assert weather.heat_cost_pct(70, 50) == pytest.approx(1.0)  # 120
    assert weather.heat_cost_pct(75, 70) == pytest.approx(3.75) # 145: halfway between 3.0 and 4.5
    assert weather.heat_cost_pct(90, 90) == pytest.approx(10.0) # 180
    assert weather.heat_cost_pct(100, 99) == 12.0               # capped
    assert weather.heat_cost_pct(70, None) is None
    assert weather.dew_point_from_humidity(70, 73) == pytest.approx(61, abs=0.5)
    assert weather.adjust_speed(3.0, 5) == pytest.approx(3.15)


def add_weather(conn, activity_id, temp, dew, humidity=None):
    conn.execute("INSERT INTO activity_weather VALUES (?, ?, ?, ?, 5, 'n', 'Fair', '{}', datetime('now'))",
                 (activity_id, temp, dew, humidity))
    conn.commit()


def test_heat_adjusted_efficiency_and_races(conn):
    from runapp import races

    add_run(conn, 1, "2026-08-10 07:00:00", 8, pace=420, zones=(0, 3000, 0, 0, 0))
    add_run(conn, 2, "2026-08-11 07:00:00", 8, pace=420, zones=(0, 3000, 0, 0, 0))
    add_weather(conn, 1, 88, 82)                 # 170 -> 8% heat cost
    add_weather(conn, 2, 70, None, humidity=73)  # dew point derived: ~61 -> ~2%
    by_id = {p["id"]: p for p in analysis.efficiency(conn, today=date(2026, 8, 20))}
    assert by_id[1]["ef_adj"] == pytest.approx(by_id[1]["ef"] * 1.08)
    assert by_id[2]["weather"]["dew_point_f"] == pytest.approx(61, abs=0.5)
    assert 1.5 < by_id[2]["weather"]["heat_cost_pct"] < 2.5

    races.save(conn, {"date": "2026-08-10", "name": "Hot 5k", "distance_m": 5000, "effort": "all_out", "official_time_s": 1350,
                      "activity_id": 1})  # linked by hand: an 8 km run is too long to be matched to a 5k on its own
    (r,) = races.all_races(conn, today=date(2026, 8, 20))["past"]
    assert r["adjusted_time_s"] == pytest.approx(1350 / 1.08)
    (res,) = races.all_out_results(conn, date(2026, 8, 20))
    assert res["time_s"] == pytest.approx(1250) and "heat-adjusted" in res["label"]
    (raw,) = races.all_out_results(conn, date(2026, 8, 20), heat_adjusted=False)
    assert raw["time_s"] == 1350


def test_weather_sync_skips_indoor_and_remembers_empty(conn):
    from runapp import weather

    add_run(conn, 1, "2026-09-01 07:00:00", 5)
    add_run(conn, 2, "2026-09-02 07:00:00", 5, type_key="treadmill_running")
    add_run(conn, 3, "2026-09-03 07:00:00", 5)

    class Client:
        calls = []

        def get_activity_weather(self, activity_id):
            self.calls.append(activity_id)
            return {"temp": 64, "dewPoint": 50, "relativeHumidity": 60} if activity_id == 1 else {"temp": None}

    client = Client()
    weather.sync_weather(client, conn, pause_s=0)
    assert sorted(client.calls) == [1, 3]  # the treadmill run is never looked up
    assert weather.for_activity(conn, 1)["heat_cost_pct"] == pytest.approx(0.7)  # 114: between 110 (0.5%) and 120 (1%)
    assert weather.for_activity(conn, 3) is None
    weather.sync_weather(client, conn, pause_s=0)
    assert sorted(client.calls) == [1, 3]  # nothing re-fetched, including the empty one


def test_pages_load_while_a_sync_is_writing(conn, tmp_path):
    import sqlite3

    # A run with no HR data keeps empty zone columns, which used to trigger a write on every connect.
    store.upsert_summary(conn, {"activityId": 1, "activityType": {"typeKey": "running"}, "startTimeLocal": "2026-09-01 07:00:00"})
    conn.commit()
    conn.execute("INSERT INTO sync_log (started_at, status) VALUES ('now', 'running')")  # uncommitted, like a sync mid-save
    try:
        sqlite3.connect(tmp_path / "test.db", timeout=0).execute("INSERT INTO goals (kind, target) VALUES ('race', 1)")
        raise AssertionError("the database should be locked for writing")
    except sqlite3.OperationalError:
        pass
    reader = db.connect(tmp_path / "test.db")
    assert analysis.overview(reader)["activity_count"] == 1  # reads the last committed state without waiting
    reader.close()


def test_event_type_is_added_to_older_databases(tmp_path):
    path = tmp_path / "old.db"
    old = db.connect(path)
    old.execute("ALTER TABLE activities DROP COLUMN event_type")
    old.execute("""INSERT INTO activities (id, is_run, start_local, raw_json) VALUES
                   (1, 1, '2026-03-01 08:00:00', '{"eventType": {"typeKey": "race"}}'), (2, 1, '2026-03-02 08:00:00', '{}')""")
    old.commit()
    old.close()
    db._ready.discard(path)  # as if the app was restarted after the upgrade
    c = db.connect(path)
    rows = c.execute("SELECT * FROM activities ORDER BY id").fetchall()
    assert [analysis.workout_kind(r) for r in rows] == ["race", "easy"]
    c.close()


# ---------- running form ----------
def form_run(conn, activity_id, start, segments, type_key="running"):
    """segments: (seconds, pace_s_per_mi, cadence, gct_ms, vr_pct, balance_pct)"""
    samples, t, d = [], 0, 0.0
    for secs, pace, cad, gct, vr, bal in segments:
        speed = 1609.344 / pace if pace else 0.5
        for _ in range(secs):
            samples.append(Sample(t=t, distance_m=d, hr=140, speed_mps=speed, cadence=cad, gct_ms=gct, vr_pct=vr,
                                  step_mm=speed / (cad / 60) * 1000 if cad else None, vo_mm=90, balance_pct=bal))
            t += 1
            d += speed
    store.upsert_summary(conn, {"activityId": activity_id, "activityName": f"Run {activity_id}", "activityType": {"typeKey": type_key},
                                "startTimeLocal": start, "distance": d, "duration": t, "averageSpeed": d / t, "averageHR": 140})
    store.store_detail(conn, activity_id, samples, [])


def test_cadence_includes_fraction():
    from runapp.streams import _spm

    assert _spm(81, 0.7421875) == pytest.approx(163.484375)
    assert _spm(81, None) == 162 and _spm(None, 0.5) is None


def test_form_bands_ignore_walking_and_use_matched_pace(conn):
    from runapp import form

    form_run(conn, 1, "2026-09-10 07:00:00", [
        (600, 660, 164, 292, 9.5, 50.4),    # easy band (11:00/mi)
        (300, None, 100, 600, 14.0, 68.0),  # walking break: slow, low cadence -> ignored everywhere
        (600, 470, 178, 248, 7.6, 50.2),    # threshold band (7:50/mi)
    ])
    stats = form.compute(conn, 1)
    assert set(stats["bands"]) == {"easy", "threshold"}
    assert stats["bands"]["easy"]["cadence"] == 164 and stats["bands"]["easy"]["gct"] == 292
    assert stats["bands"]["threshold"]["stride"] == pytest.approx(1609.344 / 470 / (178 / 60), abs=0.001)
    assert stats["balance"] == pytest.approx(50.3, abs=0.11)  # the 68% walking reading doesn't count
    r = form.report(conn, today=date(2026, 10, 3))
    assert r["by_band"]["easy"] == []  # one run isn't enough to define a month
    form_run(conn, 2, "2026-09-12 07:00:00", [(600, 660, 166, 290, 9.4, 50.4)])
    r = form.report(conn, today=date(2026, 10, 3))
    (sep,) = r["by_band"]["easy"]
    assert (sep["month"], sep["runs"], sep["cadence"]) == ("2026-09", 2, 165)  # equal time in each run: the average
    assert r["by_band"]["steady"] == [] and r["by_band"]["threshold"] == []
    assert r["alerts"] == []


def test_form_fatigue_is_judged_against_your_own_pace_curve(conn):
    from runapp import form

    # Three reference runs teach the curve: 11:00/mi -> 164 spm / 292 ms; 10:00/mi -> 168 spm / 282 ms.
    for i in range(3):
        form_run(conn, 10 + i, f"2026-08-{10 + i} 07:00:00", [(1500, 660, 164, 292, 9.5, 50.3), (1500, 600, 168, 282, 9.0, 50.3)])
    # Slows from 10:00 to 11:00/mi with exactly the form that's normal at each pace: not fading.
    form_run(conn, 20, "2026-09-20 07:00:00", [(300, 600, 168, 282, 9.0, 50.3), (900, 600, 168, 282, 9.0, 50.3),
                                                 (900, 630, 166, 287, 9.2, 50.3), (900, 660, 164, 292, 9.5, 50.3)])
    # Holds 11:00/mi but form deteriorates late: contact +16 ms, cadence -5.
    form_run(conn, 21, "2026-09-21 07:00:00", [(300, 660, 164, 292, 9.5, 50.3), (900, 660, 164, 292, 9.5, 50.3),
                                                 (900, 660, 162, 300, 9.6, 50.8), (900, 660, 159, 308, 9.7, 53.2)])
    by_id = {f["activity_id"]: f for f in form.report(conn, today=date(2026, 10, 3))["fatigue"]}
    assert by_id[20]["verdict"] == "held" and by_id[20]["pace_change_pct"] == pytest.approx(10.0)
    assert abs(by_id[20]["delta"]["gct"]) < 2 and abs(by_id[20]["delta"]["cadence"]) < 1
    assert by_id[21]["verdict"] == "fading"
    assert by_id[21]["delta"]["gct"] == pytest.approx(16) and by_id[21]["delta"]["cadence"] == pytest.approx(-5)
    assert set(by_id[21]["signs"]) == {"longer ground contact", "lower cadence"}
    alerts = form.report(conn, today=date(2026, 10, 3))["alerts"]
    assert [a["activity_id"] for a in alerts] == [21]  # 53.2% left in the last quarter


def test_form_api_and_treadmill_excluded_from_pace_bands(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    c = db.connect(tmp_path / "api.db")
    form_run(c, 1, "2026-09-10 07:00:00", [(900, 660, 164, 292, 9.5, 50.4)], type_key="treadmill_running")
    r = TestClient(app).get("/api/form").json()
    assert r["by_band"]["easy"] == [] and len(r["balance"]) == 1  # treadmill pace isn't trusted; balance still counts


# ---------- strength plan ----------
def strength_days(conn):
    from runapp import plan

    return [(date.fromisoformat(s["date"]).strftime("%a %d"), s["title"]) for s in plan.get(conn)["sessions"] if s["kind"] == "strength"]


RUN_PLAN = [
    {"date": "2026-10-03", "kind": "rest", "title": "Rest"},
    {"date": "2026-10-04", "kind": "easy", "title": "50 min easy"},
    {"date": "2026-10-05", "kind": "easy", "title": "Easy run"},
    {"date": "2026-10-06", "kind": "threshold", "title": "5x6 min"},
    {"date": "2026-10-07", "kind": "easy", "title": "35 min easy"},
    {"date": "2026-10-08", "kind": "intervals", "title": "4x1 km"},
    {"date": "2026-10-09", "kind": "rest", "title": "Rest"},
    {"date": "2026-10-10", "kind": "easy", "title": "20 min easy + strides"},
    {"date": "2026-10-11", "kind": "race", "title": "Park 5k"},
    {"date": "2026-10-12", "kind": "rest", "title": "Rest"},
    {"date": "2026-10-13", "kind": "easy", "title": "30 min easy"},
    {"date": "2026-10-14", "kind": "intervals", "title": "3x2 min"},
    {"date": "2026-10-15", "kind": "easy", "title": "25 min easy"},
    {"date": "2026-10-16", "kind": "rest", "title": "Rest"},
    {"date": "2026-10-17", "kind": "easy", "title": "20 min easy + strides"},
    {"date": "2026-10-18", "kind": "race", "title": "Croton 5k"},
]


def run_plan_with_tune_up(conn):
    """Two race weeks: a controlled tune-up on the 11th, then an all-out 5k on the 18th."""
    from runapp import plan, races

    plan.save(conn, "", RUN_PLAN)
    races.save(conn, {"date": "2026-10-11", "name": "Park 5k", "distance_m": 5000, "effort": "controlled"})


def test_strength_fits_around_runs_and_races(conn):
    from runapp import plan, strength

    run_plan_with_tune_up(conn)
    today = date(2026, 10, 3)
    assert strength.refit(conn, today) == 0  # nothing until it's set up
    strength.set_up(conn, 2, "bodyweight", "2026-10-03", today)
    days = strength_days(conn)
    # Sat 3 is a rest day; Mon 5 and Wed 7 are the day before hard runs; Tue 6 would make three in a week;
    # Fri 9 - Mon 12 surround the tune-up; before the all-out race on the 18th only Wed 14 is free, and it's light.
    assert days[:3] == [("Sun 04", "Strength A: legs & calves"), ("Thu 08", "Strength B: hips & hamstrings"),
                        ("Wed 14", "Light strength A: legs & calves")]
    # Past the end of the running plan it follows your usual hard days (Tue and Wed in this plan):
    # Tue 20 is still within 2 days of the all-out race, so the first one is Wed 21.
    assert [d for d, _ in days[3:5]] == ["Wed 21", "Sat 24"] and len(days) == 7
    assert strength.issues(conn, today) == []

    by_date = {s["date"]: s for s in plan.with_actuals(conn, today)["sessions"] if s["kind"] == "strength"}
    first, light = by_date["2026-10-04"], by_date["2026-10-14"]
    assert first["notes"][0] == "Intro, week 1 · about 20 min · after your run or later in the day"
    assert first["exercises"][0] == {"name": "Bodyweight squat", "dose": "2 × 8", "cue": "Sit back and down, knees over toes, 3 s on the way down"}
    assert len(first["exercises"]) == 5 and first["state"] == "upcoming"
    assert light["notes"][0].startswith("Race week · about 12 min") and [e["dose"] for e in light["exercises"]][:2] == ["1 × 8", "1 × 8 each side"]
    assert "Pogo hops" not in by_date["2026-10-14"]["details"]  # (routine A has none anyway) no jumping in race week
    text = plan.as_text(conn)
    assert "[strength]: Strength A: legs & calves. Intro, week 1 · about 20 min" in text and "Bodyweight squat 2 × 8;" in text
    assert "knees over toes" not in text  # the coach gets the doses, not every cue


def test_strength_progression_and_equipment():
    from runapp import strength

    start = date(2026, 10, 3)
    assert [strength.phase_on(start, start + timedelta(days=n))["key"] for n in (0, 20, 21, 48, 49, 400)] == [
        "intro", "intro", "build", "build", "strong", "strong"]
    strong = strength.phase_on(start, start + timedelta(days=60))
    gym = strength.parse_details(strength.session("A", strong, "gym")["details"])["exercises"]
    assert (gym[0]["name"], gym[0]["dose"], gym[1]["dose"]) == ("Back squat", "4 × 5", "3 × 6 each side")
    body = strength.parse_details(strength.session("B", strong, "bodyweight")["details"])
    assert body["exercises"][0]["dose"] == "3 × 12 each side" and "4 s" in body["notes"][1]
    light = strength.session("B", strong, "gym", light=True)
    assert "Pogo hops" not in light["details"] and light["title"] == "Light strength B: hips & hamstrings"


def test_strength_refit_keeps_the_past_and_flags_clashes(conn):
    from runapp import plan, strength

    run_plan_with_tune_up(conn)
    strength.set_up(conn, 2, "dumbbells", "2026-10-03", date(2026, 10, 3))
    assert plan.with_actuals(conn, date(2026, 10, 5))["sessions"][2]["state"] == "missed"  # Sun 4: nothing logged
    strength.log(conn, "2026-10-04", True)
    done = next(s for s in plan.with_actuals(conn, date(2026, 10, 5))["sessions"] if s["kind"] == "strength")
    assert (done["state"], done["logged"]) == ("done", True)
    strength.log(conn, "2026-10-04", False)
    # A strength activity recorded on the watch ticks it off too, and shows on the strength row rather than the run's.
    store.upsert_summary(conn, {"activityId": 7, "activityName": "Strength", "activityType": {"typeKey": "strength_training"},
                                "startTimeLocal": "2026-10-04 18:00:00", "duration": 1200})
    rows = [s for s in plan.with_actuals(conn, date(2026, 10, 5))["sessions"] if s["date"] == "2026-10-04"]
    assert [(s["kind"], s["state"], len(s["actual"])) for s in rows] == [("easy", "missed", 0), ("strength", "done", 1)]

    # The threshold run moves to Friday: Thursday's strength is now the day before a hard run.
    conn.execute("UPDATE plan_sessions SET date = '2026-10-09', kind = 'threshold' WHERE date = '2026-10-08' AND kind = 'intervals'")
    conn.execute("DELETE FROM plan_sessions WHERE date = '2026-10-09' AND kind = 'rest'")
    conn.commit()
    today = date(2026, 10, 6)
    assert [(i["date"], i["reason"]) for i in strength.issues(conn, today)] == [("2026-10-08", "the next day is a hard run")]
    assert plan.with_actuals(conn, today)["strength"]["issues"]
    strength.refit(conn, today)
    days = strength_days(conn)
    assert days[0] == ("Sun 04", "Strength A: legs & calves")          # the past stays
    assert days[1] == ("Tue 06", "Strength B: hips & hamstrings")      # after the hard run, alternating on from A
    assert "Thu 08" not in [d for d, _ in days] and strength.issues(conn, today) == []

    strength.remove(conn, today)
    assert strength_days(conn) == [("Sun 04", "Strength A: legs & calves")] and strength.settings(conn) is None


def test_strength_proposals_leave_the_run_alone(conn):
    from runapp import plan

    plan.save(conn, "", [{"date": "2026-10-06", "kind": "threshold", "title": "5x6 min"},
                         {"date": "2026-10-06", "kind": "strength", "title": "Strength A: legs & calves", "details": "Squat | 2 × 8 | Slow"}])
    pid = plan.propose(conn, "Easier day, strength moves", [
        {"op": "set", "date": "2026-10-06", "kind": "easy", "title": "40 min easy"},
        {"op": "remove", "date": "2026-10-06", "kind": "strength"},
        {"op": "set", "date": "2026-10-08", "kind": "strength", "title": "Strength A: legs & calves", "details": "Squat | 2 × 8 | Slow"},
    ])
    (p,) = plan.proposals(conn)
    assert p["changes"][0]["current"] == [{"kind": "threshold", "title": "5x6 min"}]
    assert p["changes"][1]["current"] == [{"kind": "strength", "title": "Strength A: legs & calves"}]
    plan.apply_proposal(conn, pid)
    assert [(s["date"], s["kind"]) for s in plan.get(conn)["sessions"]] == [("2026-10-06", "easy"), ("2026-10-08", "strength")]
    # Changing only the run keeps the strength session beside it.
    plan.apply_proposal(conn, plan.propose(conn, "Rest", [{"op": "set", "date": "2026-10-08", "kind": "rest", "title": "Rest"},
                                                         {"op": "remove", "date": "2026-10-06"}]))
    assert [(s["date"], s["kind"]) for s in plan.get(conn)["sessions"]] == [("2026-10-08", "rest"), ("2026-10-08", "strength")]


def test_strength_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert client.put("/api/strength", json={"per_week": 5, "equipment": "gym", "start": tomorrow}).status_code == 422
    assert client.put("/api/strength", json={"per_week": 2, "equipment": "anvil", "start": tomorrow}).status_code == 422
    p = client.put("/api/strength", json={"per_week": 2, "equipment": "gym", "start": tomorrow}).json()
    sessions = [s for s in p["sessions"] if s["kind"] == "strength"]
    assert p["strength"]["enabled"] and p["strength"]["settings"]["equipment"] == "gym" and p["strength"]["phase"]["starts_in_days"] == 1
    assert len(sessions) >= 6 and all(s["date"] >= tomorrow for s in sessions)  # two a week, at least four weeks out
    first = sessions[0]["date"]
    logged = client.post("/api/strength/log", json={"date": first, "done": True}).json()
    assert next(s for s in logged["sessions"] if s["date"] == first)["state"] == "done"
    # The plan editor round-trips strength sessions like any other.
    body = {"notes": "", "sessions": [{k: s[k] for k in ("date", "kind", "title", "details")} for s in sessions]}
    assert len(client.put("/api/plan", json=body).json()["sessions"]) == len(sessions)
    assert len(client.post("/api/strength/refit").json()["sessions"]) == len(sessions)
    gone = client.delete("/api/strength").json()
    assert not gone["strength"]["enabled"] and gone["sessions"] == []


# ---------- sending planned runs to Garmin ----------
def raw_step(kind, **fields):
    """A step as the conversion returns it: every field present, 0 or "" meaning none."""
    return {"type": kind, "duration_s": 0, "distance_m": 0, "hr_low": 0, "hr_high": 0, "pace_fast": "", "pace_slow": "", "note": "",
            "times": 0, "steps": [], **fields}


THRESHOLD_STEPS = [
    raw_step("warmup", duration_s=900),
    raw_step("repeat", times=5, steps=[raw_step("run", duration_s=360, hr_high=160, pace_fast="7:39", pace_slow="8:19"),
                                       raw_step("recover", duration_s=60)]),
    raw_step("cooldown", duration_s=900, note="15 min easy"),
]


def test_garmin_steps_warmup_and_cooldown_end_on_the_lap_button():
    from runapp import garmin_workouts as gw

    steps = gw.normalize(THRESHOLD_STEPS)
    # The planned 15 minutes become a note; neither step keeps a timer.
    assert steps[0] == {"type": "warmup", "note": "Plan: about 15 min easy"}
    assert steps[2] == {"type": "cooldown", "note": "15 min easy"}
    rep, jog = steps[1]["steps"]
    assert rep == {"type": "run", "duration_s": 360, "hr_low": 150, "hr_high": 160}  # a cap becomes a range; heart rate beats pace
    assert jog == {"type": "recover", "duration_s": 60}

    g = gw.to_garmin("5x6 min sub-threshold", "15 min easy before and after", steps)
    warm, group, cool = g["workoutSegments"][0]["workoutSteps"]
    for s in (warm, cool):
        assert s["endCondition"]["conditionTypeKey"] == "lap.button" and "endConditionValue" not in s
    assert (group["numberOfIterations"], group["skipLastRestStep"], [s["stepOrder"] for s in group["workoutSteps"]]) == (5, True, [3, 4])
    work = group["workoutSteps"][0]
    assert work["endCondition"]["conditionTypeKey"] == "time" and work["endConditionValue"] == 360
    assert (work["targetType"]["workoutTargetTypeKey"], work["targetValueOne"], work["targetValueTwo"]) == ("heart.rate.zone", 150, 160)
    assert cool["stepOrder"] == 5 and warm["description"] == "Plan: about 15 min easy"


def test_garmin_steps_pace_targets_distances_and_bad_input():
    from runapp import garmin_workouts as gw

    steps = gw.normalize([
        raw_step("run", distance_m=1000, pace_fast="7:15", pace_slow="7:06"),   # ends swapped: sorted
        raw_step("repeat", times=1, steps=[raw_step("run", duration_s=20)]),    # "1 x" is just the step
        raw_step("sprint", duration_s=10),                                       # unknown type: dropped
        raw_step("recover", duration_s=0),                                       # no length: until Lap
        raw_step("run", duration_s=1200, hr_low=300, hr_high=20),                # nonsense target: no target
    ])
    assert steps == [{"type": "run", "distance_m": 1000, "pace_fast": 426.0, "pace_slow": 435.0}, {"type": "run", "duration_s": 20},
                     {"type": "recover"}, {"type": "run", "duration_s": 1200}]
    km, _, jog, _ = gw.to_garmin("x", "", steps)["workoutSegments"][0]["workoutSteps"]
    assert km["endCondition"]["conditionTypeKey"] == "distance" and km["endConditionValue"] == 1000
    # Garmin wants pace as speed in m/s, fast end first: 7:06/mi = 3.78 m/s, 7:15/mi = 3.70 m/s.
    assert km["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert (round(km["targetValueOne"], 2), round(km["targetValueTwo"], 2)) == (3.78, 3.70)
    assert jog["endCondition"]["conditionTypeKey"] == "lap.button" and jog["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert gw.parse_pace("7:06/mi") == 426 and gw.parse_pace("4:25") == 265 and gw.parse_pace("fast") is None
    with pytest.raises(gw.GarminWorkoutError):
        gw.normalize([raw_step("warmup"), raw_step("cooldown")])  # no running in it


class FakeWorkoutApi:
    def __init__(self):
        self.uploaded, self.scheduled, self.deleted = [], [], []

    def upload_workout(self, payload):
        self.uploaded.append(payload)
        return {"workoutId": 111}

    def schedule_workout(self, workout_id, day):
        self.scheduled.append((workout_id, day))
        return {"workoutScheduleId": 222}

    def delete_workout(self, workout_id):
        self.deleted.append(workout_id)


def test_garmin_workout_build_send_remove(conn):
    from runapp import garmin_workouts as gw, plan

    plan.save(conn, "HR cap 160 on threshold reps", [
        {"date": "2026-10-05", "kind": "rest", "title": "Rest"},
        {"date": "2026-10-06", "kind": "threshold", "title": "5x6 min sub-threshold", "details": "15 min easy before and after"},
        {"date": "2026-10-06", "kind": "strength", "title": "Strength A: legs & calves", "details": "Squat | 2 × 8 | Slow"},
    ])
    asked = {}

    def runner(message, prompt, args, timeout):
        asked.update(prompt=prompt, args=args)
        return {"structured_output": {"steps": THRESHOLD_STEPS}}

    with pytest.raises(gw.GarminWorkoutError):
        gw.build(conn, "2026-10-05", runner)  # a rest day has nothing to send
    with pytest.raises(gw.GarminWorkoutError):
        gw.send(conn, FakeWorkoutApi(), "2026-10-06")  # nothing built yet
    steps = gw.build(conn, "2026-10-06", runner)
    assert steps[0]["type"] == "warmup" and "duration_s" not in steps[0]
    assert "5x6 min sub-threshold" in asked["prompt"] and "HR cap 160" in asked["prompt"] and "--json-schema" in asked["args"]

    by_kind = {s["kind"]: s for s in plan.with_actuals(conn, date(2026, 10, 5))["sessions"]}
    assert by_kind["threshold"]["garmin"]["state"] == "draft" and by_kind["threshold"]["garmin"]["steps"] == steps
    assert "garmin" not in by_kind["rest"] and "garmin" not in by_kind["strength"]

    garmin = FakeWorkoutApi()
    gw.send(conn, garmin, "2026-10-06")
    assert garmin.scheduled == [(111, "2026-10-06")] and garmin.uploaded[0]["workoutName"] == "5x6 min sub-threshold"
    sent = next(s for s in plan.with_actuals(conn, date(2026, 10, 5))["sessions"] if s["kind"] == "threshold")["garmin"]
    assert (sent["state"], sent["scheduled"], sent["changed"]) == ("sent", True, False)
    with pytest.raises(gw.GarminWorkoutError):
        gw.send(conn, garmin, "2026-10-06")  # not twice
    with pytest.raises(gw.GarminWorkoutError):
        gw.build(conn, "2026-10-06", runner)  # nor rebuilt while it's on Garmin

    # Editing the plan afterwards shows on the session, so you know the watch has the old version.
    conn.execute("UPDATE plan_sessions SET title = '4x6 min sub-threshold' WHERE kind = 'threshold'")
    assert next(s for s in plan.with_actuals(conn, date(2026, 10, 5))["sessions"] if s["kind"] == "threshold")["garmin"]["changed"]
    gw.discard(conn, "2026-10-06")  # only drops unsent drafts
    assert conn.execute("SELECT COUNT(*) FROM garmin_workouts").fetchone()[0] == 1
    gw.remove(conn, garmin, "2026-10-06")
    assert garmin.deleted == [111] and conn.execute("SELECT COUNT(*) FROM garmin_workouts").fetchone()[0] == 0


def test_garmin_workout_kept_when_scheduling_fails(conn):
    from runapp import garmin_workouts as gw, plan

    plan.save(conn, "", [{"date": "2026-10-06", "kind": "easy", "title": "40 min easy"}])
    gw.build(conn, "2026-10-06", lambda *a: {"structured_output": {"steps": [raw_step("run", duration_s=2400, hr_low=100, hr_high=150)]}})

    class NoCalendar(FakeWorkoutApi):
        def schedule_workout(self, workout_id, day):
            raise RuntimeError("boom")

    garmin = NoCalendar()
    with pytest.raises(gw.GarminWorkoutError, match="in your Garmin library"):
        gw.send(conn, garmin, "2026-10-06")
    info = plan.with_actuals(conn, date(2026, 10, 5))["sessions"][0]["garmin"]
    assert (info["state"], info["scheduled"]) == ("sent", False)  # still known here, so it can be removed
    gw.remove(conn, garmin, "2026-10-06")
    assert garmin.deleted == [111]


def test_garmin_workout_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp import garmin_workouts as gw, plan, sync
    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    c = db.connect(tmp_path / "api.db")
    plan.save(c, "", [{"date": "2026-10-06", "kind": "easy", "title": "40 min easy"}])
    client = TestClient(app)
    assert client.post("/api/garmin/workouts/2026-10-07/send").status_code == 409  # nothing built for that day
    assert client.post("/api/garmin/workouts/tuesday/build").status_code == 422
    gw.build(c, "2026-10-06", lambda *a: {"structured_output": {"steps": [raw_step("run", duration_s=2400)]}})
    assert client.get("/api/plan").json()["sessions"][0]["garmin"]["state"] == "draft"

    def not_logged_in(interactive=True):
        raise sync.NotLoggedIn("Garmin session expired")

    monkeypatch.setattr(sync, "login", not_logged_in)
    assert client.post("/api/garmin/workouts/2026-10-06/send").status_code == 503
    garmin = FakeWorkoutApi()
    monkeypatch.setattr(sync, "login", lambda interactive=True: garmin)
    assert client.post("/api/garmin/workouts/2026-10-06/send").json()["sessions"][0]["garmin"]["state"] == "sent"
    assert client.delete("/api/garmin/workouts/2026-10-06").json()["sessions"][0]["garmin"]["state"] == "sent"  # a draft-only delete
    assert client.delete("/api/garmin/workouts/2026-10-06?sent=true").json()["sessions"][0]["garmin"]["state"] == "none"
    assert garmin.deleted == [111]
