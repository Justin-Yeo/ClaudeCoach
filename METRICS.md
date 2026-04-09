# ClaudeCoach — Stream Metrics

**Formulas, edge cases, and nullability rules for every derived metric stored on the `runs` table.**

This doc is the canonical source for what `app/services/metrics.py` must compute. Implement against this spec; tests in `tests/test_metrics.py` should pin the formulas with fixture-based round-trips against saved Strava activities.

---

## 1. Overview

Strava gives us two things per activity:

1. **Summary stats** — single values per activity (`distance`, `moving_time`, `average_heartrate`, etc.) from `GET /activities/{id}`. No computation needed; copy them straight into the matching `runs` columns.
2. **Streams** — time-series arrays (one value per second of the activity) from `GET /activities/{id}/streams`. The metrics in this doc are computed from these streams.

The streams we request:

```
keys = time, distance, heartrate, velocity_smooth,
       altitude, cadence, grade_smooth
```

All streams are arrays of equal length; index `i` represents the same point in time across all streams. `time[i]` is elapsed seconds since the run started. `distance[i]` is cumulative metres. `velocity_smooth[i]` is metres per second.

**Stream availability** is not guaranteed:
- `time`, `distance`, `velocity_smooth`, `altitude`, `grade_smooth` — always present
- `heartrate` — present if the user wore a HR-capable device and synced via a method that preserves the stream (see schema.md §2.1)
- `cadence` — present on Garmin reliably; unreliable on Apple Watch

If a stream is absent, every metric derived from it must be set to `NULL` and the corresponding section is omitted from the Claude prompt.

---

## 2. Pure Summary Stats `[no computation]`

These are copied directly from the `GET /activities/{id}` JSON response. No stream processing.

| Column | Source field | Notes |
|---|---|---|
| `distance_m` | `distance` | metres |
| `duration_secs` | `moving_time` | excludes auto-pauses |
| `elapsed_secs` | `elapsed_time` | wall clock |
| `elevation_gain_m` | `total_elevation_gain` | total ascent across the run |
| `elevation_high_m` | `elev_high` | highest altitude reached |
| `elevation_low_m` | `elev_low` | lowest altitude reached |
| `avg_speed_ms` | `average_speed` | m/s |
| `max_speed_ms` | `max_speed` | m/s, peak instantaneous |
| `avg_hr` | `average_heartrate` | bpm, NULL if no HR stream |
| `max_hr` | `max_heartrate` | bpm, NULL if no HR stream |
| `workout_type` | `workout_type` | Strava enum: 0=default, 1=race, 2=long, 3=workout |
| `start_date` | `start_date_local` | local time |
| `timezone` | `timezone` | IANA name |

**Computed from summary alone**:

| Column | Formula |
|---|---|
| `avg_pace_min_km` | `(duration_secs / 60) / (distance_m / 1000)` — total minutes ÷ total km |

---

## 3. Pace Analysis `[from velocity_smooth + distance + time streams]`

### 3.1 `pace_splits_json`

Per-kilometre pace breakdown.

**Algorithm**:

```python
def compute_pace_splits(distance_stream, time_stream):
    splits = []
    last_km_idx = 0
    last_km = 0
    for i in range(len(distance_stream)):
        if distance_stream[i] >= (last_km + 1) * 1000:
            time_for_this_km = time_stream[i] - time_stream[last_km_idx]
            pace_min = time_for_this_km / 60.0  # 1km / minutes = min/km
            splits.append({"km": last_km + 1, "pace_min": round(pace_min, 2)})
            last_km_idx = i
            last_km += 1
    return splits
```

**Edge cases**:
- The trailing partial kilometre (e.g. the last 0.4 km of a 5.4 km run) is **not** included as its own split — it's captured in the avg pace and the second-half stats but shows no entry in `pace_splits_json`.
- Runs shorter than 1 km return an empty array `[]`.

**JSON shape**: `[{"km": 1, "pace_min": 5.20}, {"km": 2, "pace_min": 5.15}, ...]`

### 3.2 `pace_first_half_min` and `pace_second_half_min`

Average pace for the first and second halves of the run, **split by distance**, not by time. Splitting by distance avoids bias when pacing varies (a positive split would skew a time-based half).

