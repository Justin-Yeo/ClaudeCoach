"""Tests for `app.services.hr_zones`."""

from __future__ import annotations

import pytest

from app.services.hr_zones import compute_default_zones, estimate_max_hr


class TestComputeDefaultZones:
    def test_200(self):
        # 200 max HR: every percentage is an exact integer
        assert compute_default_zones(200) == (120, 140, 160, 180)

    def test_180(self):
        assert compute_default_zones(180) == (108, 126, 144, 162)

    def test_190(self):
        # 60% → 114, 70% → 133, 80% → 152, 90% → 171
        assert compute_default_zones(190) == (114, 133, 152, 171)

    def test_185_rounding_boundaries(self):
        # 185 * 0.7 = 129.5 and 185 * 0.9 = 166.5 land on rounding boundaries
        # Python's banker's rounding + float imprecision → accept either value
        bounds = compute_default_zones(185)
        assert bounds[0] == 111  # 60% exact
        assert bounds[2] == 148  # 80% exact
        assert bounds[1] in (129, 130)
        assert bounds[3] in (166, 167)

    def test_zones_monotonic(self):
        for max_hr in (170, 180, 190, 200, 210):
            bounds = compute_default_zones(max_hr)
            assert bounds[0] < bounds[1] < bounds[2] < bounds[3]

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            compute_default_zones(0)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            compute_default_zones(-10)


class TestEstimateMaxHr:
    @pytest.mark.parametrize(
        "age,expected",
        [
            (20, 200),
            (25, 195),
            (30, 190),
            (40, 180),
            (50, 170),
        ],
    )
    def test_basic(self, age, expected):
        assert estimate_max_hr(age) == expected

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            estimate_max_hr(0)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            estimate_max_hr(-1)
