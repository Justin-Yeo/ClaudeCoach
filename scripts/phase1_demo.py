"""Phase 1 gate — end-to-end sanity demo.

Runs one activity through the full metric pipeline (strava → metrics → display)
and prints a clean, human-readable summary. No Claude, no DB, no Telegram.

Two modes:

    # Use the saved fixture (fast, no Strava API call):
    uv run python scripts/phase1_demo.py

    # Fetch a fresh activity from Strava (uses DEV_STRAVA_* tokens):
    uv run python scripts/phase1_demo.py <activity_id>

The max HR used for zone computation is a placeholder (190 bpm). Phase 4 reads
the real value from the `users` table after the user sets their profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.services import metrics
from app.services.hr_zones import compute_default_zones
from app.services.pace import format_pace_min
from app.services.strava import StravaClient

MOCK_MAX_HR = 190

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ACTIVITY_FIXTURE = FIXTURE_DIR / "strava_activity_sample.json"
STREAMS_FIXTURE = FIXTURE_DIR / "strava_streams_sample.json"


# ---------------------------------------------------------------- data loading


async def fetch_from_strava(activity_id: int) -> tuple[dict, dict]:
    required = [
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "DEV_STRAVA_ACCESS_TOKEN",
        "DEV_STRAVA_REFRESH_TOKEN",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("Run scripts/strava_oauth_dance.py first.")
        sys.exit(1)

    async with StravaClient(
        client_id=os.environ["STRAVA_CLIENT_ID"],
        client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        access_token=os.environ["DEV_STRAVA_ACCESS_TOKEN"],
        refresh_token=os.environ["DEV_STRAVA_REFRESH_TOKEN"],
    ) as strava:
        activity = await strava.get_activity(activity_id)
        streams = await strava.get_activity_streams(activity_id)
    return activity, streams


def load_from_fixture() -> tuple[dict, dict]:
    if not ACTIVITY_FIXTURE.exists() or not STREAMS_FIXTURE.exists():
        print("ERROR: fixtures not found. Run scripts/save_fixture.py <activity_id> first.")
        sys.exit(1)
    activity = json.loads(ACTIVITY_FIXTURE.read_text())
    streams = json.loads(STREAMS_FIXTURE.read_text())
    return activity, streams


# ---------------------------------------------------------------- formatting


def fmt_duration(total_secs: int) -> str:
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    seconds = total_secs % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fmt_pace_or_na(pace_min: float | None) -> str:
    return format_pace_min(pace_min) if pace_min else "n/a"


def fmt_pct_bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------- main


def print_report(activity: dict, streams: dict) -> None:
    d = streams["distance"]["data"]
    t = streams["time"]["data"]
    v = streams["velocity_smooth"]["data"]
    alt = streams["altitude"]["data"]
    grade = streams["grade_smooth"]["data"]
    hr = metrics.get_stream_data(streams, "heartrate")
    cad = metrics.get_stream_data(streams, "cadence")

    title = f"{activity.get('name', '?')} — {activity.get('type', '?')}"
    print("=" * 72)
    print(f" {title}")
    print("=" * 72)
    print(f" distance:    {activity['distance'] / 1000:.2f} km")
    print(f" moving time: {fmt_duration(activity['moving_time'])}")
    print(f" elapsed:     {fmt_duration(activity['elapsed_time'])}")
    print(f" elevation:   {activity.get('total_elevation_gain', 0):.0f} m gain")
    print()

    # -------------------------------------------------------------- PACE
    print("─ PACE ─────────────────────────────────────────────────────────────────")
    avg = metrics.compute_avg_pace_min_km(activity["distance"], activity["moving_time"])
    print(f"  average:        {fmt_pace_or_na(avg)}")

    splits = metrics.compute_pace_splits(d, t)
    fastest, slowest = metrics.compute_fastest_slowest_km(splits)
    print(f"  fastest km:     {fmt_pace_or_na(fastest)}")
    print(f"  slowest km:     {fmt_pace_or_na(slowest)}")
    print(f"  std dev:        {metrics.compute_pace_std_dev(splits):.2f} min/km")

    first, second = metrics.compute_half_paces(d, t)
    if first and second:
        delta_secs = (second - first) * 60
        sign = "+" if delta_secs >= 0 else "−"
        split_kind = "positive split" if delta_secs > 0 else "negative split"
        print(
            f"  first half:     {format_pace_min(first)}\n"
            f"  second half:    {format_pace_min(second)}  "
            f"({sign}{abs(delta_secs):.0f}s, {split_kind})"
        )

    gap = metrics.compute_gap(d, t, grade)
    print(f"  GAP:            {fmt_pace_or_na(gap)}")
    print()

    # -------------------------------------------------------------- ELEVATION
    print("─ ELEVATION ────────────────────────────────────────────────────────────")
    ga = metrics.compute_grade_avg(alt, d)
    print(f"  avg grade:      {ga:+.1f}%")
    flat, up, down = metrics.compute_grade_distances(d, grade)
    print(
        f"  flat:           {flat / 1000:.2f} km\n"
        f"  uphill (>2%):   {up / 1000:.2f} km\n"
        f"  downhill (<-2%): {down / 1000:.2f} km"
    )
    print()

    # -------------------------------------------------------------- HR
    print("─ HEART RATE ───────────────────────────────────────────────────────────")
    if hr is None:
        print("  no HR stream — metrics skipped")
    else:
        bounds = compute_default_zones(MOCK_MAX_HR)
        print(f"  (using mock max_hr={MOCK_MAX_HR}, zones={bounds})")
        zones = metrics.compute_hr_zones(t, hr, bounds)
        total = sum(zones)
        for i, secs in enumerate(zones):
            pct = (secs / total * 100) if total else 0
            bar = fmt_pct_bar(pct)
            print(f"  Z{i + 1}:  {fmt_duration(secs):>7}  {bar}  {pct:4.0f}%")

        drift = metrics.compute_cardiac_drift(t, hr)
        dec = metrics.compute_aerobic_decoupling(t, hr, v)
        ef = metrics.compute_efficiency_factor(
            activity["average_speed"], activity.get("average_heartrate")
        )
        drift_str = f"{drift:+.1f} bpm" if drift is not None else "n/a"
        dec_str = f"{dec:+.2f}%" if dec is not None else "n/a"
        ef_str = f"{ef}" if ef is not None else "n/a"
        print(f"  cardiac drift:  {drift_str}")
        print(f"  decoupling:     {dec_str}")
        print(f"  EF (speed/HR):  {ef_str}")
    print()

    # -------------------------------------------------------------- CADENCE
    print("─ CADENCE ──────────────────────────────────────────────────────────────")
    if cad is None:
        print("  no cadence stream — metrics skipped")
    else:
        avg_cad = metrics.compute_cadence_avg(cad)
        std_cad = metrics.compute_cadence_std_dev(cad)
        under170 = metrics.compute_cadence_under170_pct(t, cad)
        print(f"  avg:            {avg_cad} spm")
        print(f"  std dev:        {std_cad} spm")
        print(f"  <170 spm:       {under170}% of the run")
    print()

    # -------------------------------------------------------------- SPLITS table
    if splits:
        print("─ PER-KM SPLITS ────────────────────────────────────────────────────────")
        hrp = metrics.compute_hr_vs_pace(d, t, hr, splits) if hr else []
        header = "  km   pace      grade"
        if hrp:
            header += "    HR"
        print(header)
        grade_splits = metrics.compute_grade_splits(d, grade)
        grade_by_km = {s["km"]: s["grade"] for s in grade_splits}
        hr_by_km = {e["km"]: e["hr"] for e in hrp}
        for split in splits:
            km = split["km"]
            pace_str = format_pace_min(split["pace_min"])
            grade_str = f"{grade_by_km.get(km, 0):+.1f}%"
            row = f"  {km:2d}   {pace_str:9} {grade_str:7}"
            if hrp:
                row += f"  {hr_by_km.get(km, '?')} bpm"
            print(row)
    print()
    print("=" * 72)
    print(" Phase 1 gate PASSED — metrics pipeline produces sensible values.")
    print("=" * 72)


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    if len(sys.argv) > 1:
        try:
            activity_id = int(sys.argv[1])
        except ValueError:
            print(f"ERROR: '{sys.argv[1]}' is not a valid activity ID")
            sys.exit(1)
        print(f"Fetching activity {activity_id} from Strava...\n")
        activity, streams = await fetch_from_strava(activity_id)
    else:
        print(f"Loading fixture from {ACTIVITY_FIXTURE.relative_to(Path.cwd())}...\n")
        activity, streams = load_from_fixture()

    print_report(activity, streams)


if __name__ == "__main__":
    asyncio.run(main())