**Algorithm**:

```python
def compute_half_paces(distance_stream, time_stream):
    half_distance_m = distance_stream[-1] / 2
    midpoint_idx = next(i for i, d in enumerate(distance_stream) if d >= half_distance_m)

    first_half_time_secs = time_stream[midpoint_idx] - time_stream[0]
    first_half_distance_m = distance_stream[midpoint_idx] - distance_stream[0]
    pace_first = (first_half_time_secs / 60) / (first_half_distance_m / 1000)

    second_half_time_secs = time_stream[-1] - time_stream[midpoint_idx]
    second_half_distance_m = distance_stream[-1] - distance_stream[midpoint_idx]
    pace_second = (second_half_time_secs / 60) / (second_half_distance_m / 1000)

    return pace_first, pace_second
```

**Interpretation**: `pace_second_half_min > pace_first_half_min` means a positive split (slower in the second half — typical fade or fatigue). `<` means a negative split (got stronger).

### 3.3 `pace_std_dev_min`

Standard deviation of the per-km pace splits. Measures pace consistency.

**Algorithm**:

```python
import statistics

def compute_pace_std_dev(pace_splits):
    if len(pace_splits) < 2:
        return 0.0
    return statistics.stdev(s["pace_min"] for s in pace_splits)
```

**Interpretation**:
- `< 0.15 min/km` (~9 sec/km) — very consistent pacing
- `0.15 – 0.30` — typical run with mild surges or fade
- `> 0.30` — interval session, very hilly terrain, or erratic pacing

### 3.4 `fastest_km_pace_min` and `slowest_km_pace_min`

`min(pace_min)` and `max(pace_min)` over `pace_splits_json`. NULL if `pace_splits_json` is empty (run < 1 km).

### 3.5 `gap_min_km` — Grade-Adjusted Pace

Flat-equivalent pace. Tells us "if this run had been on flat ground, how fast would the same effort have produced?". Allows comparing hilly easy runs against flat easy runs at the same effort level.

**Method**: Minetti's running cost equation. For each sample, compute the energy cost ratio relative to flat ground, then sum the grade-adjusted distance.

**Minetti's cost-of-running polynomial** (for grade `g` as a fraction, e.g. `0.05` = 5% grade):

```
C_r(g) = 155.4·g⁵ − 30.4·g⁴ − 43.3·g³ + 46.3·g² + 19.5·g + 3.6
```

`C_r` is metabolic cost in J/kg/m. `C_r(0) ≈ 3.6` is the flat baseline.

**Algorithm**:

```python
def minetti_cost(grade_fraction: float) -> float:
    g = grade_fraction
    return 155.4*g**5 - 30.4*g**4 - 43.3*g**3 + 46.3*g**2 + 19.5*g + 3.6

def compute_gap(distance_stream, time_stream, grade_stream):
    flat_cost = minetti_cost(0)  # ~3.6
    adjusted_distance_m = 0.0
    for i in range(1, len(distance_stream)):
        segment_distance = distance_stream[i] - distance_stream[i-1]
        grade_pct = grade_stream[i]
        cost_ratio = minetti_cost(grade_pct / 100) / flat_cost
        adjusted_distance_m += segment_distance * cost_ratio

    total_time_secs = time_stream[-1] - time_stream[0]
    if adjusted_distance_m == 0:
        return None
    return (total_time_secs / 60) / (adjusted_distance_m / 1000)
```

**NULL conditions**: if `grade_smooth` stream is missing entirely (rare — Strava computes it from altitude when available), return NULL.

**Reference**: Minetti AE et al. "Energy cost of walking and running at extreme uphill and downhill slopes." J Appl Physiol 93(3):1039–1046, 2002.

---

## 4. Elevation & Grade `[from altitude + grade_smooth streams]`

### 4.1 `grade_avg_pct`

Average gradient across the run. Defined as **net altitude change ÷ total distance**, expressed as a percentage. Matches what Strava displays.

```python
def compute_grade_avg(altitude_stream, distance_stream):
    net_altitude_change = altitude_stream[-1] - altitude_stream[0]
    total_distance = distance_stream[-1]
    if total_distance == 0:
        return 0.0
    return (net_altitude_change / total_distance) * 100
```

