"""Tests for `app.services.claude.build_user_prompt` and helpers.

These tests verify the prompt builder output without actually calling Claude
(no network, no API key required). We assert:
- The prompt builder fills every `{placeholder}` (no curly braces remain)
- Each block builder produces the expected shape for realistic inputs
- `compute_next_available_day` returns a valid future date from the allowed set
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.claude import (
    build_cadence_block,
    build_goals_block,
    build_grade_splits_compact,
    build_hr_block,
    build_pace_splits_compact,
    build_recent_runs_block,
    build_user_prompt,
    compute_next_available_day,
)

# -------------------------------------------------------------- fixtures


@pytest.fixture
def user() -> dict:
    return {
        "age": 28,
        "weight_kg": 68.5,
        "max_hr": 190,
        "recent_5k_secs": 20 * 60 + 30,  # 20:30
        "recent_10k_secs": 42 * 60 + 30,  # 42:30
        "available_days": ["Mon", "Wed", "Fri", "Sun"],
        "current_injury": None,
        "hr_zone1_max": 114,
        "hr_zone2_max": 133,
        "hr_zone3_max": 152,
        "hr_zone4_max": 171,
    }


@pytest.fixture
def goals() -> dict:
    return {
        "weekly_volume_goal_km": 40.0,
        "race_date": "2026-06-15",
        "race_distance": "10K",
        "race_target_secs": 45 * 60,  # 45:00
    }


@pytest.fixture
def current_run() -> dict:
    return {
        "start_date_local": "2026-04-09",
        "distance_m": 21470.0,
        "duration_secs": 9191,
        "elapsed_secs": 9389,
        "avg_pace_min_km": 7.133,
        "avg_hr": 165.0,
        "max_hr": 182,
        "elevation_gain_m": 39.0,
        "grade_avg_pct": 0.0,
        "pace_first_half_min": 7.36,
        "pace_second_half_min": 7.22,
        "fastest_km_pace_min": 6.03,
        "slowest_km_pace_min": 7.70,
        "pace_std_dev_min": 0.37,
        "gap_min_km": 7.25,
        "pace_splits": [
            {"km": 1, "pace_min": 7.15},
            {"km": 2, "pace_min": 7.22},
            {"km": 3, "pace_min": 7.35},
        ],
        "flat_distance_m": 9050,
        "uphill_distance_m": 1620,
        "downhill_distance_m": 1740,
        "grade_splits": [
            {"km": 1, "grade": 0.0},
            {"km": 2, "grade": 0.1},
            {"km": 3, "grade": -0.1},
        ],
        "hr_zones_secs": [0, 17, 251, 6766, 2355],
        "cardiac_drift_bpm": 11.0,
        "aerobic_decoupling": 3.54,
        "efficiency_factor": 0.014,
        "cadence_avg": 171,
        "cadence_std_dev": 9.2,
        "cadence_under170_pct": 9.1,
    }


@pytest.fixture
def recent_runs() -> list[dict]:
    return [
        {
            "date": "Fri 4 Apr",
            "run_type": "intervals",
            "distance_km": 10.0,
            "avg_pace_min_km": 4.75,
            "avg_hr": 162,
        },
        {
            "date": "Wed 2 Apr",
            "run_type": "easy",
            "distance_km": 8.0,
            "avg_pace_min_km": 5.5,
            "avg_hr": 148,
        },
    ]


# -------------------------------------------------------------- block tests


class TestGoalsBlock:
    def test_both_goals(self, goals):
        out = build_goals_block(goals)
        assert "Weekly volume target: 40.0 km" in out
        assert "Race goal: 10K on 2026-06-15" in out
        assert "target 45:00" in out

    def test_weekly_only(self):
        out = build_goals_block({"weekly_volume_goal_km": 40})
        assert "Weekly volume target: 40 km" in out
        assert "Race goal" not in out

    def test_race_no_target_time(self):
        out = build_goals_block({"race_date": "2026-06-15", "race_distance": "Half"})
        assert "Race goal: Half on 2026-06-15 (no time goal)" in out

    def test_race_with_hours(self):
        out = build_goals_block(
            {
                "race_date": "2026-06-15",
                "race_distance": "Marathon",
                "race_target_secs": 3 * 3600 + 30 * 60,  # 3:30:00
            }
        )
        assert "target 3:30:00" in out

    def test_empty(self):
        assert build_goals_block({}) == "- No goals set"


class TestRecentRunsBlock:
    def test_with_runs(self, recent_runs):
        out = build_recent_runs_block(recent_runs)
        assert "Fri 4 Apr · intervals · 10.0km · 4:45/km · 162bpm" in out
        assert "Wed 2 Apr · easy · 8.0km · 5:30/km · 148bpm" in out

    def test_cold_start(self):
        out = build_recent_runs_block([])
        assert "cold start" in out.lower()


class TestHrBlock:
    def test_with_hr(self, current_run):
        out = build_hr_block(current_run)
        assert "Average: 165 bpm" in out
        assert "Max: 182 bpm" in out
        assert "Z5:" in out
        assert "Cardiac drift: +11.0 bpm" in out
        assert "Aerobic decoupling: +3.54%" in out

    def test_without_hr(self):
        assert build_hr_block({}) == ""
        assert build_hr_block({"avg_hr": None}) == ""


class TestCadenceBlock:
    def test_with_cadence(self, current_run):
        out = build_cadence_block(current_run)
        assert "Average: 171 spm" in out
        assert "Std dev: 9.2 spm" in out
        assert "Under 170 spm: 9.1%" in out

    def test_without_cadence(self):
        assert build_cadence_block({}) == ""


class TestPaceSplitsCompact:
    def test_with_splits(self):
        splits = [{"km": 1, "pace_min": 4.5}, {"km": 2, "pace_min": 4.25}]
        out = build_pace_splits_compact(splits)
        assert "km1 4:30/km" in out
        assert "km2 4:15/km" in out

    def test_empty(self):
        assert build_pace_splits_compact([]) == "n/a"


class TestGradeSplitsCompact:
    def test_with_splits(self):
        splits = [{"km": 1, "grade": 0.2}, {"km": 2, "grade": -0.1}]
        out = build_grade_splits_compact(splits)
        assert "km1 +0.2%" in out
        assert "km2 -0.1%" in out

    def test_empty(self):
        assert build_grade_splits_compact([]) == "n/a"


# -------------------------------------------------------------- next-day helper


class TestNextAvailableDay:
    def test_next_day_is_mon_from_sun(self):
        # 2026-04-12 is a Sunday
        today = date(2026, 4, 12)
        nad = compute_next_available_day(today, ["Mon", "Wed", "Fri"])
        assert nad == date(2026, 4, 13)
        assert nad.strftime("%a") == "Mon"

    def test_skips_ahead_multiple_days(self):
        # From Sunday, next available is Wednesday if Mon/Tue are not allowed
        today = date(2026, 4, 12)  # Sun
        nad = compute_next_available_day(today, ["Wed", "Fri", "Sun"])
        assert nad == date(2026, 4, 15)  # Wed

    def test_strictly_in_future(self):
        today = date(2026, 4, 12)  # Sun
        nad = compute_next_available_day(today, ["Sun"])
        assert nad == date(2026, 4, 19)  # next Sunday, not today

    def test_empty_days_raises(self):
        with pytest.raises(ValueError):
            compute_next_available_day(date(2026, 4, 12), [])


# -------------------------------------------------------------- full prompt


class TestBuildUserPrompt:
    def test_no_curly_braces_remain(self, user, goals, current_run, recent_runs):
        today = date(2026, 4, 9)
        nad = date(2026, 4, 12)
        prompt = build_user_prompt(
            user=user,
            goals=goals,
            current_run=current_run,
            recent_runs=recent_runs,
            today_local=today,
            weekly_volume_done_km=24.0,
            next_available_day=nad,
        )
        # If any placeholder slipped through unfilled, there'd be a `{...}` left
        import re

        leftovers = re.findall(r"\{[^}]*\}", prompt)
        assert leftovers == [], f"unfilled placeholders: {leftovers}"

    def test_contains_key_values(self, user, goals, current_run, recent_runs):
        today = date(2026, 4, 9)
        nad = date(2026, 4, 12)
        prompt = build_user_prompt(
            user=user,
            goals=goals,
            current_run=current_run,
            recent_runs=recent_runs,
            today_local=today,
            weekly_volume_done_km=24.0,
            next_available_day=nad,
        )
        # Spot check that crucial values are in the output
        assert "28" in prompt  # age
        assert "68.5" in prompt  # weight
        assert "190" in prompt  # max_hr
        assert "5K in 20:30" in prompt
        assert "10K in 42:30" in prompt
        assert "Mon, Wed, Fri, Sun" in prompt
        assert "21.47" in prompt  # distance
        assert "40.0 km" in prompt  # weekly goal
        assert "24.0" in prompt  # weekly done
        assert "16.0" in prompt  # weekly remaining
        assert "7:08/km" in prompt  # avg pace label
        assert "6:02/km" in prompt  # fastest km
        assert "Heart rate" in prompt
        assert "Cadence" in prompt
        assert "2026-04-12" in prompt  # scheduled date

    def test_cold_start_no_history(self, user, goals, current_run):
        today = date(2026, 4, 9)
        nad = date(2026, 4, 12)
        prompt = build_user_prompt(
            user=user,
            goals=goals,
            current_run=current_run,
            recent_runs=[],
            today_local=today,
            weekly_volume_done_km=0.0,
            next_available_day=nad,
        )
        assert "cold start" in prompt.lower()

    def test_current_injury_none(self, user, goals, current_run, recent_runs):
        today = date(2026, 4, 9)
        nad = date(2026, 4, 12)
        prompt = build_user_prompt(
            user=user,
            goals=goals,
            current_run=current_run,
            recent_runs=recent_runs,
            today_local=today,
            weekly_volume_done_km=24.0,
            next_available_day=nad,
        )
        assert "Current injury / niggle: none" in prompt

    def test_current_injury_set(self, user, goals, current_run, recent_runs):
        user_with_injury = {**user, "current_injury": "left achilles sore 3/10"}
        today = date(2026, 4, 9)
        nad = date(2026, 4, 12)
        prompt = build_user_prompt(
            user=user_with_injury,
            goals=goals,
            current_run=current_run,
            recent_runs=recent_runs,
            today_local=today,
            weekly_volume_done_km=24.0,
            next_available_day=nad,
        )
        assert "left achilles sore 3/10" in prompt
