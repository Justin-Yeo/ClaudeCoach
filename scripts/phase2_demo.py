"""Phase 2 gate — end-to-end Claude coaching demo.

Loads the saved Strava fixture, constructs a mock user + goals + empty
history, runs the full metric pipeline, builds the Claude prompt, calls
Claude Sonnet 4.6 via tool use, and prints the parsed coaching response.

Usage:
    uv run python scripts/phase2_demo.py              # full demo (calls Claude)
    uv run python scripts/phase2_demo.py --dry-run    # just print the prompt

Prerequisites:
    - Task 1.4.2 completed — tests/fixtures/strava_*.json present
    - ANTHROPIC_API_KEY set in .env
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from app.services import metrics
from app.services.claude import (
    build_user_prompt,
    call_claude_coaching,
    compute_next_available_day,
)
from app.services.hr_zones import compute_default_zones
from app.services.pace import format_pace_min

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


# ---------------------------------------------------------------- mock data


def mock_user() -> dict:
    """A realistic mock user profile for the phase 2 demo.

    Phase 4 replaces this with a real row from the users table.
    """
    max_hr = 190
    z1, z2, z3, z4 = compute_default_zones(max_hr)
    return {
        "age": 28,
        "weight_kg": 70.0,
        "max_hr": max_hr,
        "recent_5k_secs": 22 * 60,  # 22:00
        "recent_10k_secs": 46 * 60,  # 46:00
        "available_days": ["Mon", "Wed", "Fri", "Sun"],
        "current_injury": None,
        "hr_zone1_max": z1,
        "hr_zone2_max": z2,
        "hr_zone3_max": z3,
        "hr_zone4_max": z4,
    }


def mock_goals() -> dict:
    return {
        "weekly_volume_goal_km": 50.0,
        "race_date": "2026-06-15",
        "race_distance": "Half",
        "race_target_secs": 1 * 3600 + 40 * 60,  # 1:40:00
    }


# ---------------------------------------------------------------- pipeline


def build_current_run_from_fixture(activity: dict, streams: dict, user: dict) -> dict:
    """Run the fixture through `app.services.metrics` and merge the results
    with the raw Strava activity stats into the dict shape expected by the
    prompt builder.

    Mirrors what `app/services/coaching.py` will do in phase 5.
    """
    d = streams["distance"]["data"]
    t = streams["time"]["data"]
    v = streams["velocity_smooth"]["data"]
    alt = streams["altitude"]["data"]
    grade = streams["grade_smooth"]["data"]
    hr = metrics.get_stream_data(streams, "heartrate")
    cad = metrics.get_stream_data(streams, "cadence")

    pace_splits = metrics.compute_pace_splits(d, t)
    first_half, second_half = metrics.compute_half_paces(d, t)
    fastest, slowest = metrics.compute_fastest_slowest_km(pace_splits)

    run: dict = {
        # --- Summary (from Strava)
        "start_date_local": activity.get("start_date_local", "").split("T")[0],
        "distance_m": activity["distance"],
        "duration_secs": activity["moving_time"],
        "elapsed_secs": activity["elapsed_time"],
        "elevation_gain_m": activity.get("total_elevation_gain", 0),
        "avg_hr": activity.get("average_heartrate"),
        "max_hr": activity.get("max_heartrate"),
        # --- Pace analysis
        "avg_pace_min_km": metrics.compute_avg_pace_min_km(
            activity["distance"], activity["moving_time"]
        ),
        "pace_first_half_min": first_half,
        "pace_second_half_min": second_half,
        "fastest_km_pace_min": fastest,
        "slowest_km_pace_min": slowest,
        "pace_std_dev_min": metrics.compute_pace_std_dev(pace_splits),
        "pace_splits": pace_splits,
        "gap_min_km": metrics.compute_gap(d, t, grade),
        # --- Elevation & grade
        "grade_avg_pct": metrics.compute_grade_avg(alt, d),
        "grade_splits": metrics.compute_grade_splits(d, grade),
    }

    flat, up, down = metrics.compute_grade_distances(d, grade)
    run["flat_distance_m"] = flat
    run["uphill_distance_m"] = up
    run["downhill_distance_m"] = down

    # --- HR (if stream present)
    if hr is not None:
        bounds = (
            user["hr_zone1_max"],
            user["hr_zone2_max"],
            user["hr_zone3_max"],
            user["hr_zone4_max"],
        )
        run["hr_zones_secs"] = metrics.compute_hr_zones(t, hr, bounds)
        run["cardiac_drift_bpm"] = metrics.compute_cardiac_drift(t, hr)
        run["aerobic_decoupling"] = metrics.compute_aerobic_decoupling(t, hr, v)
        run["efficiency_factor"] = metrics.compute_efficiency_factor(
            activity["average_speed"], activity.get("average_heartrate")
        )

    # --- Cadence (if stream present)
    if cad is not None:
        run["cadence_avg"] = metrics.compute_cadence_avg(cad)
        run["cadence_std_dev"] = metrics.compute_cadence_std_dev(cad)
        run["cadence_under170_pct"] = metrics.compute_cadence_under170_pct(t, cad)
        run["cadence_splits"] = metrics.compute_cadence_splits(d, cad)

    return run


# ---------------------------------------------------------------- pretty print


def print_banner(text: str, char: str = "=") -> None:
    print(char * 72)
    print(f" {text}")
    print(char * 72)


def print_coaching_response(response: dict) -> None:
    print_banner("COACHING RESPONSE")
    print()
    print(f"run_type:    {response['run_type']}")
    print(f"load_rating: {response['load_rating']}")
    print(f"flags:       {response['flags'] or '(none)'}")
    print()

    review = response["post_run_review"]
    print("─ POST-RUN REVIEW ──────────────────────────────────────────────────────")
    print()
    print("Run Summary:")
    print(f"  {review['run_summary']}")
    print()
    print("What Went Well:")
    print(f"  {review['went_well']}")
    print()
    print("What to Watch:")
    print(f"  {review['to_watch']}")
    print()
    print(f"Digest (for /history): {review['digest']}")
    print()

    ns = response["next_session"]
    print("─ NEXT SESSION ─────────────────────────────────────────────────────────")
    print()
    print(
        f"  {ns['type'].upper():>10}  {ns['scheduled_day_label']} "
        f"{ns['scheduled_date']}  (in {ns['relative_offset_days']} days)"
    )
    print(f"  Distance:   {ns['distance_km']} km")
    print(f"  Target pace: {ns['target_pace_label']}")
    print(f"  HR zone:    {ns['target_hr_zone']}")
    print()
    workout = ns["workout"]
    if workout.get("warmup"):
        print(f"  Warmup:   {workout['warmup']}")
    print(f"  Main:     {workout['main']}")
    if workout.get("cooldown"):
        print(f"  Cooldown: {workout['cooldown']}")
    if ns.get("notes"):
        print()
        print(f"  Notes: {ns['notes']}")
    print()
    print_banner("Phase 2 gate PASSED — Claude returned a valid coaching response.")


# ---------------------------------------------------------------- main


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    dry_run = "--dry-run" in sys.argv

    if not (FIXTURE_DIR / "strava_activity_sample.json").exists():
        print("ERROR: fixture not found. Run scripts/save_fixture.py <activity_id> first.")
        sys.exit(1)

    activity = json.loads((FIXTURE_DIR / "strava_activity_sample.json").read_text())
    streams = json.loads((FIXTURE_DIR / "strava_streams_sample.json").read_text())

    user = mock_user()
    goals = mock_goals()
    current_run = build_current_run_from_fixture(activity, streams, user)
    recent_runs: list[dict] = []  # cold start — no history

    today_local = date.today()
    next_day = compute_next_available_day(today_local, user["available_days"])

    print_banner(f"DEMO — {activity.get('name', 'Run')}")
    print(f" distance:     {current_run['distance_m'] / 1000:.2f} km")
    print(f" moving time:  {current_run['duration_secs']}s")
    pace = current_run["avg_pace_min_km"]
    pace_label = format_pace_min(pace) if pace else "n/a"
    print(f" avg pace:     {pace_label}")
    print(f" today:        {today_local.isoformat()}")
    print(f" next avail:   {next_day.isoformat()} ({next_day.strftime('%a')})")
    print()

    prompt = build_user_prompt(
        user=user,
        goals=goals,
        current_run=current_run,
        recent_runs=recent_runs,
        today_local=today_local,
        weekly_volume_done_km=current_run["distance_m"] / 1000,
        next_available_day=next_day,
    )

    print_banner("FILLED USER PROMPT", "─")
    print()
    print(prompt)
    print()

    if dry_run:
        print("--dry-run flag set; skipping Claude call.")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    print_banner("CALLING CLAUDE", "─")
    print(f" model: {os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')}")
    print(" waiting for response...")
    print()

    response = await call_claude_coaching(
        user=user,
        goals=goals,
        current_run=current_run,
        recent_runs=recent_runs,
        today_local=today_local,
        weekly_volume_done_km=current_run["distance_m"] / 1000,
        next_available_day=next_day,
    )

    print_coaching_response(response)


if __name__ == "__main__":
    asyncio.run(main())
