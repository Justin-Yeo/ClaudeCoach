"""GET /auth/strava/callback — Strava OAuth redirect handler.

Implements the callback half of the onboarding flow specified in
[spec.md §3.1](spec.md). Validates the `state` token issued by `/start`,
exchanges the `code` for Strava tokens, and atomically creates (or refreshes)
the `users` row, consumes the invite code, and deletes the `oauth_states` row.

Three onboarding paths converge here:
  1. Existing user reconnecting → UPDATE tokens
  2. New admin (telegram_user_id matches BOOTSTRAP_ADMIN_TELEGRAM_USER_ID) → INSERT, is_admin=true
  3. New friend with invite code → INSERT, consume invite_code

After commit, the bot DMs the user a "✅ connected" confirmation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models import InviteCode, OAuthState, User
from app.services.strava import StravaAuthError, exchange_code_for_tokens

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/auth/strava/callback", response_class=HTMLResponse)
async def strava_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> str:
    if error:
        log.warning("auth.strava.user_denied error=%s", error)
        return _html("Authorization denied", f"Strava returned: {error}")
    if not code or not state:
        return _html("Missing parameters", "Both `code` and `state` are required.")

    settings = get_settings()

    async with AsyncSessionLocal() as session:
        # 1. Validate state — must exist AND not be expired.
        oauth_state = await session.scalar(
            select(OAuthState).where(
                OAuthState.state == state,
                OAuthState.expires_at > datetime.now(UTC),
            )
        )
        if oauth_state is None:
            log.warning("auth.strava.invalid_state state=%s", state[:8] if state else "?")
            return _html(
                "OAuth session expired",
                "The link expired or was already used. Run /start again from Telegram.",
            )

        telegram_user_id = oauth_state.telegram_user_id
        invite_code_str = oauth_state.invite_code

        # 2. Exchange the one-shot code for tokens.
        try:
            token_data = await exchange_code_for_tokens(
                client_id=settings.STRAVA_CLIENT_ID,
                client_secret=settings.STRAVA_CLIENT_SECRET,
                code=code,
            )
        except StravaAuthError as exc:
            log.warning("auth.strava.exchange_failed: %s", exc)
            return _html(
                "Strava rejected the code",
                "Try /start again from Telegram. (Codes are single-use and short-lived.)",
            )

        # Defensive parse — Strava is contractually obliged to return all of
        # access_token, refresh_token, expires_at, and athlete.id, but if the
        # response is malformed we want a friendly error page, not a 500.
        athlete = token_data.get("athlete") or {}
        athlete_id = athlete.get("id")
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_at_unix = token_data.get("expires_at")
        if not all([athlete_id, access_token, refresh_token, expires_at_unix]):
            log.error("auth.strava.malformed_token_response keys=%s", list(token_data))
            return _html(
                "Bad response from Strava",
                "Couldn't read all the required fields. Try /start again.",
            )

        expires_at = datetime.fromtimestamp(expires_at_unix, tz=UTC)
        # Strava returns timezone as `(GMT+08:00) Asia/Singapore` — a formatted
        # string, NOT a valid IANA zone. Extract the IANA portion (after the
        # `) `), or default to UTC if parsing looks weird.
        raw_tz = athlete.get("timezone") or ""
        athlete_tz = raw_tz.rsplit(") ", 1)[-1].strip() if ") " in raw_tz else "UTC"
        if not athlete_tz:
            athlete_tz = "UTC"

        # 3. Look up existing user (by telegram_user_id) or create a new one.
        existing = await session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )

        try:
            if existing is not None:
                existing.athlete_id = athlete_id
                existing.strava_access_token = access_token
                existing.strava_refresh_token = refresh_token
                existing.strava_token_expires_at = expires_at
                user = existing
                action = "reconnected"
            else:
                is_admin = telegram_user_id == settings.BOOTSTRAP_ADMIN_TELEGRAM_USER_ID
                user = User(
                    telegram_user_id=telegram_user_id,
                    # In a 1-on-1 chat with a bot, chat_id == user_id. Spec.md §3.1
                    # restricts the bot to private chats, so this is correct.
                    telegram_chat_id=telegram_user_id,
                    athlete_id=athlete_id,
                    strava_access_token=access_token,
                    strava_refresh_token=refresh_token,
                    strava_token_expires_at=expires_at,
                    is_admin=is_admin,
                    timezone=athlete_tz,
                )
                session.add(user)
                await session.flush()  # populate user.id for the invite update below
                action = "created"

            # 4. Consume the invite code if one was supplied.
            if invite_code_str:
                invite = await session.scalar(
                    select(InviteCode).where(InviteCode.code == invite_code_str)
                )
                # Re-check unused — an attacker could have raced two callbacks
                # against the same code. The state-row deletion below makes that
                # impossible in practice, but defensive belt-and-suspenders.
                if invite is not None and invite.used_by is None:
                    invite.used_by = user.id
                    invite.used_at = datetime.now(UTC)

            # 5. Delete the oauth_state — one-shot, never reused.
            await session.delete(oauth_state)

            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            log.warning("auth.strava.integrity_error %s", exc)
            return _html(
                "Strava account already linked",
                "That Strava account is already linked to another user of this bot.",
            )

    # 6. DM the user via the bot. Failures here are logged but don't break the
    # onboarding — the success page tells them to return to Telegram anyway.
    try:
        bot = request.app.state.bot
        await bot.send_message(
            chat_id=user.telegram_chat_id,
            text=(
                "✅ *Strava connected*\n\n"
                "Set up your runner profile with /profile, "
                "then a goal with /goal\\."
            ),
            parse_mode="MarkdownV2",
        )
    except Exception:
        log.exception("auth.strava.dm_failed user_id=%s", user.id)

    log.info(
        "auth.strava.success user_id=%s telegram_user_id=%s action=%s",
        user.id,
        telegram_user_id,
        action,
    )
    title = "Reconnected!" if action == "reconnected" else "Connected!"
    body = (
        "Strava is linked. You can close this tab and return to Telegram. "
        "Run /profile to set up your runner profile, then /goal."
    )
    return _html(title, body)


def _html(title: str, body: str) -> str:
    """Render a tiny HTML page for the OAuth landing.

    `title` and `body` are author-controlled (no user input is interpolated),
    so no escaping is needed.
    """
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:520px;margin:80px auto;"
        "padding:0 24px;color:#222;line-height:1.5}</style>"
        "</head><body>"
        f"<h1>{title}</h1><p>{body}</p>"
        "</body></html>"
    )
