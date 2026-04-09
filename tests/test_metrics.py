"""Tests for `app.services.metrics`.

Loads the real Strava fixture saved in task 1.4.2 and asserts:
- Every metric function returns a plausible value on real data
- Shape/length invariants (e.g. pace_splits length == int(distance_km))
- Edge cases (empty streams, short runs, sensor dropouts)

The fixture is a real run, so exact values depend on which activity you saved.
Tests here use property-based assertions (ranges, shapes, invariants) rather
than hardcoded snapshots so they survive re-saving the fixture with a
different activity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import metrics

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def activity() -> dict:
    return json.loads((FIXTURE_DIR / "strava_activity_sample.json").read_text())


@pytest.fixture(scope="session")
def streams() -> dict:
    return json.loads((FIXTURE_DIR / "strava_streams_sample.json").read_text())


@pytest.fixture(scope="session")
def time_stream(streams) -> list:
    return streams["time"]["data"]


@pytest.fixture(scope="session")
def distance_stream(streams) -> list:
    return streams["distance"]["data"]


@pytest.fixture(scope="session")
def velocity_stream(streams) -> list:
    return streams["velocity_smooth"]["data"]


@pytest.fixture(scope="session")
def altitude_stream(streams) -> list:
    return streams["altitude"]["data"]


@pytest.fixture(scope="session")
def grade_stream(streams) -> list:
    return streams["grade_smooth"]["data"]


@pytest.fixture(scope="session")
def hr_stream(streams) -> list | None:
    return metrics.get_stream_data(streams, "heartrate")


@pytest.fixture(scope="session")
def cadence_stream(streams) -> list | None:
    return metrics.get_stream_data(streams, "cadence")


# ============================================================================
# Summary
# ============================================================================


class TestSummary:
    def test_avg_pace_min_km(self, activity):
        pace = metrics.compute_avg_pace_min_km(activity["distance"], activity["moving_time"])
        assert pace is not None
        assert 2.5 < pace < 9.0  # any reasonable human running pace

    def test_avg_pace_zero_distance(self):
        assert metrics.compute_avg_pace_min_km(0, 600) is None

    def test_avg_pace_zero_time(self):
        assert metrics.compute_avg_pace_min_km(5000, 0) is None

    def test_get_stream_data_missing(self):
        assert metrics.get_stream_data({}, "heartrate") is None
        assert metrics.get_stream_data({"heartrate": {}}, "heartrate") is None
        assert metrics.get_stream_data({"heartrate": {"data": []}}, "heartrate") is None

    def test_get_stream_data_present(self):
        assert metrics.get_stream_data({"heartrate": {"data": [120, 125, 130]}}, "heartrate") == [
            120,
            125,
            130,
        ]


# ============================================================================
# Pace analysis
# ============================================================================


class TestPaceAnalysis:
    def test_pace_splits_length(self, distance_stream, time_stream, activity):
        splits = metrics.compute_pace_splits(distance_stream, time_stream)
        expected_kms = int(activity["distance"] // 1000)
        assert len(splits) == expected_kms

    def test_pace_splits_shape(self, distance_stream, time_stream):
        splits = metrics.compute_pace_splits(distance_stream, time_stream)
        for split in splits:
            assert set(split.keys()) == {"km", "pace_min"}
            assert isinstance(split["km"], int)
            assert isinstance(split["pace_min"], float)
            assert 2.0 < split["pace_min"] < 12.0

    def test_pace_splits_monotonic_km(self, distance_stream, time_stream):
        splits = metrics.compute_pace_splits(distance_stream, time_stream)
        kms = [s["km"] for s in splits]
        assert kms == list(range(1, len(splits) + 1))

    def test_pace_splits_empty_stream(self):
        assert metrics.compute_pace_splits([], []) == []

    def test_pace_splits_short_run(self):
        # 900 metres — no splits since the run is under 1 km
        assert metrics.compute_pace_splits([0, 500, 900], [0, 150, 270]) == []

    def test_pace_splits_mismatched_lengths(self):
        assert metrics.compute_pace_splits([0, 1000, 2000], [0, 300]) == []

    def test_half_paces(self, distance_stream, time_stream):
        first, second = metrics.compute_half_paces(distance_stream, time_stream)
        assert first is not None and second is not None
        assert 2.0 < first < 12.0
        assert 2.0 < second < 12.0

    def test_half_paces_empty(self):
        assert metrics.compute_half_paces([], []) == (None, None)

    def test_pace_std_dev_real_run(self, distance_stream, time_stream):
        splits = metrics.compute_pace_splits(distance_stream, time_stream)
        std = metrics.compute_pace_std_dev(splits)
        assert std >= 0

    def test_pace_std_dev_single(self):
        assert metrics.compute_pace_std_dev([{"km": 1, "pace_min": 5.0}]) == 0.0

    def test_pace_std_dev_empty(self):
        assert metrics.compute_pace_std_dev([]) == 0.0

    def test_fastest_slowest(self, distance_stream, time_stream):
        splits = metrics.compute_pace_splits(distance_stream, time_stream)
        fastest, slowest = metrics.compute_fastest_slowest_km(splits)
        assert fastest is not None and slowest is not None
        assert fastest <= slowest

    def test_fastest_slowest_empty(self):
        assert metrics.compute_fastest_slowest_km([]) == (None, None)

    def test_gap_on_real_run(self, distance_stream, time_stream, grade_stream):
        gap = metrics.compute_gap(distance_stream, time_stream, grade_stream)
        assert gap is not None
        assert 2.0 < gap < 12.0

    def test_gap_missing_grade(self):
        assert metrics.compute_gap([0, 500, 1000], [0, 150, 300], []) is None

    def test_gap_mismatched_lengths(self):
        assert metrics.compute_gap([0, 1000], [0, 300], [0, 1, 2]) is None


# ============================================================================
# Elevation & grade
# ============================================================================


class TestGrade:
    def test_grade_avg(self, altitude_stream, distance_stream):
        grade = metrics.compute_grade_avg(altitude_stream, distance_stream)
        # Any reasonable run has net avg grade within ±15% (extremely steep)
        assert -15.0 < grade < 15.0

    def test_grade_avg_empty(self):
        assert metrics.compute_grade_avg([], []) == 0.0

    def test_grade_distances(self, distance_stream, grade_stream, activity):
        flat, up, down = metrics.compute_grade_distances(distance_stream, grade_stream)
        assert flat >= 0 and up >= 0 and down >= 0
        # Sum of buckets ≤ total distance (neutral band not counted)
        total_bucketed = flat + up + down
        assert total_bucketed <= activity["distance"] * 1.01  # 1% rounding tolerance

    def test_grade_distances_empty(self):
        assert metrics.compute_grade_distances([], []) == (0.0, 0.0, 0.0)

    def test_grade_splits_length(self, distance_stream, grade_stream, activity):
        splits = metrics.compute_grade_splits(distance_stream, grade_stream)
        expected = int(activity["distance"] // 1000)
        assert len(splits) == expected

    def test_grade_splits_shape(self, distance_stream, grade_stream):
        splits = metrics.compute_grade_splits(distance_stream, grade_stream)
        for s in splits:
            assert set(s.keys()) == {"km", "grade"}
            assert isinstance(s["km"], int)
            assert isinstance(s["grade"], float)


# ============================================================================
# Heart rate
# ============================================================================


class TestHeartRate:
    def test_hr_zones(self, time_stream, hr_stream, activity):
        if hr_stream is None:
            pytest.skip("fixture has no HR stream")

        max_hr = 190
        bounds = (
            round(max_hr * 0.6),
            round(max_hr * 0.7),
            round(max_hr * 0.8),
            round(max_hr * 0.9),
        )
        zones = metrics.compute_hr_zones(time_stream, hr_stream, bounds)
        assert len(zones) == 5
        assert all(z >= 0 for z in zones)

        total = sum(zones)
        # Should be close to moving_time; allow wide tolerance for dropouts + elapsed vs moving diff
        assert 0 < total <= activity["elapsed_time"] * 1.05

    def test_hr_zones_empty(self):
        zones = metrics.compute_hr_zones([], [], (114, 133, 152, 171))
        assert zones == [0, 0, 0, 0, 0]

    def test_cardiac_drift(self, time_stream, hr_stream):
        if hr_stream is None:
            pytest.skip("fixture has no HR stream")

        drift = metrics.compute_cardiac_drift(time_stream, hr_stream)
        duration = time_stream[-1] - time_stream[0]
        if duration < 25 * 60:
            assert drift is None
        else:
            assert drift is not None
            assert -50 < drift < 50

    def test_cardiac_drift_short_run(self):
        time = list(range(600))  # 10 minutes
        hr = [150] * 600
        assert metrics.compute_cardiac_drift(time, hr) is None

    def test_aerobic_decoupling(self, time_stream, hr_stream, velocity_stream):
        if hr_stream is None:
            pytest.skip("fixture has no HR stream")

        dec = metrics.compute_aerobic_decoupling(time_stream, hr_stream, velocity_stream)
        duration = time_stream[-1] - time_stream[0]
        if duration < 20 * 60:
            assert dec is None
        else:
            assert dec is not None
            assert -50 < dec < 50

    def test_aerobic_decoupling_short_run(self):
        time = list(range(600))
        hr = [150] * 600
        velocity = [3.0] * 600
        assert metrics.compute_aerobic_decoupling(time, hr, velocity) is None

    def test_efficiency_factor(self, activity):
        avg_hr = activity.get("average_heartrate")
        if avg_hr is None:
            pytest.skip("fixture has no average_heartrate")

        ef = metrics.compute_efficiency_factor(activity["average_speed"], avg_hr)
        assert ef is not None
        assert 0.005 < ef < 0.05  # typical runner EF range

    def test_efficiency_factor_zero_hr(self):
        assert metrics.compute_efficiency_factor(3.0, 0) is None
        assert metrics.compute_efficiency_factor(3.0, None) is None

    def test_hr_vs_pace(self, distance_stream, time_stream, hr_stream):
        if hr_stream is None:
            pytest.skip("fixture has no HR stream")

        pace_splits = metrics.compute_pace_splits(distance_stream, time_stream)
        hrp = metrics.compute_hr_vs_pace(distance_stream, time_stream, hr_stream, pace_splits)
        assert len(hrp) == len(pace_splits)
        for entry in hrp:
            assert set(entry.keys()) == {"km", "hr", "pace_min"}


# ============================================================================
# Cadence
# ============================================================================


class TestCadence:
    def test_cadence_avg(self, cadence_stream):
        if cadence_stream is None:
            pytest.skip("fixture has no cadence stream")

        avg = metrics.compute_cadence_avg(cadence_stream)
        assert avg is not None
        assert 140 <= avg <= 220  # typical running cadence

    def test_cadence_avg_empty(self):
        assert metrics.compute_cadence_avg([]) is None
        assert metrics.compute_cadence_avg([0, 0, 0]) is None

    def test_cadence_std_dev(self, cadence_stream):
        if cadence_stream is None:
            pytest.skip("fixture has no cadence stream")

        std = metrics.compute_cadence_std_dev(cadence_stream)
        assert std >= 0

    def test_cadence_under170_pct(self, time_stream, cadence_stream):
        if cadence_stream is None:
            pytest.skip("fixture has no cadence stream")

        pct = metrics.compute_cadence_under170_pct(time_stream, cadence_stream)
        assert pct is not None
        assert 0 <= pct <= 100

    def test_cadence_splits(self, distance_stream, cadence_stream, activity):
        if cadence_stream is None:
            pytest.skip("fixture has no cadence stream")

        splits = metrics.compute_cadence_splits(distance_stream, cadence_stream)
        expected = int(activity["distance"] // 1000)
        assert len(splits) == expected
        for s in splits:
            assert set(s.keys()) == {"km", "cadence"}