A flat out-and-back run will have `grade_avg_pct ≈ 0` even if it climbed and descended significantly. Use `elevation_gain_m` for cumulative climb.

### 4.2 `flat_distance_m`, `uphill_distance_m`, `downhill_distance_m`

Distance covered in three grade buckets, computed from `grade_smooth`. The bands deliberately leave a "neutral" band between flat and up/down so that the up/down counts represent meaningful hills, not undulations.

**Buckets**:
- **flat**: `|grade| < 1%`
- **uphill**: `grade > 2%`
- **downhill**: `grade < -2%`
- **neutral** (between 1% and 2% in either direction): not counted in any of the three columns

The three columns will not sum to `distance_m` — the difference is the neutral band.

**Algorithm**:

```python
def compute_grade_distances(distance_stream, grade_stream):
    flat = up = down = 0.0
    for i in range(1, len(distance_stream)):
        seg = distance_stream[i] - distance_stream[i-1]
        grade = grade_stream[i]
        if abs(grade) < 1.0:
            flat += seg
        elif grade > 2.0:
            up += seg
        elif grade < -2.0:
            down += seg
        # else: neutral, skip
    return flat, up, down
```

### 4.3 `grade_splits_json`

Per-km grade. Average grade over each km segment.

**JSON shape**: `[{"km": 1, "grade": 2.3}, {"km": 2, "grade": -0.5}, ...]`

**Algorithm**:

```python
def compute_grade_splits(distance_stream, grade_stream):
    splits = []
    last_km_idx = 0
    last_km = 0
    for i in range(len(distance_stream)):
        if distance_stream[i] >= (last_km + 1) * 1000:
            grade_samples = grade_stream[last_km_idx:i+1]
            avg_grade = sum(grade_samples) / len(grade_samples)
            splits.append({"km": last_km + 1, "grade": round(avg_grade, 1)})
            last_km_idx = i
            last_km += 1
    return splits
```

---

## 5. Heart Rate `[from heartrate stream]` `[P2 — nullable]`

**All metrics in this section are NULL if the `heartrate` stream is absent.** The presence check is `run.avg_hr is not None` (since the summary value is also derived from the stream).

User-specific HR zones come from `users.hr_zone1_max` ... `users.hr_zone4_max`. Defaults: 60% / 70% / 80% / 90% of `users.max_hr`. See [spec.md §11.17](spec.md).

### 5.1 `hr_zone1_secs` ... `hr_zone5_secs`

Time spent in each zone, in seconds.

**Algorithm**:

```python
def compute_hr_zones(time_stream, hr_stream, user):
    zones = [0, 0, 0, 0, 0]  # Z1, Z2, Z3, Z4, Z5
    bounds = [
        user.hr_zone1_max,
        user.hr_zone2_max,
        user.hr_zone3_max,
        user.hr_zone4_max,
    ]
    for i in range(1, len(time_stream)):
        dt = time_stream[i] - time_stream[i-1]
        hr = hr_stream[i]
        if hr <= bounds[0]:
            zones[0] += dt
        elif hr <= bounds[1]:
            zones[1] += dt
        elif hr <= bounds[2]:
            zones[2] += dt
        elif hr <= bounds[3]:
            zones[3] += dt
        else:
            zones[4] += dt
    return zones  # [z1_secs, z2_secs, z3_secs, z4_secs, z5_secs]
```

**Edge case**: if `hr[i]` is `0` (sensor dropout), skip that interval (`continue`). Don't accumulate it into Z1.

### 5.2 `cardiac_drift_bpm`

How much the heart rate rose from the start of the run to the end, controlling for warmup. A common indicator of fatigue, dehydration, or overheating.

**Definition**: mean HR in the last 10 minutes minus mean HR in the first 10 minutes after a 5-minute warmup is excluded.

**Algorithm**:

