"""Stream metrics for runs — pure functions.

Computes every derived metric that lives on the `runs` table (schema.md §4C–§4F)
from raw Strava stream arrays. Formulas, edge cases, and nullability rules are
documented in METRICS.md — this file is the implementation of that spec.

These functions have **no side effects**: no DB access, no logging, no I/O.
Input is raw stream data (lists of floats/ints) and scalar parameters. Output
is a single value, list, or dict ready to store on a `runs` row.

The orchestrator in `app/services/coaching.py` calls these in dependency order
and handles NULL sections when streams are missing (see METRICS.md §7).
"""

from __future__ import annotations

import statistics
from typing import Any

# =============================================================================
# §2 — Summary stat helpers (values derivable from the activity summary alone)
# =============================================================================


def compute_avg_pace_min_km(distance_m: float, duration_secs: float) -> float | None:
    """Average pace in minutes/km from raw distance + time.

    Returns None if distance is zero.
    """
    if distance_m <= 0 or duration_secs <= 0:
        return None
    return round((duration_secs / 60) / (distance_m / 1000), 3)


def get_stream_data(streams: dict, key: str) -> list | None:
    """Extract the `data` array for a single stream key.

    Returns None if the key is absent, the stream is empty, or the data array
    is missing. Orchestrator uses this to gate which metric sections to run.
    """
    stream = streams.get(key)
    if not stream:
        return None
    data = stream.get("data")
    if not data:
        return None
    return data


# =============================================================================
# §3 — Pace analysis (from velocity_smooth + distance + time streams)
# =============================================================================


def compute_pace_splits(
    distance_stream: list[float],
    time_stream: list[float],
) -> list[dict[str, float]]:
    """Per-km pace breakdown. See METRICS.md §3.1.

    Returns a list of `{"km": int, "pace_min": float}` entries. The trailing
    partial kilometre (e.g. last 0.47 km of a 21.47 km run) is NOT included
    — its time is captured in the second-half and avg pace stats.

    Returns [] for runs shorter than 1 km.
    """
    if not distance_stream or not time_stream:
        return []
    if len(distance_stream) != len(time_stream):
        return []
    if distance_stream[-1] < 1000:
        return []

    splits: list[dict[str, float]] = []
    last_km_idx = 0
    last_km = 0

    for i, d in enumerate(distance_stream):
        if d >= (last_km + 1) * 1000:
            time_for_this_km = time_stream[i] - time_stream[last_km_idx]
            pace_min = time_for_this_km / 60.0  # 1 km → `time_minutes` min/km
            splits.append({"km": last_km + 1, "pace_min": round(pace_min, 2)})
            last_km_idx = i
            last_km += 1

    return splits


def compute_half_paces(
    distance_stream: list[float],
    time_stream: list[float],
) -> tuple[float | None, float | None]:
    """Average pace for the first and second halves of the run, split by distance.

    Splitting by distance avoids bias when pacing varies (a positive split
    would skew a time-based half).

    Returns `(first_half_min, second_half_min)` or `(None, None)` if the run
    has no distance.
    """
    if not distance_stream or not time_stream:
        return None, None
    if len(distance_stream) != len(time_stream):
        return None, None
    if distance_stream[-1] <= 0:
        return None, None

    half_distance_m = distance_stream[-1] / 2
    midpoint_idx = next(
        (i for i, d in enumerate(distance_stream) if d >= half_distance_m),
        len(distance_stream) - 1,
    )

    first_time = time_stream[midpoint_idx] - time_stream[0]
    first_distance = distance_stream[midpoint_idx] - distance_stream[0]
    if first_distance <= 0:
        return None, None
    pace_first = (first_time / 60) / (first_distance / 1000)

    second_time = time_stream[-1] - time_stream[midpoint_idx]
    second_distance = distance_stream[-1] - distance_stream[midpoint_idx]
    if second_distance <= 0:
        return None, None
    pace_second = (second_time / 60) / (second_distance / 1000)

    return round(pace_first, 3), round(pace_second, 3)


def compute_pace_std_dev(pace_splits: list[dict[str, float]]) -> float:
    """Standard deviation of per-km pace splits in min/km.

    Returns 0.0 if there are fewer than 2 splits.
    """
    if len(pace_splits) < 2:
        return 0.0
    return round(statistics.stdev(s["pace_min"] for s in pace_splits), 3)


