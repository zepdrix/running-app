"""Generate a year of plausible synthetic runs so the dashboard can be tried without Garmin."""

import random
from datetime import date, datetime, timedelta

from . import store
from .streams import Lap, Sample

MAX_HR = 190
ZONE_BOUNDS = [0.6, 0.7, 0.8, 0.9]  # fraction of max HR where zones 2-5 begin


def _zone(hr: float) -> int:
    return sum(hr >= b * MAX_HR for b in ZONE_BOUNDS)


def _run(rng: random.Random, km: float, pace_s: float, kind: str, fitness: float):
    """Simulate a run sampled every 5 seconds; returns samples and laps."""
    samples, t, d = [], 0.0, 0.0
    target = km * 1000
    while d < target:
        p = pace_s
        if kind == "tempo" and 0.25 < d / target < 0.8:
            p = pace_s * 0.85
        if kind == "intervals" and 0.2 < d / target < 0.8 and int(d // 400) % 2 == 0:
            p = pace_s * 0.78
        speed = 1000 / p * rng.uniform(0.96, 1.04)
        effort = (1000 / speed) / pace_s  # <1 means faster than easy pace
        hr = 128 + (1 - effort) * 160 + min(t / 60, 60) * 0.15 - fitness * 6 + rng.uniform(-2, 2)
        samples.append(Sample(t=t, distance_m=d, hr=hr, speed_mps=speed, cadence=170 + rng.uniform(-4, 6)))
        t += 5
        d += speed * 5
    laps = []
    for k in range(int(km)):
        seg = [s for s in samples if k * 1000 <= s.distance_m < (k + 1) * 1000]
        if seg:
            laps.append(
                Lap(1000, seg[-1].t - seg[0].t + 5, sum(s.hr for s in seg) / len(seg), 1000 / (seg[-1].t - seg[0].t + 5))
            )
    return samples, laps


def generate(conn, days: int = 365, seed: int = 7) -> int:
    rng = random.Random(seed)
    today = date.today()
    start = today - timedelta(days=days)
    activity_id = 10_000_000_000
    count = 0
    for offset in range(days + 1):
        day = start + timedelta(days=offset)
        progress = offset / days
        fitness = progress
        weekday = day.weekday()
        plan = {
            1: ("intervals", 8),
            3: ("tempo", 10),
            5: ("easy", 6),
            6: ("long", 14 + 10 * progress),
            0: ("easy", 7) if progress > 0.4 else None,
        }.get(weekday)
        if not plan or rng.random() < 0.1:
            continue
        kind, km = plan
        km = round(km * rng.uniform(0.9, 1.1), 1)
        easy_pace = 375 - 45 * fitness  # 6:15/km improving to 5:30/km
        pace = easy_pace * (1.04 if kind == "long" else 1.0)
        # A few races sprinkled in.
        if weekday == 5 and offset % 70 == 0:
            kind, km, pace = "race", 10, easy_pace * 0.8

        samples, laps = _run(rng, km, pace, kind, fitness)
        zones = [0.0] * 5
        for s in samples:
            zones[_zone(s.hr)] += 5
        duration = samples[-1].t + 5
        dist = samples[-1].distance_m
        avg_hr = sum(s.hr for s in samples) / len(samples)
        activity_id += 1
        started = datetime.combine(day, datetime.min.time()) + timedelta(hours=7, minutes=rng.randint(0, 50))
        summary = {
            "activityId": activity_id,
            "activityName": {"race": "10K race", "long": "Long run", "tempo": "Tempo run",
                             "intervals": "400m repeats", "easy": "Easy run"}[kind],
            "activityType": {"typeKey": "running"},
            "eventType": {"typeKey": "race" if kind == "race" else "uncategorized"},
            "startTimeLocal": started.strftime("%Y-%m-%d %H:%M:%S"),
            "startTimeGMT": started.strftime("%Y-%m-%d %H:%M:%S"),
            "distance": dist,
            "duration": duration,
            "movingDuration": duration,
            "elevationGain": rng.uniform(10, 120),
            "averageSpeed": dist / duration,
            "averageHR": avg_hr,
            "maxHR": max(s.hr for s in samples),
            "averageRunningCadenceInStepsPerMinute": 172,
            "activityTrainingLoad": duration / 60 * (avg_hr / 140) ** 3,
            "vO2MaxValue": round(44 + 5 * fitness) if kind in ("tempo", "race") else None,
            **{f"hrTimeInZone_{i + 1}": zones[i] for i in range(5)},
        }
        store.upsert_summary(conn, summary)
        store.store_detail(conn, activity_id, samples, laps)
        count += 1

    conn.execute("DELETE FROM goals")
    conn.executemany(
        "INSERT INTO goals (kind, label, distance_m, target, target_date) VALUES (?, ?, ?, ?, ?)",
        [
            ("race", "Half marathon PR", 21097.5, 1 * 3600 + 55 * 60, (today + timedelta(days=60)).isoformat()),
            ("weekly_distance", "Weekly volume", None, 40000, None),
            ("easy_hr_share", "80/20 easy running", None, 0.8, None),
        ],
    )
    conn.commit()
    return count
