"""Save a real Strava activity + streams as a test fixture.

Fetches one activity (by ID) from Strava using your dev tokens and saves the
raw responses into `tests/fixtures/strava_activity_sample.json` and
`tests/fixtures/strava_streams_sample.json`. These become the input to
`tests/test_metrics.py` in task 1.5 and the inputs to the Claude demo in phase 2.

Usage:
    uv run python scripts/save_fixture.py <activity_id>

Find your activity ID in the Strava web URL:
    https://www.strava.com/activities/12345678   <-- the number is the ID

Pick a run that has HR, cadence, and altitude streams so the fixture exercises
every metric path. A Garmin-synced tempo run or long run is usually ideal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.services.strava import StravaClient

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
ACTIVITY_PATH = FIXTURE_DIR / "strava_activity_sample.json"
STREAMS_PATH = FIXTURE_DIR / "strava_streams_sample.json"


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    try:
        activity_id = int(sys.argv[1])
    except ValueError:
        print(f"ERROR: '{sys.argv[1]}' is not a valid activity ID (must be an integer)")
        sys.exit(1)

    required = [
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "DEV_STRAVA_ACCESS_TOKEN",
        "DEV_STRAVA_REFRESH_TOKEN",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("Run scripts/strava_oauth_dance.py first to populate the DEV_ vars.")
        sys.exit(1)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    def on_refresh(new_tokens: dict) -> None:
        print("\n⚠️  Access token was refreshed. Update these in your .env:")
        print(f"DEV_STRAVA_ACCESS_TOKEN={new_tokens['access_token']}")
        print(f"DEV_STRAVA_REFRESH_TOKEN={new_tokens['refresh_token']}")
        print(f"DEV_STRAVA_TOKEN_EXPIRES_AT={new_tokens['expires_at']}\n")

    async with StravaClient(
        client_id=os.environ["STRAVA_CLIENT_ID"],
        client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        access_token=os.environ["DEV_STRAVA_ACCESS_TOKEN"],
        refresh_token=os.environ["DEV_STRAVA_REFRESH_TOKEN"],
        on_refresh=on_refresh,
    ) as strava:
        print(f"Fetching activity {activity_id}...")
        activity = await strava.get_activity(activity_id)

        print(f"  → {activity.get('name')} ({activity.get('type')})")
        print(f"  → distance: {activity.get('distance', 0) / 1000:.2f} km")
        print(f"  → moving_time: {activity.get('moving_time')} s")

        if activity.get("type") != "Run":
            print(
                f"WARNING: activity type is '{activity.get('type')}', not 'Run'. "
                "The metrics module only processes Runs in production."
            )

        print("\nFetching streams...")
        streams = await strava.get_activity_streams(activity_id)

        present = [k for k in streams if streams[k].get("data")]
        missing_keys = [k for k in ["heartrate", "cadence", "altitude"] if k not in present]
        print(f"  → streams present: {', '.join(sorted(present))}")
        if missing_keys:
            print(f"  ⚠️  missing (metrics will be NULL): {', '.join(missing_keys)}")

    ACTIVITY_PATH.write_text(json.dumps(activity, indent=2))
    STREAMS_PATH.write_text(json.dumps(streams, indent=2))

    print()
    print(f"Saved: {ACTIVITY_PATH.relative_to(Path.cwd())}")
    print(f"Saved: {STREAMS_PATH.relative_to(Path.cwd())}")
    print("\nTask 1.4.2 complete — ready for task 1.5 (metrics module).")


if __name__ == "__main__":
    asyncio.run(main())