def compute_fastest_slowest_km(
    pace_splits: list[dict[str, float]],
) -> tuple[float | None, float | None]:
    """Min and max pace across the per-km splits.

    Returns `(fastest, slowest)` in min/km, or `(None, None)` if there are no
    splits. Note: smaller pace = faster.
    """
    if not pace_splits:
        return None, None
    paces = [s["pace_min"] for s in pace_splits]
    return min(paces), max(paces)


def _minetti_cost(grade_fraction: float) -> float:
    """Minetti's running cost polynomial.

    `grade_fraction` is the slope as a fraction (e.g. 0.05 = 5%).
    Returns metabolic cost in arbitrary units; only the ratio to the flat
    baseline matters for GAP computation.

    Reference: Minetti AE et al., J Appl Physiol 93(3):1039–1046, 2002.
    """
    g = grade_fraction
    return 155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3 + 46.3 * g**2 + 19.5 * g + 3.6


def compute_gap(
    distance_stream: list[float],
    time_stream: list[float],
    grade_stream: list[float],
) -> float | None:
    """Grade-Adjusted Pace in min/km using Minetti's cost polynomial.

    Returns None if any stream is missing or streams have mismatched lengths.
    See METRICS.md §3.5.
    """
    if not distance_stream or not time_stream or not grade_stream:
        return None
    if len(distance_stream) != len(time_stream) or len(distance_stream) != len(grade_stream):
        return None

    flat_cost = _minetti_cost(0.0)  # ≈ 3.6
    adjusted_distance_m = 0.0
    for i in range(1, len(distance_stream)):
        segment = distance_stream[i] - distance_stream[i - 1]
        grade_pct = grade_stream[i]
        cost_ratio = _minetti_cost(grade_pct / 100) / flat_cost
        adjusted_distance_m += segment * cost_ratio

    if adjusted_distance_m <= 0:
        return None

    total_time_secs = time_stream[-1] - time_stream[0]
    return round((total_time_secs / 60) / (adjusted_distance_m / 1000), 3)


# =============================================================================
# §4 — Elevation & grade (from altitude + grade_smooth streams)
# =============================================================================


def compute_grade_avg(
    altitude_stream: list[float],
    distance_stream: list[float],
) -> float:
    """Net altitude change ÷ total distance, in percent.

    Matches Strava's "Avg Grade". A flat out-and-back run will be ~0 even if
    it climbs and descends significantly — use `elevation_gain_m` (from the
    activity summary) for cumulative climb.
    """
    if not altitude_stream or not distance_stream or distance_stream[-1] == 0:
        return 0.0
    net = altitude_stream[-1] - altitude_stream[0]
    return round((net / distance_stream[-1]) * 100, 2)


def compute_grade_distances(
    distance_stream: list[float],
    grade_stream: list[float],
) -> tuple[float, float, float]:
    """Distance in three grade buckets: flat, uphill, downhill.

    Buckets:
        flat: |grade| < 1%
        uphill: grade > 2%
        downhill: grade < -2%
        (the 1–2% band is "neutral" and excluded from all three)

    The returned values will NOT sum to `distance_stream[-1]` — the difference
    is the neutral band.
    """
    if not grade_stream or not distance_stream:
        return 0.0, 0.0, 0.0
    if len(distance_stream) != len(grade_stream):
        return 0.0, 0.0, 0.0

    flat = 0.0
    up = 0.0
    down = 0.0
    for i in range(1, len(distance_stream)):
        seg = distance_stream[i] - distance_stream[i - 1]
        grade = grade_stream[i]
        if abs(grade) < 1.0:
            flat += seg
        elif grade > 2.0:
            up += seg
        elif grade < -2.0:
            down += seg
    return round(flat, 1), round(up, 1), round(down, 1)


def compute_grade_splits(
    distance_stream: list[float],
    grade_stream: list[float],
) -> list[dict[str, float]]:
    """Per-km average grade. Returns a list of `{"km": int, "grade": float}` entries."""
    if not distance_stream or not grade_stream:
        return []
    if len(distance_stream) != len(grade_stream):
        return []
    if distance_stream[-1] < 1000:
        return []

    splits: list[dict[str, float]] = []
    last_km_idx = 0
    last_km = 0

    for i, d in enumerate(distance_stream):
        if d >= (last_km + 1) * 1000:
            window = grade_stream[last_km_idx : i + 1]
            avg = sum(window) / len(window) if window else 0.0
            splits.append({"km": last_km + 1, "grade": round(avg, 1)})
            last_km_idx = i
            last_km += 1

    return splits


