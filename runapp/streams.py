"""Turn Garmin activity files into time-series samples, and find best efforts in them."""

import io
import zipfile
from dataclasses import dataclass

import fitdecode

SEMICIRCLE_TO_DEG = 180 / 2**31


@dataclass
class Sample:
    t: float
    distance_m: float | None = None
    hr: float | None = None
    speed_mps: float | None = None
    cadence: float | None = None
    altitude_m: float | None = None
    lat: float | None = None
    lon: float | None = None
    # running dynamics (watch, chest strap or footpod)
    gct_ms: float | None = None        # ground contact time
    vo_mm: float | None = None         # vertical oscillation
    vr_pct: float | None = None        # vertical ratio
    step_mm: float | None = None       # step length
    balance_pct: float | None = None   # ground contact balance, % left
    power_w: float | None = None


@dataclass
class Lap:
    distance_m: float | None
    duration_s: float | None
    avg_hr: float | None
    avg_speed_mps: float | None
    cadence_spm: float | None = None
    gct_ms: float | None = None
    vo_mm: float | None = None
    vr_pct: float | None = None
    step_mm: float | None = None
    balance_pct: float | None = None
    power_w: float | None = None


def _spm(whole, fraction):
    """FIT stores running cadence per leg (plus a fraction); steps per minute is twice that."""
    if whole is None:
        return None
    return (whole + (fraction or 0)) * 2


def fit_bytes_from_zip(blob: bytes) -> bytes | None:
    """Garmin's "original" download is a zip holding a single .fit file."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".fit"):
                    return zf.read(name)
    except zipfile.BadZipFile:
        # Some endpoints return the bare FIT file.
        if blob[8:12] == b".FIT":
            return blob
    return None


def _first(frame, *names):
    for name in names:
        value = frame.get_value(name, fallback=None)
        if value is not None:
            return value
    return None


def parse_fit(fit_bytes: bytes) -> tuple[list[Sample], list[Lap]]:
    samples: list[Sample] = []
    laps: list[Lap] = []
    t0 = None
    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            if frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                t0 = t0 or ts
                lat = _first(frame, "position_lat")
                lon = _first(frame, "position_long")
                samples.append(
                    Sample(
                        t=(ts - t0).total_seconds(),
                        distance_m=_first(frame, "distance"),
                        hr=_first(frame, "heart_rate"),
                        speed_mps=_first(frame, "enhanced_speed", "speed"),
                        cadence=_spm(_first(frame, "cadence"), _first(frame, "fractional_cadence")),
                        altitude_m=_first(frame, "enhanced_altitude", "altitude"),
                        lat=lat * SEMICIRCLE_TO_DEG if lat is not None else None,
                        lon=lon * SEMICIRCLE_TO_DEG if lon is not None else None,
                        gct_ms=_first(frame, "stance_time"),
                        vo_mm=_first(frame, "vertical_oscillation"),
                        vr_pct=_first(frame, "vertical_ratio"),
                        step_mm=_first(frame, "step_length"),
                        balance_pct=_first(frame, "stance_time_balance"),
                        power_w=_first(frame, "power"),
                    )
                )
            elif frame.name == "lap":
                laps.append(
                    Lap(
                        distance_m=_first(frame, "total_distance"),
                        duration_s=_first(frame, "total_timer_time", "total_elapsed_time"),
                        avg_hr=_first(frame, "avg_heart_rate"),
                        avg_speed_mps=_first(frame, "enhanced_avg_speed", "avg_speed"),
                        cadence_spm=_spm(_first(frame, "avg_running_cadence", "avg_cadence"), _first(frame, "avg_fractional_cadence")),
                        gct_ms=_first(frame, "avg_stance_time"),
                        vo_mm=_first(frame, "avg_vertical_oscillation"),
                        vr_pct=_first(frame, "avg_vertical_ratio"),
                        step_mm=_first(frame, "avg_step_length"),
                        balance_pct=_first(frame, "avg_stance_time_balance"),
                        power_w=_first(frame, "avg_power"),
                    )
                )
    return samples, laps


def samples_from_details(details: dict) -> list[Sample]:
    """Fallback when no FIT file is available: Garmin's /details chart data."""
    descriptors = {d["key"]: d["metricsIndex"] for d in details.get("metricDescriptors") or []}
    rows = details.get("activityDetailMetrics") or []
    if "directTimestamp" not in descriptors or not rows:
        return []

    def col(metrics, key):
        i = descriptors.get(key)
        return metrics[i] if i is not None and i < len(metrics) else None

    samples = []
    t0 = None
    for row in rows:
        m = row.get("metrics") or []
        ts = col(m, "directTimestamp")
        if ts is None:
            continue
        t0 = t0 if t0 is not None else ts
        samples.append(
            Sample(
                t=(ts - t0) / 1000,
                distance_m=col(m, "sumDistance"),
                hr=col(m, "directHeartRate"),
                speed_mps=col(m, "directSpeed"),
                cadence=col(m, "directRunCadence") or col(m, "directDoubleCadence"),
                altitude_m=col(m, "directElevation"),
                lat=col(m, "directLatitude"),
                lon=col(m, "directLongitude"),
            )
        )
    return samples


def best_efforts(samples: list[Sample], distances: dict[str, float]) -> dict[str, float]:
    """Fastest elapsed time (s) covering each distance anywhere in the run.

    Two-pointer sweep over the cumulative distance stream, interpolating the
    start point so the window covers exactly the target distance.
    """
    pts = [(s.t, s.distance_m) for s in samples if s.distance_m is not None]
    # Distance must be non-decreasing; drop glitches where it goes backwards.
    clean: list[tuple[float, float]] = []
    for t, d in pts:
        if not clean or (d >= clean[-1][1] and t > clean[-1][0]):
            clean.append((t, d))
    if len(clean) < 2:
        return {}

    total = clean[-1][1] - clean[0][1]
    results = {}
    for label, target in distances.items():
        if total < target:
            continue
        best = None
        i = 0
        for j in range(1, len(clean)):
            tj, dj = clean[j]
            # Advance i while the window starting at i+1 still covers the target.
            while i + 1 < j and dj - clean[i + 1][1] >= target:
                i += 1
            ti, di = clean[i]
            if dj - di < target:
                continue
            # Slide the start forward within segment [i, i+1] so the window is exactly target.
            t_next, d_next = clean[i + 1]
            start_d = dj - target
            if d_next > di:
                frac = (start_d - di) / (d_next - di)
                start_t = ti + frac * (t_next - ti)
            else:
                start_t = ti
            elapsed = tj - start_t
            if best is None or elapsed < best:
                best = elapsed
        if best is not None:
            results[label] = best
    return results
