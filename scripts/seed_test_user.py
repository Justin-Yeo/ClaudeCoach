"""Seed your admin user into the `users` table.

Inserts (or upserts) one row using the dev tokens from `.env` plus a sensible
default profile + weekly goal so the rest of phase 4 — and phase 5's coaching
pipeline — has something real to work with.

Idempotent: re-running just updates the row in place keyed on
`telegram_user_id`. Safe to run after re-running `scripts/strava_oauth_dance.py`
to refresh tokens.

Usage:
    uv run python scripts/seed_test_user.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import User
from app.services.hr_zones import compute_default_zones, estimate_max_hr


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    settings = get_settings()

    required = [
        "DEV_STRAVA_ACCESS_TOKEN",
        "DEV_STRAVA_REFRESH_TOKEN",
        "DEV_STRAVA_TOKEN_EXPIRES_AT",
        "DEV_STRAVA_ATHLETE_ID",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        print("Run scripts/strava_oauth_dance.py first.")
        raise SystemExit(1)

    # Reasonable defaults for the demo profile. You can edit these in the DB
    # (or via /profile once phase 4.5 wires the bot to the DB) afterwards.
    age = 28
    max_hr = estimate_max_hr(age)
    z1, z2, z3, z4 = compute_default_zones(max_hr)

    expires_at = datetime.fromtimestamp(int(os.environ["DEV_STRAVA_TOKEN_EXPIRES_AT"]), tz=UTC)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_user_id == settings.BOOTSTRAP_ADMIN_TELEGRAM_USER_ID)
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                telegram_user_id=settings.BOOTSTRAP_ADMIN_TELEGRAM_USER_ID,
                telegram_chat_id=settings.BOOTSTRAP_ADMIN_TELEGRAM_USER_ID,
                athlete_id=int(os.environ["DEV_STRAVA_ATHLETE_ID"]),
                strava_access_token=os.environ["DEV_STRAVA_ACCESS_TOKEN"],
                strava_refresh_token=os.environ["DEV_STRAVA_REFRESH_TOKEN"],
                strava_token_expires_at=expires_at,
                is_admin=True,
                timezone="Asia/Singapore",
                age=age,
                weight_kg=70.0,
                recent_5k_secs=22 * 60,  # 22:00
                recent_10k_secs=46 * 60,  # 46:00
                available_days_json=["Mon", "Wed", "Fri", "Sun"],
                current_injury=None,
                max_hr=max_hr,
                hr_zone1_max=z1,
                hr_zone2_max=z2,
                hr_zone3_max=z3,
                hr_zone4_max=z4,
                weekly_volume_goal_km=50.0,
            )
            session.add(user)
            action = "CREATED"
        else:
            user.athlete_id = int(os.environ["DEV_STRAVA_ATHLETE_ID"])
            user.strava_access_token = os.environ["DEV_STRAVA_ACCESS_TOKEN"]
            user.strava_refresh_token = os.environ["DEV_STRAVA_REFRESH_TOKEN"]
            user.strava_token_expires_at = expires_at
            user.is_admin = True
            action = "UPDATED"

        await session.commit()
        await session.refresh(user)

    print(f"{action}: {user!r}")
    print(f"  telegram_user_id: {user.telegram_user_id}")
    print(f"  athlete_id:       {user.athlete_id}")
    print(f"  is_admin:         {user.is_admin}")
    print(f"  timezone:         {user.timezone}")
    print(f"  age/weight/maxHR: {user.age} / {user.weight_kg}kg / {user.max_hr}bpm")
    print(
        f"  HR zones:         Z1≤{user.hr_zone1_max}  Z2≤{user.hr_zone2_max}  "
        f"Z3≤{user.hr_zone3_max}  Z4≤{user.hr_zone4_max}"
    )
    print(f"  weekly target:    {user.weekly_volume_goal_km} km")
    print(f"  available days:   {user.available_days_json}")


if __name__ == "__main__":
    asyncio.run(main())
