"""Small DB lookup helpers used by bot command handlers.

Keep these tiny and side-effect-free. Real business logic belongs in
`app/services/coaching.py` (or a new service module if it grows).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    """Look up the User row for a given Telegram user id, or None if not registered."""
    return await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
