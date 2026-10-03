"""Running form (running dynamics): cadence, ground contact time, vertical ratio, stride, balance.

Form changes with pace (slower running = shorter stride, longer ground contact), so
comparisons are made at matched paces:
- pace bands: medians of the seconds spent in each band, trended by month
- a cadence/stride curve against pace, recent vs earlier
- fatigue: first vs last quarter of a run (or first vs last rep), each measured
  against your own norm for the pace it was run at
- left/right balance per run, with alerts when it drifts from 50/50

Per-activity results are cached in form_stats; bump VERSION to recompute them.
"""

import json
import sqlite3
from datetime import date, timedelta
from statistics import median

from . import analysis, weather

VERSION = 1
MILE = 1609.344
MIN_SPEED = 1.5        # m/s; slower is walking or stopped
MIN_CADENCE = 130      # steps/min; lower is walking
MAX_GAP_S = 10
WARMUP_S = 300         # skip the first 5 minutes when judging fatigue
BANDS = [              # (key, label, slowest pace s/mi, fastest pace s/mi)
    ("easy", "Easy · 10:00–12:00/mi", 720, 600),
    ("steady", "Steady · 8:30–10:00/mi", 600, 510),
    ("threshold", "Threshold · 7:15–8:30/mi", 510, 435),
    ("race", "5k pace · 6:00–7:15/mi", 435, 360),
]
BIN_S = 30             # cadence curve: 30 s/mi pace bins
METRICS = ("cadence", "gct", "vr", "stride", "vo")
# Fatigue flags: how far form moves from your own norm for the pace, start vs end of a run.
GCT_FADE_MS = 10
CADENCE_FADE_SPM = 3
VR_FADE_PCT = 0.4
BALANCE_ALERT = 2.0    # % away from 50/50 on average
BALANCE_LATE_ALERT = 2.5


def _metrics(rows: list[tuple]) -> dict | None:
    """rows: (pace_s_per_mi, cadence, gct, vr, step_mm, vo_mm, balance)."""
    if not rows:
        return None
    col = lambda i: [r[i] for r in rows if r[i] is not None]  # noqa: E731
    med = lambda i: round(median(col(i)), 2) if col(i) else None  # noqa: E731
    stride = med(4)
    vo = med(5)
    return {"pace": med(0), "cadence": med(1), "gct": med(2), "vr": med(3),
            "stride": round(stride / 1000, 3) if stride else None, "vo": round(vo / 10, 2) if vo else None,
            "balance": med(6), "secs": len(rows)}