```python
def compute_cardiac_drift(time_stream, hr_stream):
    duration_secs = time_stream[-1] - time_stream[0]
    if duration_secs < 25 * 60:  # need at least 25 min: 5 warmup + 10 first + 10 last
        return None

    warmup_end = 5 * 60
    first_window_end = warmup_end + 10 * 60
    last_window_start = duration_secs - 10 * 60

    first_hrs = [hr_stream[i] for i in range(len(time_stream))
                 if warmup_end <= time_stream[i] <= first_window_end and hr_stream[i] > 0]
    last_hrs = [hr_stream[i] for i in range(len(time_stream))
                if time_stream[i] >= last_window_start and hr_stream[i] > 0]

    if not first_hrs or not last_hrs:
        return None
    return round(statistics.mean(last_hrs) - statistics.mean(first_hrs), 1)
```

**Interpretation**:
- `< 3 bpm` — no meaningful drift
- `3 – 6 bpm` — mild drift, expected on long runs
- `> 6 bpm` — significant drift, signal of fatigue / heat / dehydration / overreaching
- `> 10 bpm` — major drift, flag for review

### 5.3 `aerobic_decoupling`

Joel Friel's measure of aerobic efficiency change between the first and second halves of a run. Positive decoupling means HR climbed while pace stayed constant — a fatigue signal. Negative means the runner got more efficient.

**Definition**:

```
ef_first  = avg_speed_first_half_ms  / avg_hr_first_half
ef_second = avg_speed_second_half_ms / avg_hr_second_half
decoupling_pct = ((ef_first - ef_second) / ef_first) * 100
```

