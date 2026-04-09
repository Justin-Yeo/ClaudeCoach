"""Tests for `app.services.pace`."""

from __future__ import annotations

import pytest

from app.services.pace import format_pace_min, parse_pace_str, secs_to_pace_min


class TestFormatPaceMin:
    @pytest.mark.parametrize(
        "pace,expected",
        [
            (4.5, "4:30/km"),
            (4.0, "4:00/km"),
            (5.0, "5:00/km"),
            (10.0, "10:00/km"),
            (3.5, "3:30/km"),
            (6.25, "6:15/km"),
            (4.0833, "4:05/km"),  # 4 min + 5 sec
            (7.1333, "7:08/km"),  # matches the avg pace of your fixture
        ],
    )
    def test_format(self, pace, expected):
        assert format_pace_min(pace) == expected

    def test_format_negative_raises(self):
        with pytest.raises(ValueError):
            format_pace_min(-1.0)

    def test_format_zero_raises(self):
        with pytest.raises(ValueError):
            format_pace_min(0)


class TestParsePaceStr:
    @pytest.mark.parametrize(
        "s,expected",
        [
            ("4:30/km", 4.5),
            ("4:30", 4.5),
            ("4:00/km", 4.0),
            ("10:00/km", 10.0),
            ("6:15/km", 6.25),
            (" 4:30/km ", 4.5),  # whitespace
        ],
    )
    def test_parse(self, s, expected):
        assert parse_pace_str(s) == expected

    def test_parse_invalid_format(self):
        with pytest.raises(ValueError):
            parse_pace_str("4.5")

    def test_parse_invalid_seconds(self):
        with pytest.raises(ValueError):
            parse_pace_str("4:60/km")

    def test_parse_gibberish(self):
        with pytest.raises(ValueError):
            parse_pace_str("not a pace")

    def test_parse_empty(self):
        with pytest.raises(ValueError):
            parse_pace_str("")


class TestRoundTrip:
    """parse(format(x)) should recover x within rounding tolerance.

    format() rounds to the nearest second, so the round-trip can differ from
    the input by up to ~0.008 min (half a second).
    """

    @pytest.mark.parametrize("pace", [3.5, 4.0, 4.5, 5.25, 6.5, 7.083, 10.0, 7.133])
    def test_format_then_parse(self, pace):
        formatted = format_pace_min(pace)
        reparsed = parse_pace_str(formatted)
        assert abs(reparsed - pace) < 0.02


class TestSecsToPaceMin:
    def test_basic(self):
        # 5000 m in 20 min = 4:00/km = 4.0
        assert secs_to_pace_min(1200, 5000) == 4.0

    def test_realistic(self):
        # 10 km in 45 min = 4:30/km = 4.5
        assert secs_to_pace_min(2700, 10000) == 4.5

    def test_zero_distance(self):
        assert secs_to_pace_min(1200, 0) is None

    def test_zero_time(self):
        assert secs_to_pace_min(0, 5000) is None

    def test_negative(self):
        assert secs_to_pace_min(-100, 5000) is None
        assert secs_to_pace_min(1200, -5000) is None