# =============================================================================
# §5 — Heart rate (from heartrate stream)
# =============================================================================


def compute_hr_zones(
    time_stream: list[float],
    hr_stream: list[float],
    zone_bounds: tuple[int, int, int, int],
) -> list[int]:
    """Seconds spent in each HR zone.

    `zone_bounds` is the inclusive upper bound of each zone:
        (z1_max, z2_max, z3_max, z4_max)
    Anything above z4_max is Z5.

    Sensor dropouts (`hr == 0`) are skipped, not accumulated into Z1.

    Returns a list of 5 integers: `[z1_secs, z2_secs, z3_secs, z4_secs, z5_secs]`.
    """
    zones = [0.0, 0.0, 0.0, 0.0, 0.0]
    if not hr_stream or not time_stream or len(hr_stream) != len(time_stream):
        return [0, 0, 0, 0, 0]

    z1, z2, z3, z4 = zone_bounds
    for i in range(1, len(time_stream)):
        hr = hr_stream[i]
        if hr <= 0:
            continue  # sensor dropout
        dt = time_stream[i] - time_stream[i - 1]
        if hr <= z1:
            zones[0] += dt
        elif hr <= z2:
            zones[1] += dt
        elif hr <= z3:
            zones[2] += dt
        elif hr <= z4:
            zones[3] += dt
        else:
            zones[4] += dt

    return [round(z) for z in zones]


def compute_cardiac_drift(
    time_stream: list[float],
    hr_stream: list[float],
) -> float | None:
    """HR drift: mean HR in the last 10 min minus mean HR in the first 10 min
    (after a 5 min warmup is excluded).

    Returns None for runs shorter than 25 minutes (5 warmup + 10 + 10). See
    METRICS.md §5.2.
    """
    if not hr_stream or not time_stream or len(hr_stream) != len(time_stream):
        return None

    duration = time_stream[-1] - time_stream[0]
    if duration < 25 * 60:
        return None

    warmup_end = 5 * 60
    first_window_end = warmup_end + 10 * 60
    last_window_start = duration - 10 * 60

    first_hrs = [
        hr_stream[i]
        for i in range(len(time_stream))
        if warmup_end <= time_stream[i] <= first_window_end and hr_stream[i] > 0
    ]
    last_hrs = [
        hr_stream[i]
        for i in range(len(time_stream))
        if time_stream[i] >= last_window_start and hr_stream[i] > 0
    ]

    if not first_hrs or not last_hrs:
        return None
    return round(statistics.mean(last_hrs) - statistics.mean(first_hrs), 1)


def compute_aerobic_decoupling(
    time_stream: list[float],
    hr_stream: list[float],
    velocity_stream: list[float],
) -> float | None:
    """Friel's aerobic decoupling: percentage change in speed:HR ratio between
    the first and second halves of the run (split by time, not distance).

    Positive = HR climbed while pace stayed the same → fatigue signal.
    Returns None for runs shorter than 20 minutes. See METRICS.md §5.3.
    """
    if not hr_stream or not velocity_stream or not time_stream:
        return None
    if len(hr_stream) != len(time_stream) or len(velocity_stream) != len(time_stream):
        return None

    duration = time_stream[-1] - time_stream[0]
    if duration < 20 * 60:
        return None

    midpoint_secs = time_stream[0] + duration / 2
    midpoint_idx = next(
        (i for i, t in enumerate(time_stream) if t >= midpoint_secs),
        len(time_stream) // 2,
    )

    def _avg(stream: list[float], lo: int, hi: int) -> float | None:
        sub = [stream[i] for i in range(lo, hi) if stream[i] > 0]
        return statistics.mean(sub) if sub else None

    speed_1 = _avg(velocity_stream, 0, midpoint_idx)
    hr_1 = _avg(hr_stream, 0, midpoint_idx)
    speed_2 = _avg(velocity_stream, midpoint_idx, len(time_stream))
    hr_2 = _avg(hr_stream, midpoint_idx, len(time_stream))

    if speed_1 is None or hr_1 is None or speed_2 is None or hr_2 is None:
        return None
    if hr_1 == 0 or hr_2 == 0:
        return None

    ef_1 = speed_1 / hr_1
    ef_2 = speed_2 / hr_2
    if ef_1 == 0:
        return None
    return round(((ef_1 - ef_2) / ef_1) * 100, 2)


