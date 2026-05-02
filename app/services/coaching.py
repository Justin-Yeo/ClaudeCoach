"""Coaching pipeline orchestrator.

The `ingest_run(athlete_id, activity_id, bot)` background task is the heart of
the production system. Called by the `/webhook` route's `BackgroundTasks` after
returning 200 to Strava, it executes the full pipeline from [spec.md §3.4](spec.md):

    1. Look up user by athlete_id (skip if unknown)
    2. Fetch activity via Strava (refresh token on 401, DM reconnect on permanent fail)
    3. Filter — only `Run` activities pass through
    4. Fetch streams + compute derived metrics
    5. Persist a new `runs` row (idempotent on duplicate strava_activity_id)
    6. Completeness gate — DM "finish setup" if profile/goal missing, stop
    7. Race day cleanup if `race_date < today`
    8. Fetch last 4 weeks of run history
    9. Call Claude with tool use → parsed coaching response
    10. Persist Claude output + mirror next session to `users.next_planned_session_json`
    11. Send 2 Telegram messages: post-run review + next session
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot

from app.db import AsyncSessionLocal
from app.models import Run, User
from app.prompts.v1 import PROMPT_VERSION
from app.services import metrics
from app.services.claude import call_claude_coaching, compute_next_available_day
from app.services.strava import StravaAuthError, client_for_user
from app.services.telegram import (
    dm_coaching_unreachable,
    dm_race_day_cleanup,
    dm_reconnect_link,
    dm_setup_incomplete,
    send_next_session,
    send_post_run_review,
)

log = logging.getLogger(__name__)


# ============================================================================
# Public entry point
# ============================================================================


async def ingest_run(athlete_id: int, activity_id: int, bot: Bot) -> None:
    """Background task triggered by a Strava `aspect_type='create'` webhook.

    Catches all expected exceptions and logs them; never raises out so a
    single bad run doesn't take down the whole server. The webhook handler
    has already returned 200 to Strava by the time this runs.
    """
    log.info("ingest_run.start athlete_id=%s activity_id=%s", athlete_id, activity_id)
    try:
        async with AsyncSessionLocal() as session:
            await _ingest_run_inner(session, athlete_id, activity_id, bot)
    except Exception:
        log.exception("ingest_run.failed athlete_id=%s activity_id=%s", athlete_id, activity_id)


async def _ingest_run_inner(
    session: AsyncSession, athlete_id: int, activity_id: int, bot: Bot
) -> None:
    # 1. Look up user
    user = await session.scalar(select(User).where(User.athlete_id == athlete_id))
    if user is None:
        log.info("ingest_run.no_user athlete_id=%s", athlete_id)
        return

    # 2. Fetch activity (with auto-refresh)
    strava = client_for_user(user)
    try:
        try:
            activity = await strava.get_activity(activity_id)
        except StravaAuthError:
            log.warning("ingest_run.disconnected user_id=%s", user.id)
            user.strava_token_expires_at = None  # disconnected sentinel
            await session.commit()
            await dm_reconnect_link(bot, user.telegram_chat_id)
            return

        # 3. Activity type filter
        if activity.get("type") != "Run":
            log.info(
                "ingest_run.skip_non_run user_id=%s type=%s",
                user.id,
                activity.get("type"),
            )
            return

        # 4. Fetch streams
        streams = await strava.get_activity_streams(activity_id)
    finally:
        await strava.aclose()

    # 5. Persist run row
    run = _build_run_row(user, activity, streams)
    session.add(run)
    try:
        await session.flush()  # surfaces IntegrityError without ending transaction
    except IntegrityError:
        await session.rollback()
        log.info(
            "ingest_run.duplicate user_id=%s strava_activity_id=%s",
            user.id,
            activity_id,
        )
        return

    # Need to commit the user-token updates the refresh callback may have made,
    # plus the new run row, before any further DB work.
    await session.commit()
    await session.refresh(run)
    await session.refresh(user)

    log.info(
        "ingest_run.run_persisted user_id=%s run_id=%s distance=%.2fkm",
        user.id,
        run.id,
        run.distance_m / 1000,
    )

    # 6. Completeness gate
    if not _is_profile_complete(user):
        log.info("ingest_run.profile_incomplete user_id=%s", user.id)
        await dm_setup_incomplete(bot, user.telegram_chat_id)
        return
    if not _has_goal(user):
        log.info("ingest_run.no_goal user_id=%s", user.id)
        await dm_setup_incomplete(bot, user.telegram_chat_id)
        return

    # 7. Race day cleanup
    today_local = _today_local(user)
    race_was_cleared = False
    if user.race_date and user.race_date < today_local:
        log.info("ingest_run.race_day_cleanup user_id=%s", user.id)
        user.race_date = None
        user.race_distance = None
        user.race_distance_m = None
        user.race_target_secs = None
        race_was_cleared = True
        await session.commit()

    # 8. Last 4 weeks of run history
    history = await _fetch_recent_runs(session, user.id, weeks=4)

    # 9. Compute next available day + weekly volume done
    next_day = compute_next_available_day(today_local, user.available_days_json or [])
    weekly_done = _compute_weekly_volume_done(history, today_local) + (
        run.distance_m / 1000  # include the run we just ingested
    )

    # 10. Call Claude
    try:
        response = await call_claude_coaching(
            user=_user_to_dict(user),
            goals=_goals_to_dict(user),
            current_run=_run_to_dict(run),
            recent_runs=[_history_run_to_dict(h, user) for h in history],
            today_local=today_local,
            weekly_volume_done_km=weekly_done,
            next_available_day=next_day,
        )
    except Exception:
        log.exception("ingest_run.claude_failed user_id=%s", user.id)
        await dm_coaching_unreachable(bot, user.telegram_chat_id)
        return

    # 11. Persist Claude output
    review = response["post_run_review"]
    run.claude_post_run_review = (
        f"Run Summary\n{review['run_summary']}\n\n"
        f"What Went Well\n{review['went_well']}\n\n"
        f"What to Watch\n{review['to_watch']}"
    )
    run.claude_digest = review["digest"]
    run.claude_next_session = response["next_session"]
    run.claude_load_rating = response["load_rating"]
    run.claude_flags = response["flags"]
    run.run_type = response["run_type"]
    run.prompt_version = PROMPT_VERSION
    run.processed_at = datetime.now(UTC)

    user.next_planned_session_json = response["next_session"]
    user.next_planned_session_updated_at = datetime.now(UTC)
    user.next_planned_session_source = "post_run"
    user.next_planned_session_run_id = run.id

    await session.commit()

    # 12. Send Telegram messages
    if race_was_cleared:
        await dm_race_day_cleanup(bot, user.telegram_chat_id)
    await send_post_run_review(bot, user.telegram_chat_id, review)
    await send_next_session(bot, user.telegram_chat_id, response["next_session"])

    log.info(
        "ingest_run.complete user_id=%s run_id=%s run_type=%s",
        user.id,
        run.id,
        run.run_type,
    )


# ============================================================================
# Internal helpers
# ============================================================================


def _is_profile_complete(user: User) -> bool:
    """Required profile fields per spec.md §3.4 step 8."""
    if user.age is None or user.weight_kg is None or user.max_hr is None:
        return False
    if user.recent_5k_secs is None and user.recent_10k_secs is None:
        return False
    return bool(user.available_days_json)


def _has_goal(user: User) -> bool:
    """At least one of weekly volume or race goal must be set."""
    return user.weekly_volume_goal_km is not None or user.race_date is not None


def _today_local(user: User) -> date:
    tz = ZoneInfo(user.timezone or "UTC")
    return datetime.now(tz).date()


async def _fetch_recent_runs(session: AsyncSession, user_id: int, weeks: int) -> list[Run]:
    cutoff = datetime.now(UTC) - timedelta(weeks=weeks)
    result = await session.scalars(
        select(Run)
        .where(Run.user_id == user_id, Run.start_date >= cutoff)
        .order_by(desc(Run.start_date))
    )
    return list(result.all())


def _compute_weekly_volume_done(history: list[Run], today_local: date) -> float:
    """Sum of distance_km for runs in the current ISO week (Mon–Sun)."""
    week_start = today_local - timedelta(days=today_local.weekday())
    week_start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
    return sum(r.distance_m for r in history if r.start_date >= week_start_dt) / 1000


# ----------------------------------------------------------------- ORM → dict


def _user_to_dict(user: User) -> dict:
    return {
        "age": user.age,
        "weight_kg": user.weight_kg,
        "max_hr": user.max_hr,
        "recent_5k_secs": user.recent_5k_secs,
        "recent_10k_secs": user.recent_10k_secs,
        "available_days": list(user.available_days_json or []),
        "current_injury": user.current_injury,
        "hr_zone1_max": user.hr_zone1_max,
        "hr_zone2_max": user.hr_zone2_max,
        "hr_zone3_max": user.hr_zone3_max,
        "hr_zone4_max": user.hr_zone4_max,
    }


def _goals_to_dict(user: User) -> dict:
    return {
        "weekly_volume_goal_km": user.weekly_volume_goal_km,
        "race_date": user.race_date.isoformat() if user.race_date else None,
        "race_distance": user.race_distance,
        "race_distance_m": user.race_distance_m,
        "race_target_secs": user.race_target_secs,
    }


def _run_to_dict(run: Run) -> dict:
    """Convert a `Run` ORM row to the shape `claude.build_user_prompt` expects."""
    return {
        "start_date_local": run.start_date.date().isoformat(),
        "distance_m": run.distance_m,
        "duration_secs": run.duration_secs,
        "elevation_gain_m": run.elevation_gain_m,
        "avg_pace_min_km": run.avg_pace_min_km,
        "avg_hr": run.avg_hr,
        "max_hr": run.max_hr,
        "grade_avg_pct": run.grade_avg_pct,
        "pace_first_half_min": run.pace_first_half_min,
        "pace_second_half_min": run.pace_second_half_min,
        "fastest_km_pace_min": run.fastest_km_pace_min,
        "slowest_km_pace_min": run.slowest_km_pace_min,
        "pace_std_dev_min": run.pace_std_dev_min,
        "gap_min_km": run.gap_min_km,
        "pace_splits": run.pace_splits_json or [],
        "grade_splits": run.grade_splits_json or [],
        "flat_distance_m": run.flat_distance_m,
        "uphill_distance_m": run.uphill_distance_m,
        "downhill_distance_m": run.downhill_distance_m,
        "hr_zones_secs": [
            run.hr_zone1_secs or 0,
            run.hr_zone2_secs or 0,
            run.hr_zone3_secs or 0,
            run.hr_zone4_secs or 0,
            run.hr_zone5_secs or 0,
        ]
        if run.avg_hr is not None
        else None,
        "cardiac_drift_bpm": run.cardiac_drift_bpm,
        "aerobic_decoupling": run.aerobic_decoupling,
        "efficiency_factor": run.efficiency_factor,
        "cadence_avg": run.cadence_avg,
        "cadence_std_dev": run.cadence_std_dev,
        "cadence_under170_pct": run.cadence_under170_pct,
    }


def _history_run_to_dict(run: Run, user: User) -> dict:
    """Compact representation for the `recent_runs_block` of the prompt."""
    tz = ZoneInfo(user.timezone or "UTC")
    local_date = run.start_date.astimezone(tz)
    return {
        "date": local_date.strftime("%a %d %b"),
        "run_type": run.run_type or "run",
        "distance_km": run.distance_m / 1000,
        "avg_pace_min_km": run.avg_pace_min_km,
        "avg_hr": run.avg_hr,
    }


# ----------------------------------------------------------------- run row builder


def _build_run_row(user: User, activity: dict, streams: dict) -> Run:
    """Run all metric computations and build a `Run` ORM row from the result.

    See [METRICS.md](METRICS.md) for the formulas.
    """
    d = streams.get("distance", {}).get("data") or []
    t = streams.get("time", {}).get("data") or []
    v = streams.get("velocity_smooth", {}).get("data") or []
    alt = streams.get("altitude", {}).get("data") or []
    grade = streams.get("grade_smooth", {}).get("data") or []
    hr = metrics.get_stream_data(streams, "heartrate")
    cad = metrics.get_stream_data(streams, "cadence")

    pace_splits = metrics.compute_pace_splits(d, t)
    pace_first, pace_second = metrics.compute_half_paces(d, t)
    fastest, slowest = metrics.compute_fastest_slowest_km(pace_splits)
    flat, up, down = metrics.compute_grade_distances(d, grade)

    hr_zones: list[int | None] = [None] * 5
    if hr is not None and user.hr_zone1_max is not None:
        bounds = (
            user.hr_zone1_max,
            user.hr_zone2_max,
            user.hr_zone3_max,
            user.hr_zone4_max,
        )
        hr_zones = list(metrics.compute_hr_zones(t, hr, bounds))  # type: ignore[arg-type]

    start_date = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))

    return Run(
        user_id=user.id,
        strava_activity_id=activity["id"],
        start_date=start_date,
        timezone=activity.get("timezone", user.timezone or "UTC"),
        workout_type=activity.get("workout_type") or 0,
        run_type=None,  # populated by Claude later
        # Summary
        distance_m=float(activity["distance"]),
        duration_secs=int(activity["moving_time"]),
        elapsed_secs=int(activity["elapsed_time"]),
        elevation_gain_m=float(activity.get("total_elevation_gain", 0)),
        elevation_high_m=float(activity.get("elev_high", 0)),
        elevation_low_m=float(activity.get("elev_low", 0)),
        avg_pace_min_km=metrics.compute_avg_pace_min_km(
            float(activity["distance"]), float(activity["moving_time"])
        )
        or 0.0,
        avg_speed_ms=float(activity.get("average_speed", 0)),
        max_speed_ms=float(activity.get("max_speed", 0)),
        # Pace
        pace_first_half_min=pace_first or 0.0,
        pace_second_half_min=pace_second or 0.0,
        pace_std_dev_min=metrics.compute_pace_std_dev(pace_splits),
        fastest_km_pace_min=fastest,
        slowest_km_pace_min=slowest,
        pace_splits_json=pace_splits,
        gap_min_km=metrics.compute_gap(d, t, grade),
        # Elevation
        grade_avg_pct=metrics.compute_grade_avg(alt, d),
        flat_distance_m=flat,
        uphill_distance_m=up,
        downhill_distance_m=down,
        grade_splits_json=metrics.compute_grade_splits(d, grade),
        # HR
        avg_hr=int(activity["average_heartrate"]) if activity.get("average_heartrate") else None,
        max_hr=int(activity["max_heartrate"]) if activity.get("max_heartrate") else None,
        hr_zone1_secs=hr_zones[0],
        hr_zone2_secs=hr_zones[1],
        hr_zone3_secs=hr_zones[2],
        hr_zone4_secs=hr_zones[3],
        hr_zone5_secs=hr_zones[4],
        cardiac_drift_bpm=metrics.compute_cardiac_drift(t, hr) if hr else None,
        aerobic_decoupling=(metrics.compute_aerobic_decoupling(t, hr, v) if hr else None),
        efficiency_factor=(
            metrics.compute_efficiency_factor(
                float(activity.get("average_speed", 0)),
                activity.get("average_heartrate"),
            )
            if hr
            else None
        ),
        hr_vs_pace_json=(metrics.compute_hr_vs_pace(d, t, hr, pace_splits) if hr else None),
        hr_zone_source="computed_from_max_hr" if hr else None,
        # Cadence
        cadence_avg=metrics.compute_cadence_avg(cad) if cad else None,
        cadence_std_dev=metrics.compute_cadence_std_dev(cad) if cad else None,
        cadence_under170_pct=(metrics.compute_cadence_under170_pct(t, cad) if cad else None),
        cadence_splits_json=metrics.compute_cadence_splits(d, cad) if cad else None,
        # Stream archive
        stream_time_json=t,
        stream_distance_json=d,
        stream_velocity_json=v,
        stream_altitude_json=alt,
        stream_grade_json=grade,
        stream_hr_json=hr,
        stream_cadence_json=cad,
    )


__all__ = ["ingest_run"]