def compute(conn: sqlite3.Connection, activity_id: int) -> dict:
    samples = conn.execute(
        """SELECT t, speed_mps, cadence, gct_ms, vr_pct, step_mm, vo_mm, balance_pct FROM samples
           WHERE activity_id = ? ORDER BY t""",
        (activity_id,),
    ).fetchall()
    moving = []
    prev_t = None
    for s in samples:
        dt = min(MAX_GAP_S, s["t"] - prev_t) if prev_t is not None else 1
        prev_t = s["t"]
        if s["speed_mps"] and s["speed_mps"] >= MIN_SPEED and (s["cadence"] or 0) >= MIN_CADENCE and s["gct_ms"]:
            moving.append((s["t"], dt, (MILE / s["speed_mps"], s["cadence"], s["gct_ms"], s["vr_pct"], s["step_mm"], s["vo_mm"], s["balance_pct"])))
    out = {"version": VERSION, "has_dynamics": bool(moving), "bands": {}, "bins": {}, "quarters": None, "balance": None}
    if not moving:
        return out

    for key, _, slow, fast in BANDS:
        rows = [m[2] for m in moving if fast <= m[2][0] < slow]
        if len(rows) >= 120:
            out["bands"][key] = _metrics(rows)
    bins: dict[int, list] = {}
    for m in moving:
        pace = m[2][0]
        if 330 <= pace < 780:
            bins.setdefault(int(pace // BIN_S * BIN_S), []).append(m[2])
    out["bins"] = {str(b): _metrics(rows) for b, rows in bins.items() if len(rows) >= 60}

    run = [m for m in moving if m[0] >= WARMUP_S]
    total = sum(m[1] for m in run)
    if total >= 1200:
        acc, q1, q4 = 0.0, [], []
        for m in run:
            acc += m[1]
            if acc <= total / 4:
                q1.append(m[2])
            elif acc > total * 3 / 4:
                q4.append(m[2])
        out["quarters"] = {"first": _metrics(q1), "last": _metrics(q4)}
    balances = [m[2][6] for m in moving if m[2][6] is not None]
    if balances:
        out["balance"] = round(median(balances), 2)
    return out


def ensure(conn: sqlite3.Connection, activity_ids: list[int] | None = None) -> None:
    """Compute and cache form stats for runs that don't have current ones."""
    if activity_ids is None:
        activity_ids = [r[0] for r in conn.execute(
            """SELECT a.id FROM activities a LEFT JOIN form_stats f ON f.activity_id = a.id
               WHERE a.is_run = 1 AND a.has_samples = 1 AND (f.activity_id IS NULL OR f.version != ?)""", (VERSION,))]
    for activity_id in activity_ids:
        conn.execute("INSERT OR REPLACE INTO form_stats VALUES (?, ?, ?)",
                     (activity_id, VERSION, json.dumps(compute(conn, activity_id))))
    conn.commit()


def _stats(conn: sqlite3.Connection, since: str) -> list[tuple[sqlite3.Row, dict]]:
    rows = conn.execute(
        """SELECT a.*, f.stats_json FROM activities a JOIN form_stats f ON f.activity_id = a.id
           WHERE a.is_run = 1 AND a.start_local >= ? ORDER BY a.start_local""", (since,)).fetchall()
    return [(r, json.loads(r["stats_json"])) for r in rows]


def _weighted(items: list[dict], metric: str) -> float | None:
    pts = [(i[metric], i["secs"]) for i in items if i and i.get(metric) is not None]
    total = sum(w for _, w in pts)
    return round(sum(v * w for v, w in pts) / total, 3) if total else None


def _expected(curve: list[dict], pace: float, metric: str) -> float | None:
    """Your normal value of a metric at a pace, interpolated from your own curve."""
    pts = [(c["pace"], c[metric]) for c in curve if c.get(metric) is not None]
    if not pts:
        return None
    if pace <= pts[0][0]:
        return pts[0][1]
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if pace <= p1:
            return v0 + (v1 - v0) * (pace - p0) / (p1 - p0)
    return pts[-1][1]


def _fatigue(conn: sqlite3.Connection, row: sqlite3.Row, stats: dict, curve: list[dict]) -> dict | None:
    """Form at the start vs the end of a run, each compared with your norm for that pace.

    Quality sessions compare the first and last rep (from lap data); other runs compare
    the first and last quarter. Because both ends are measured against your own
    pace curve, slowing down or speeding up doesn't hide or fake a change in form.
    """
    kind = analysis.workout_kind(row)
    first = last = None
    method = "first vs last quarter"
    if kind in ("threshold", "intervals"):
        from . import workouts  # avoid a circular import at load time

        reps = workouts.reps(conn, row["id"])
        laps = {l["idx"]: l for l in conn.execute("SELECT * FROM laps WHERE activity_id = ?", (row["id"],))}
        rep_laps = [laps[r["lap"]] for r in reps if laps.get(r["lap"]) and laps[r["lap"]]["gct_ms"]]
        if len(rep_laps) >= 2:
            as_m = lambda x: {"pace": MILE / x["avg_speed_mps"], "cadence": x["cadence_spm"], "gct": x["gct_ms"],  # noqa: E731
                              "vr": x["vr_pct"], "balance": x["balance_pct"]}
            first, last, method = as_m(rep_laps[0]), as_m(rep_laps[-1]), "first vs last rep"
    if first is None:
        q = stats.get("quarters") or {}
        first, last = q.get("first"), q.get("last")
    if not first or not last or not first.get("pace") or not last.get("pace"):
        return None

    def drift(metric):  # change in (actual - normal for that pace)
        if first.get(metric) is None or last.get(metric) is None:
            return None
        e0, e1 = _expected(curve, first["pace"], metric), _expected(curve, last["pace"], metric)
        if e0 is None or e1 is None:
            return last[metric] - first[metric]
        return (last[metric] - e1) - (first[metric] - e0)

    d = {k: drift(k) for k in ("cadence", "gct", "vr")}
    d["balance"] = last["balance"] - first["balance"] if first.get("balance") is not None and last.get("balance") is not None else None
    signs = []
    if (d["gct"] or 0) >= GCT_FADE_MS:
        signs.append("longer ground contact")
    if (d["cadence"] or 0) <= -CADENCE_FADE_SPM:
        signs.append("lower cadence")
    if (d["vr"] or 0) >= VR_FADE_PCT:
        signs.append("more bounce per stride")
    return {"activity_id": row["id"], "date": row["start_local"][:10], "name": row["name"], "kind": kind, "method": method,
            "pace_first": first["pace"], "pace_last": last["pace"],
            "pace_change_pct": round((last["pace"] - first["pace"]) / first["pace"] * 100, 1),
            "delta": {k: round(v, 2) if v is not None else None for k, v in d.items()},
            "signs": signs, "verdict": "fading" if signs else "held"}


def report(conn: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    ensure(conn)
    stats = _stats(conn, (today - timedelta(days=550)).isoformat())
    outdoor = [(r, s) for r, s in stats if s["has_dynamics"] and weather.is_outdoor(r["type"])]

    # 1. form at matched paces, by month
    months: dict[str, dict[str, list]] = {}
    for r, s in outdoor:
        for band, m in s["bands"].items():
            months.setdefault(r["start_local"][:7], {}).setdefault(band, []).append(m)
    by_band = {key: [] for key, *_ in BANDS}
    for month in sorted(months):
        for band, items in months[month].items():
            # One odd run shouldn't define a month: need two runs (and 5+ minutes) in the band.
            if len(items) >= 2 and sum(i["secs"] for i in items) >= 300:
                by_band[band].append({"month": month, "runs": len(items), **{k: _weighted(items, k) for k in METRICS + ("pace",)}})

    # 2. cadence / stride curve vs pace: last 90 days vs the year before
    cut = (today - timedelta(days=90)).isoformat()
    def curve(pairs):
        bins: dict[str, list] = {}
        for _, s in pairs:
            for b, m in s["bins"].items():
                bins.setdefault(b, []).append(m)
        return [{"pace": int(b) + BIN_S / 2, "secs": sum(i["secs"] for i in items), **{k: _weighted(items, k) for k in METRICS}}
                for b, items in sorted(bins.items(), key=lambda x: int(x[0])) if sum(i["secs"] for i in items) >= 300]
    recent = [(r, s) for r, s in outdoor if r["start_local"] >= cut]
    year = (today - timedelta(days=455)).isoformat()
    earlier = [(r, s) for r, s in outdoor if year <= r["start_local"] < cut]
    norm = curve([(r, s) for r, s in outdoor if r["start_local"] >= year])  # your normal form at each pace

    # 3. fatigue, 4. balance
    fatigue = [f for f in (_fatigue(conn, r, s, norm) for r, s in reversed(stats) if s["has_dynamics"]) if f][:25]
    balance = []
    for r, s in stats:
        if s.get("balance") is None:
            continue
        late = ((s.get("quarters") or {}).get("last") or {}).get("balance")
        alert = abs(s["balance"] - 50) >= BALANCE_ALERT or (late is not None and abs(late - 50) >= BALANCE_LATE_ALERT)
        balance.append({"activity_id": r["id"], "date": r["start_local"][:10], "name": r["name"], "kind": analysis.workout_kind(r),
                        "balance": s["balance"], "late_balance": late, "alert": alert})
    return {
        "bands": [{"key": k, "label": label} for k, label, *_ in BANDS],
        "by_band": by_band,
        "curve": {"recent": curve(recent), "earlier": curve(earlier)},
        "fatigue": fatigue,
        "balance": balance,
        "alerts": [b for b in balance if b["alert"]],
        "thresholds": {"gct_fade_ms": GCT_FADE_MS, "cadence_fade_spm": CADENCE_FADE_SPM, "vr_fade_pct": VR_FADE_PCT,
                       "balance_alert": BALANCE_ALERT},
    }


def brief(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Compact form summary for the coach and status card (paces in s/mile, stride in m, vo in cm)."""
    r = report(conn, today)
    latest = {band: rows[-1] for band, rows in r["by_band"].items() if rows}
    year_ago = {}
    for band, rows in r["by_band"].items():
        if len(rows) >= 2:
            year_ago[band] = rows[0]
    return {
        "latest_month_by_pace_band": latest,
        "earliest_month_by_pace_band": year_ago,
        "recent_fatigue": r["fatigue"][:6],
        "balance_alerts": r["alerts"][-5:],
        "recent_balance": r["balance"][-6:],
    }