def compute_efficiency_factor(
    avg_speed_ms: float,
    avg_hr: float | None,
) -> float | None:
    """Friel's efficiency factor: avg speed ÷ avg HR.

    A long-term fitness tracker — watch the trend, not the absolute value.
    Higher EF over time at the same HR = improving aerobic fitness.
    """
    if not avg_hr or avg_hr <= 0:
        return None
    return round(avg_speed_ms / avg_hr, 4)


def compute_hr_vs_pace(
    distance_stream: list[float],
    time_stream: list[float],
    hr_stream: list[float],
    pace_splits: list[dict[str, float]],
) -> list[dict[str, Any]]:
    """Per-km `{km, hr, pace_min}` list for the HR/pace chart.

    Joins `pace_splits` (computed earlier) with the per-km avg HR over the
    same distance boundaries.
    """
    if not pace_splits or not hr_stream or not distance_stream or not time_stream:
        return []
    if len(hr_stream) != len(distance_stream):
        return []

    out: list[dict[str, Any]] = []
    last_km_idx = 0

    for split in pace_splits:
        target = split["km"] * 1000
        i = next(
            (j for j, d in enumerate(distance_stream) if d >= target),
            len(distance_stream) - 1,
        )
        hrs = [hr_stream[k] for k in range(last_km_idx, i + 1) if hr_stream[k] > 0]
        avg_hr = round(statistics.mean(hrs)) if hrs else None
        out.append({"km": split["km"], "hr": avg_hr, "pace_min": split["pace_min"]})
        last_km_idx = i

    return out


# =============================================================================
# §6 — Cadence (from cadence stream)
# =============================================================================

# Strava reports cadence as SINGLE-foot per minute for runs. Multiply by 2 to
# get the runner-standard "steps per minute" (spm).
CADENCE_MULTIPLIER = 2


def compute_cadence_avg(cadence_stream: list[float]) -> int | None:
    """Average cadence in spm (steps per minute, both feet).

    Returns None if the stream is empty or contains only sensor dropouts.
    """
    if not cadence_stream:
        return None
    samples = [c for c in cadence_stream if c > 0]
    if not samples:
        return None
    return round(statistics.mean(samples) * CADENCE_MULTIPLIER)


def compute_cadence_std_dev(cadence_stream: list[float]) -> float:
    """Standard deviation of cadence in spm.

    Returns 0.0 if there are fewer than 2 samples.
    """
    if not cadence_stream:
        return 0.0
    samples = [c * CADENCE_MULTIPLIER for c in cadence_stream if c > 0]
    if len(samples) < 2:
        return 0.0
    return round(statistics.stdev(samples), 1)


def compute_cadence_under170_pct(
    time_stream: list[float],
    cadence_stream: list[float],
) -> float | None:
    """Percentage of run time spent at cadence < 170 spm (overstriding threshold)."""
    if not cadence_stream or not time_stream or len(cadence_stream) != len(time_stream):
        return None

    total_secs = 0.0
    under_secs = 0.0
    for i in range(1, len(time_stream)):
        c = cadence_stream[i] * CADENCE_MULTIPLIER
        if c == 0:
            continue
        dt = time_stream[i] - time_stream[i - 1]
        total_secs += dt
        if c < 170:
            under_secs += dt

    if total_secs == 0:
        return None
    return round((under_secs / total_secs) * 100, 1)


def compute_cadence_splits(
    distance_stream: list[float],
    cadence_stream: list[float],
) -> list[dict[str, int]]:
    """Per-km average cadence in spm."""
    if not distance_stream or not cadence_stream:
        return []
    if len(distance_stream) != len(cadence_stream):
        return []
    if distance_stream[-1] < 1000:
        return []

    splits: list[dict[str, int]] = []
    last_km_idx = 0
    last_km = 0

    for i, d in enumerate(distance_stream):
        if d >= (last_km + 1) * 1000:
            window = [c * CADENCE_MULTIPLIER for c in cadence_stream[last_km_idx : i + 1] if c > 0]
            avg = round(sum(window) / len(window)) if window else 0
            splits.append({"km": last_km + 1, "cadence": avg})
            last_km_idx = i
            last_km += 1

    return splits
