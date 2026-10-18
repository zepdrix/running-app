"""Tests for the fixes that came out of the project review."""

from datetime import date

import pytest
from test_core import add_run, conn  # noqa: F401  (conn is a fixture)

from runapp import analysis, config, store


def test_race_entered_early_gets_its_run_after_race_day(conn):
    from runapp import races

    races.save(conn, {"date": "2026-10-11", "name": "Park 5k", "distance_m": 5000, "effort": "all_out"})
    add_run(conn, 1, "2026-10-11 08:00:00", 2)  # the warm-up: too short to be the race
    assert races.link_activities(conn, date(2026, 10, 11)) == 0
    store.upsert_summary(conn, {"activityId": 2, "activityName": "Park 5k", "activityType": {"typeKey": "running"},
                                "eventType": {"typeKey": "race"}, "startTimeLocal": "2026-10-11 09:00:00", "distance": 5050, "duration": 1300})
    assert [u["activity_id"] for u in races.all_races(conn, today=date(2026, 10, 12))["unlogged"]] == [2]
    assert races.link_activities(conn, date(2026, 10, 10)) == 0  # not before race day
    assert races.link_activities(conn, date(2026, 10, 11)) == 1
    d = races.all_races(conn, today=date(2026, 10, 12))
    assert d["past"][0]["activity"]["id"] == 2 and d["past"][0]["time_s"] == 1300
    assert d["unlogged"] == []  # no longer offered as a second, duplicate entry


def test_wellness_sync_fills_the_days_since_the_last_one(conn, tmp_path, monkeypatch):
    from runapp import wellness

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(wellness.time, "sleep", lambda s: None)

    class Client:
        def __init__(self):
            self.days = []

        def get_stats(self, day):
            self.days.append(day)
            return {}

        get_sleep_data = get_hrv_data = get_training_readiness = get_training_status = lambda self, day: {}

    client = Client()
    wellness.sync_wellness(client, conn, today=date(2026, 10, 1))
    assert len(client.days) == wellness.FIRST_SYNC_DAYS
    client.days.clear()
    wellness.sync_wellness(client, conn, today=date(2026, 10, 2))
    assert len(client.days) == wellness.LATER_SYNC_DAYS  # synced yesterday: just the usual few days
    client.days.clear()
    wellness.sync_wellness(client, conn, today=date(2026, 10, 12))
    assert client.days[0] == "2026-10-12" and client.days[-1] == "2026-10-02"  # ten days away: all of them
    client.days.clear()
    wellness.sync_wellness(client, conn, today=date(2027, 3, 1))
    assert len(client.days) == wellness.MAX_SYNC_DAYS


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return TestClient(app)