Splits are by **time**, not distance, for this metric (Friel's convention).

**Algorithm**:

```python
def compute_aerobic_decoupling(time_stream, hr_stream, velocity_stream):
    duration_secs = time_stream[-1] - time_stream[0]
    if duration_secs < 20 * 60:  # need at least 20 min for the metric to be meaningful
        return None

    midpoint_secs = time_stream[0] + duration_secs / 2
    midpoint_idx = next(i for i, t in enumerate(time_stream) if t >= midpoint_secs)

    def avg(stream, lo, hi):
        sub = [stream[i] for i in range(lo, hi) if stream[i] > 0]
        return statistics.mean(sub) if sub else None

    avg_speed_1 = avg(velocity_stream, 0, midpoint_idx)
    avg_hr_1    = avg(hr_stream,       0, midpoint_idx)
    avg_speed_2 = avg(velocity_stream, midpoint_idx, len(time_stream))
    avg_hr_2    = avg(hr_stream,       midpoint_idx, len(time_stream))

    if not all([avg_speed_1, avg_hr_1, avg_speed_2, avg_hr_2]):
        return None

    ef_1 = avg_speed_1 / avg_hr_1
    ef_2 = avg_speed_2 / avg_hr_2
    return round(((ef_1 - ef_2) / ef_1) * 100, 2)
```

**Interpretation**:
- `< 5%` — well-conditioned aerobic system, no fatigue signal
- `5 – 10%` — moderate drift, usually OK on long runs
- `> 10%` — significant decoupling, flag in the coaching message

**Reference**: Joel Friel, *The Triathlete's Training Bible*, 4th ed., Chapter on aerobic threshold testing.

### 5.4 `efficiency_factor`

A long-term aerobic fitness tracker. Watch the trend over weeks, not the absolute value.

**Definition**:

```
efficiency_factor = avg_speed_ms / avg_hr
```

Higher EF over time at the same HR = improving aerobic fitness.

**Algorithm**:

```python
def compute_efficiency_factor(avg_speed_ms, avg_hr):
    if not avg_hr or avg_hr == 0:
        return None
    return round(avg_speed_ms / avg_hr, 4)
```

Stored as a float with 4 decimal places (typical values: 0.015 – 0.030).

### 5.5 `hr_vs_pace_json`

Per-km arrays of HR and pace, for the per-km HR/pace chart that Claude can reason over.

**JSON shape**: `[{"km": 1, "hr": 145, "pace_min": 5.33}, ...]`

**Algorithm**: join `pace_splits_json` with the per-km average HR over the same intervals.

```python
def compute_hr_vs_pace(distance_stream, time_stream, hr_stream, pace_splits):
    out = []
    last_km_idx = 0
    for split in pace_splits:
        target_distance = split["km"] * 1000
        i = next(j for j, d in enumerate(distance_stream) if d >= target_distance)
        hr_samples = [hr_stream[k] for k in range(last_km_idx, i+1) if hr_stream[k] > 0]
        avg_hr = round(statistics.mean(hr_samples)) if hr_samples else None
        out.append({"km": split["km"], "hr": avg_hr, "pace_min": split["pace_min"]})
        last_km_idx = i
    return out
```

### 5.6 `hr_zone_source`

A small bookkeeping field: did we use Strava's per-user custom zones, or did we compute the zones ourselves from the user's `max_hr`?

- `'strava_custom'` — we honoured Strava's zones from the activity's `zones` field (rare; user must have configured them)
- `'computed_from_max_hr'` — we computed Z1–Z5 from `users.max_hr` × percentages (the default path)

---

## 6. Cadence `[from cadence stream]` `[P3 — nullable]`

**All metrics NULL if `cadence` stream is absent.** Garmin reports cadence reliably; Apple Watch is hit-or-miss depending on sync method.

**Important**: Strava reports cadence as "one foot per minute" (single-leg cadence). To get "steps per minute" (the runner-standard unit), **multiply by 2**.

### 6.1 `cadence_avg`

```python
def compute_cadence_avg(cadence_stream):
    samples = [c for c in cadence_stream if c > 0]
    if not samples:
        return None
    return round(statistics.mean(samples) * 2)  # ×2 for both feet
```

### 6.2 `cadence_std_dev`

```python
def compute_cadence_std_dev(cadence_stream):
    samples = [c * 2 for c in cadence_stream if c > 0]
    if len(samples) < 2:
        return 0.0
    return round(statistics.stdev(samples), 1)
```

### 6.3 `cadence_under170_pct`

Percentage of run time spent at a cadence below 170 spm (a common overstriding threshold).

```python
def compute_cadence_under170_pct(time_stream, cadence_stream):
    total_secs = 0
    under_secs = 0
    for i in range(1, len(time_stream)):
        c = cadence_stream[i] * 2
        if c == 0:
            continue
        dt = time_stream[i] - time_stream[i-1]
        total_secs += dt
        if c < 170:
            under_secs += dt
    if total_secs == 0:
        return None
    return round((under_secs / total_secs) * 100, 1)
```

### 6.4 `cadence_splits_json`

Per-km average cadence.

**JSON shape**: `[{"km": 1, "cadence": 175}, ...]`

**Algorithm**: same per-km windowing as `compute_grade_splits`, averaging cadence (×2) over each km window.

---

## 7. Edge Cases & Nullability Reference

| Stream missing | Metrics that go to NULL |
|---|---|
| `heartrate` | All §5 metrics, plus `avg_hr`, `max_hr` from summary |
| `cadence` | All §6 metrics |
| `altitude` (very rare) | `gap_min_km`, `grade_*` columns |
| `velocity_smooth` (very rare) | All pace analysis (§3), GAP, aerobic decoupling, EF |

| Run condition | Behaviour |
|---|---|
| Run < 1 km | `pace_splits_json = []`, `fastest_km_pace_min = NULL`, `slowest_km_pace_min = NULL` |
| Run < 20 min | `aerobic_decoupling = NULL` |
| Run < 25 min | `cardiac_drift_bpm = NULL` |
| HR sensor dropouts (`hr == 0`) | Skip those samples in HR-based metrics |
| Cadence dropouts (`cadence == 0`) | Skip those samples |
| Distance == 0 | `grade_avg_pct = 0`, all pace metrics NULL |

---

## 8. Implementation Notes

- All metric functions live in `app/services/metrics.py` as **pure functions**. No DB access, no I/O, no logging. Input is stream arrays + the user object. Output is the value(s) to store on the `runs` row.
- Each function has a corresponding test in `tests/test_metrics.py` that exercises it against a saved Strava activity JSON in `tests/fixtures/`. These tests are the regression suite — if a formula changes, the snapshot output will change too, which is intentional and visible.
- The orchestrator (`app/services/coaching.py`) calls the metric functions in dependency order, checks each result for `None`, and writes the appropriate columns. Sections of the Claude prompt are gated on the same `None` checks (see [spec.md §3.4](spec.md) "adaptive prompt").
- All metric functions accept the same `streams` dict shape that Strava returns from `GET /activities/{id}/streams`, plus the `user` ORM model. No Strava SDK coupling — the metrics module never knows where the streams came from.
