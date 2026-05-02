"""User ORM model.

Mirrors [schema.md §7](schema.md). Sections in this file match the schema:

- A: Identity & Auth        (id, telegram_*, athlete_id, strava tokens, admin, tz, ts)
- B: Profile                (age, weight, baseline times, available days, injury)
- C: HR Zones               (max_hr, hr_zone1_max..hr_zone4_max)
- D: Goals                  (weekly volume, race_*)
- E: Coaching State         (next_planned_session_*)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    # ----------------------------------------------------- A · Identity & Auth
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    athlete_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)

    strava_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    strava_refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable as a sentinel: NULL = disconnected (refresh failed permanently).
    # See spec.md §3.4 step 3 and §11 admin bootstrap.
    strava_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # --------------------------------------------------------- B · Profile
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_5k_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recent_10k_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_days_json: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    current_injury: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --------------------------------------------------------- C · HR Zones
    max_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone1_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone2_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone3_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hr_zone4_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --------------------------------------------------------- D · Goals
    weekly_volume_goal_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    race_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    race_distance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    race_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    race_target_secs: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --------------------------------------------------------- E · Coaching State
    next_planned_session_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    next_planned_session_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 'post_run' | 'plan_command'
    next_planned_session_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # `use_alter=True` breaks the circular FK between users <-> runs so Alembic
    # creates this FK via ALTER TABLE after both tables exist.
    next_planned_session_run_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "runs.id",
            ondelete="SET NULL",
            name="fk_users_next_planned_session_run_id",
            use_alter=True,
        ),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} tg={self.telegram_user_id} athlete={self.athlete_id}>"