def test_other_websites_cannot_reach_the_app(client):
    assert client.get("/api/sync").status_code == 200
    assert client.get("/api/sync", headers={"host": "localhost:8000"}).status_code == 200
    assert client.get("/api/sync", headers={"host": "my-mac.tail1234.ts.net"}).status_code == 200
    assert client.get("/api/sync", headers={"host": "evil.example"}).status_code == 403  # DNS rebinding

    dismiss = "/api/plan/proposals/1/dismiss"
    assert client.post(dismiss).status_code == 200
    assert client.post(dismiss, headers={"sec-fetch-site": "same-origin"}).status_code == 200
    assert client.post(dismiss, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert client.post(dismiss, headers={"sec-fetch-site": "same-site"}).status_code == 403  # e.g. another local port
    assert client.post(dismiss, headers={"origin": "https://evil.example"}).status_code == 403  # older browsers
    assert client.post(dismiss, headers={"origin": "http://testserver"}).status_code == 200
    assert client.get("/api/sync", headers={"sec-fetch-site": "cross-site"}).status_code == 200  # reading stays blocked by the browser


def test_sync_problems_are_reported_not_swallowed(client, tmp_path, monkeypatch):
    from runapp import server, status, sync

    token = tmp_path / "garmin_tokens.json"
    token.write_text("{}")
    monkeypatch.setattr(config, "TOKEN_PATH", token)

    class Broken:
        def login(self, path):
            raise RuntimeError("boom")

    monkeypatch.setattr(sync, "Garmin", Broken)
    monkeypatch.setattr(sync, "_garmin_reachable", lambda: False)
    with pytest.raises(sync.GarminUnreachable):
        sync.login(interactive=False)
    monkeypatch.setattr(sync, "_garmin_reachable", lambda: True)
    with pytest.raises(sync.NotLoggedIn):
        sync.login(interactive=False)

    error = server._sync_worker(False)
    assert "sign-in has expired" in error and server._sync_state == {"running": False, "message": error}

    monkeypatch.setattr(status, "generate", lambda conn, units: None)  # no Claude in tests
    assert client.post("/api/status").json()["sync_error"] == error
    assert client.post("/api/status?sync_first=false").json()["sync_error"] is None


def test_coach_message_goes_in_on_stdin_and_keeps_its_conversation(conn, monkeypatch):
    from runapp import coach

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["input"] = cmd, kwargs["input"]

        class Proc:
            stdout, stderr = '{"result": "ok", "session_id": "s1"}', ""

        return Proc()

    monkeypatch.setattr(coach, "_claude_bin", lambda: "claude")
    monkeypatch.setattr(coach.subprocess, "run", fake_run)
    coach.run_claude("-10 s/mi realistic?", "system", [])
    assert seen["input"] == "-10 s/mi realistic?" and "-10 s/mi realistic?" not in seen["cmd"]

    conn.execute("INSERT OR REPLACE INTO coach_state VALUES ('session_id', 'old')")
    calls = []

    def flaky(message, session_id, *rest):
        calls.append(session_id)
        raise coach.CoachError("Claude Code error: rate limited")

    monkeypatch.setattr(coach, "_run", flaky)
    with pytest.raises(coach.CoachError):
        coach.ask(conn, "hi")
    assert calls == ["old"]  # a passing failure doesn't throw the conversation away

    def gone(message, session_id, *rest):
        calls.append(session_id)
        if session_id:
            raise coach.CoachError("Claude Code failed: No conversation found with session ID: old")
        return {"result": "hello", "session_id": "new"}

    calls.clear()
    monkeypatch.setattr(coach, "_run", gone)
    assert coach.ask(conn, "hi") == {"reply": "hello"}
    assert calls == ["old", None]


def test_resumed_coach_chat_gets_the_current_prompt(monkeypatch):
    from runapp import coach

    monkeypatch.setattr(coach, "_can_refresh_prompt", lambda: True)
    args = coach._coach_args("s1")
    assert args[args.index("--resume") + 1] == "s1"
    assert args[args.index("--system-prompt-snapshot") + 1] == "off"  # else the plan and date stay as they were on day one
    monkeypatch.setattr(coach, "_can_refresh_prompt", lambda: False)  # an older Claude Code would reject the option
    assert "--system-prompt-snapshot" not in coach._coach_args("s1")


def test_treadmill_efforts_are_not_records_or_predictions(conn):
    add_run(conn, 1, "2026-09-01 07:00:00", 10.2, pace=300)
    add_run(conn, 2, "2026-09-02 07:00:00", 10.2, pace=240, type_key="treadmill_running")  # "faster", on the belt
    records = {r["label"]: r for r in analysis.personal_records(conn, today=date(2026, 10, 2))}
    assert records["10k"]["all_time"]["activity_id"] == 1 and records["10k"]["recent"]["activity_id"] == 1
    p10k = analysis.predictions(conn, today=date(2026, 10, 2))[1]
    assert p10k["riegel"]["time_s"] == pytest.approx(3000, rel=0.01)
    assert conn.execute("SELECT COUNT(*) FROM best_efforts WHERE activity_id = 2").fetchone()[0] > 0  # still saved


def test_cross_training_day_counts_as_done(conn):
    from runapp import plan

    add_run(conn, 1, "2026-10-03 08:00:00", 20, type_key="cycling")
    plan.save(conn, "", [{"date": "2026-10-03", "kind": "cross", "title": "Easy ride"},
                         {"date": "2026-10-04", "kind": "cross", "title": "Swim"}])
    p = plan.with_actuals(conn, today=date(2026, 10, 5))
    assert [s["state"] for s in p["sessions"]] == ["done", "missed"]


def test_impossible_dates_and_distances_are_refused(client):
    race = {"date": "2026-02-30", "name": "Never", "distance_m": 5000}
    assert client.post("/api/races", json=race).status_code == 422
    assert client.post("/api/races", json={**race, "date": "2026-02-28"}).status_code == 200
    assert client.put("/api/plan", json={"sessions": [{"date": "2026-13-01", "kind": "easy", "title": "x"}]}).status_code == 422
    goal = {"kind": "race", "target": 1260, "distance_m": 5000}
    assert client.post("/api/goals", json={**goal, "target_date": "next spring"}).status_code == 422
    assert client.post("/api/goals", json={**goal, "distance_m": -5000}).status_code == 422
    assert client.post("/api/goals", json={**goal, "target_date": "2027-01-16"}).status_code == 200
    assert client.get("/api/dashboard").status_code == 200


def test_weather_sync_gives_up_when_garmin_keeps_failing(conn):
    from runapp import weather

    for i in range(1, 7):
        add_run(conn, i, f"2026-09-0{i} 07:00:00", 5)

    class Down:
        calls = 0

        def get_activity_weather(self, activity_id):
            self.calls += 1
            raise RuntimeError("429")

    client = Down()
    weather.sync_weather(client, conn, pause_s=0)
    assert client.calls == 3  # not all six: the rest wait for the next sync
    assert conn.execute("SELECT COUNT(*) FROM activity_weather").fetchone()[0] == 0  # and nothing is written off


def test_coach_queries_are_bounded(conn, tmp_path, monkeypatch):
    from runapp import query

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    add_run(conn, 1, "2026-09-01 07:00:00", 5)
    conn.commit()
    assert query.run("SELECT id FROM activities") == '[{"id": 1}]'
    monkeypatch.setattr(query, "MAX_CHARS", 50)
    assert query.run("SELECT raw_json FROM activities").startswith("Result too large")
    monkeypatch.setattr(query, "TIMEOUT_S", 0.05)
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT COUNT(*) FROM n"
    assert "ran for more than" in query.run(slow)


def test_login_locks_after_repeated_wrong_passwords(client, tmp_path, monkeypatch):
    from runapp import auth, db, server

    monkeypatch.setattr(server, "WRONG_PASSWORD_DELAY_S", 0)
    monkeypatch.setattr(server, "_login", {"failures": 0, "locked_until": 0.0})
    c = db.connect(tmp_path / "api.db")
    auth.set_password(c, "correct horse")
    c.execute("INSERT INTO auth_sessions VALUES ('stale', '2020-01-01T00:00:00')")
    c.commit()
    for _ in range(server.MAX_LOGIN_FAILURES):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 429  # locked, even when right
    server._login["locked_until"] = 0.0
    assert client.post("/api/login", json={"password": "correct horse"}).status_code == 200
    assert [r[0] for r in c.execute("SELECT COUNT(*) FROM auth_sessions")] == [1]  # the expired session was cleared out


def test_race_run_today_counts_once_its_result_is_in(conn):
    from runapp import races

    races.save(conn, {"date": "2026-10-11", "name": "Park 5k", "distance_m": 5000, "effort": "all_out"})
    assert [r["name"] for r in races.all_races(conn, today=date(2026, 10, 11))["upcoming"]] == ["Park 5k"]
    add_run(conn, 1, "2026-10-11 09:00:00", 5.05, pace=260)
    races.link_activities(conn, date(2026, 10, 11))
    d = races.all_races(conn, today=date(2026, 10, 11))
    assert d["upcoming"] == [] and d["past"][0]["is_pr"]


def test_app_files_are_rechecked_on_every_load(tmp_path, monkeypatch):
    """Without this a phone keeps running old code for as long as its browser guesses the copy is still good."""
    from fastapi.testclient import TestClient

    from runapp.server import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api.db")
    client = TestClient(app)
    for path in ("/", "/static/app.js", "/static/style.css", "/sw.js", "/login"):
        assert client.get(path).headers["cache-control"] == "no-cache", path
    vendor = client.get("/static/vendor/marked-12.0.2.min.js")  # versioned by name: may be kept
    assert vendor.status_code == 200 and "cache-control" not in vendor.headers
