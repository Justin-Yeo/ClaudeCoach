"""OAuthState ORM model — CSRF protection for the Strava OAuth callback.

Mirrors [schema.md §9](schema.md). One-shot tokens with a 5-minute lifetime.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)

    # We don't FK this to users.id because the user record doesn't exist yet
    # at the point /start is invoked — the OAuth callback creates it.
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Reference to the invite_codes.code that's about to be consumed.
    # NULL for the admin-bootstrap path (admin skips the invite-code requirement).
    invite_code: Mapped[str | None] = mapped_column(
        ForeignKey("invite_codes.code", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    def __repr__(self) -> str:
        return f"<OAuthState {self.state[:8]}... tg={self.telegram_user_id}>"
