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
    assert "session expired" in error and server._sync_state == {"running": False, "message": error}

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
