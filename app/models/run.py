"""Run ORM model.

Mirrors [schema.md §4](schema.md). Sections match the schema:

- A: Identity & Metadata   (id, user_id FK, strava_activity_id, dates, types)
- B: Summary Stats         (distance, time, elevation, avg pace/HR)
- C: Pace Analysis         (half splits, std dev, fastest/slowest, splits json, GAP)
- D: Elevation & Grade     (avg grade, distance buckets, grade splits)
- E: Heart Rate            (zones, drift, decoupling, EF, hr_vs_pace)
- F: Cadence               (avg, std dev, under170, splits)
- G: Stream Archive        (raw stream JSON arrays)
- H: Claude Output         (post-run review, digest, next session, load, flags, version)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("strava_activity_id", name="uq_runs_strava_activity_id"),)

    # ----------------------------------------------------- A · Identity & Metadata
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    strava_activity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    workout_type: Mapped[int] = mapped_column(Integer, nullable=False)

    # Fixed taxonomy from spec.md §4: easy | long | tempo | intervals | recovery | race
    run_type: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # ----------------------------------------------------- B · Summary Stats
    distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    duration_secs: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_secs: Mapped[int] = mapped_column(Integer, nullable=False)
    elevation_gain_m: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_high_m: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_low_m: Mapped[float] = mapped_column(Float, nullable=False)
    avg_pace_min_km: Mapped[float] = mapped_column(Float, nullable=False)
    avg_speed_ms: Mapped[float] = mapped_column(Float, nullable=False)
    max_speed_ms: Mapped[float] = mapped_column(Float, nullable=False)

    # ----------------------------------------------------- C · Pace Analysis
    pace_first_half_min: Mapped[float] = mapped_column(Float, nullable=False)
    pace_second_half_min: Mapped[float] = mapped_column(Float, nullable=False)
    pace_std_dev_min: Mapped[float] = mapped_column(Float, nullable=False)
    fastest_km_pace_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    slowest_km_pace_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    pace_splits_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    gap_min_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ----------------------------------------------------- D · Elevation & Grade
    grade_avg_pct: Mapped[float] = mapped_column(Float, nullable=False)
    flat_distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    uphill_distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    downhill_distance_m: Mapped[float] = mapped_column(Float, nullable=False)
    grade_splits_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # ----------------------------------------------------- E · Heart Rate (P2 — nullable)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone1_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone2_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone3_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone4_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone5_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cardiac_drift_bpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    aerobic_decoupling: Mapped[float | None] = mapped_column(Float, nullable=True)
    efficiency_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    hr_vs_pace_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    hr_zone_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ----------------------------------------------------- F · Cadence (P3 — nullable)
    cadence_avg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cadence_std_dev: Mapped[float | None] = mapped_column(Float, nullable=True)
    cadence_under170_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cadence_splits_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    # ----------------------------------------------------- G · Stream Archive
    # Raw stream arrays from Strava — kept so we can recompute metrics later if
    # the formulas change, without re-hitting the Strava API.
    stream_time_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_distance_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_velocity_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_altitude_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_grade_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_hr_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    stream_cadence_json: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)

    # ----------------------------------------------------- H · Claude Output
    claude_post_run_review: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_next_session: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    claude_load_rating: Mapped[str | None] = mapped_column(String(16), nullable=True)
    claude_flags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Run id={self.id} user={self.user_id} "
            f"strava={self.strava_activity_id} dist={self.distance_m / 1000:.2f}km>"
        )
