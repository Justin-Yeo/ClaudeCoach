"""Telegram bot static commands and handler registration.

Wires `/start`, `/help`, `/cancel`, `/history`, `/plan`, `/status`, `/injury`
plus the `/profile` and `/goal` conversation handlers (defined in
`app/bot/conversations.py`) to the `Application`.

Phase 3: `/start` is a stub that just echoes the user's id and args.
Phase 8 replaces it with the full invite-code + OAuth onboarding flow.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from app.bot.conversations import build_goal_handler, build_profile_handler
from app.bot.db_helpers import get_user_by_telegram_id
from app.db import AsyncSessionLocal
from app.models import Run
from app.services.claude import call_claude_coaching, compute_next_available_day
from app.services.coaching import (
    _compute_weekly_volume_done,
    _fetch_recent_runs,
    _goals_to_dict,
    _has_goal,
    _history_run_to_dict,
    _is_profile_complete,
    _run_to_dict,
    _user_to_dict,
)
from app.services.pace import format_pace_min
from app.services.telegram import escape_md_v2, send_next_session

log = logging.getLogger(__name__)

# Single source of truth for the /help output. Mirrors the BotFather command
# menu list in spec.md §11.20 (with the addition of /invite for admins).
HELP_LINES: list[tuple[str, str]] = [
    ("/start <code>", "Begin onboarding (with invite code) or reconnect Strava"),
    ("/help", "Show this command list"),
    ("/profile", "View or edit your runner profile"),
    ("/goal", "View or edit your training goals"),
    ("/plan", "Get next session recommendation right now"),
    ("/history", "Show recent runs and coaching summaries"),
    ("/injury", "Set or clear current injury / niggle note"),
    ("/status", "Connection state, goals, week progress, next session"),
    ("/cancel", "Exit any active conversation"),
]


# ----------------------------------------------------------------- /start (stub)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Phase 3 stub. Phase 8 replaces with invite-code + OAuth onboarding."""
    user = update.effective_user
    args_str = " ".join(context.args) if context.args else "(no args)"

    msg = (
        f"Hello {escape_md_v2(user.first_name or 'runner')}\\!\n\n"
        f"You sent `/start` with args: `{escape_md_v2(args_str)}`\n\n"
        f"Your Telegram user id: `{user.id}`\n\n"
        "_Phase 8 will replace this stub with the full invite\\-code \\+ Strava OAuth flow\\._"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ----------------------------------------------------------------- /help


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Print every command with a one-liner. Static content."""
    lines = ["*ClaudeCoach commands*", ""]
    for cmd, description in HELP_LINES:
        lines.append(f"`{escape_md_v2(cmd)}` — {escape_md_v2(description)}")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ----------------------------------------------------------------- /cancel (top-level)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top-level /cancel — only fires when no ConversationHandler is active.

    Inside an active conversation, the ConversationHandler's own /cancel
    fallback runs first and ends the flow with "Cancelled, no changes saved."
    """
    await update.message.reply_text(
        escape_md_v2("Nothing to cancel — no active conversation."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ----------------------------------------------------------------- helper


async def _send_plain(update: Update, text: str) -> None:
    """Send a plain-text message escaped for MarkdownV2."""
    await update.message.reply_text(escape_md_v2(text), parse_mode=ParseMode.MARKDOWN_V2)


def _fmt_pace_time(secs: int | None) -> str:
    if secs is None:
        return "not set"
    return f"{secs // 60}:{secs % 60:02d}"


def _fmt_duration(secs: int) -> str:
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ----------------------------------------------------------------- /injury


async def injury_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set, view, or clear the user's current injury note.

    /injury           → show current value
    /injury <text>    → set to <text>
    /injury clear     → clear
    """
    args_str = " ".join(context.args).strip() if context.args else ""

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if user is None:
            await _send_plain(update, "You're not registered. Use /start <code> first.")
            return

        if not args_str:
            current = user.current_injury or "(none)"
            await _send_plain(
                update,
                f"Current injury note: {current}\n\n"
                "Set with: /injury <text>\nClear with: /injury clear",
            )
            return

        if args_str.lower() == "clear":
            user.current_injury = None
            await session.commit()
            await _send_plain(update, "Injury note cleared.")
            return

        if len(args_str) > 200:
            await _send_plain(update, "Injury note must be 200 characters or less.")
            return

        user.current_injury = args_str
        await session.commit()
        await _send_plain(update, f"Injury note saved: {args_str}")


# ----------------------------------------------------------------- /history


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last N runs (default 5, max 20) with their coaching digests."""
    n = 5
    if context.args:
        with contextlib.suppress(ValueError):
            n = max(1, min(20, int(context.args[0])))

    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if user is None:
            await _send_plain(update, "You're not registered. Use /start <code> first.")
            return

        runs = (
            await session.scalars(
                select(Run).where(Run.user_id == user.id).order_by(desc(Run.start_date)).limit(n)
            )
        ).all()

        if not runs:
            await _send_plain(
                update,
                "No runs yet — upload one to Strava and I'll send your first "
                "coaching message automatically.",
            )
            return

        tz = ZoneInfo(user.timezone or "UTC")
        cards = []
        for run in runs:
            local = run.start_date.astimezone(tz)
            date_str = local.strftime("%a %d %b")
            run_type = run.run_type or "run"
            distance = f"{run.distance_m / 1000:.1f}km"
            duration = _fmt_duration(run.duration_secs)
            pace = format_pace_min(run.avg_pace_min_km) if run.avg_pace_min_km else "n/a"
            hr = f"{round(run.avg_hr)}bpm" if run.avg_hr else "—"

            card = (
                f"*{escape_md_v2(date_str)} · {escape_md_v2(run_type)}*\n"
                f"{escape_md_v2(distance)} · {escape_md_v2(duration)} · "
                f"{escape_md_v2(pace)} · {escape_md_v2(hr)}"
            )
            if run.claude_digest:
                card += f'\n_"{escape_md_v2(run.claude_digest)}"_'
            cards.append(card)

        message = "\n\n".join(cards)
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN_V2)


# ----------------------------------------------------------------- /plan


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a fresh next-session recommendation on demand.

    Uses the most recent run as context. Overwrites
    `users.next_planned_session_json` with `source='plan_command'`.
    """
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if user is None:
            await _send_plain(update, "You're not registered. Use /start <code> first.")
            return

        if not _is_profile_complete(user):
            await _send_plain(update, "Finish your profile first with /profile.")
            return
        if not _has_goal(user):
            await _send_plain(update, "Set a goal first with /goal.")
            return

        history = await _fetch_recent_runs(session, user.id, weeks=4)
        if not history:
            await _send_plain(
                update,
                "No runs in the last 4 weeks — upload one to Strava first, "
                "then I can plan your next session.",
            )
            return

        await _send_plain(update, "Generating plan…")

        today = datetime.now(ZoneInfo(user.timezone or "UTC")).date()
        next_day = compute_next_available_day(today, user.available_days_json or [])
        weekly_done = _compute_weekly_volume_done(history, today)

        # Use the most recent run as the "current run" context for Claude
        latest = history[0]
        current_run_dict = _run_to_dict(latest)

        try:
            response = await call_claude_coaching(
                user=_user_to_dict(user),
                goals=_goals_to_dict(user),
                current_run=current_run_dict,
                recent_runs=[_history_run_to_dict(h, user) for h in history],
                today_local=today,
                weekly_volume_done_km=weekly_done,
                next_available_day=next_day,
            )
        except Exception:
            log.exception("plan_command.claude_failed user_id=%s", user.id)
            await _send_plain(
                update,
                "Coaching engine unreachable. Try again in a moment.",
            )
            return

        # Persist the new plan
        user.next_planned_session_json = response["next_session"]
        user.next_planned_session_updated_at = datetime.now(UTC)
        user.next_planned_session_source = "plan_command"
        user.next_planned_session_run_id = None
        await session.commit()

        await send_next_session(context.bot, update.effective_chat.id, response["next_session"])


# ----------------------------------------------------------------- /status


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's snapshot — Strava state, profile, goals, weekly progress,
    last run, next session. See [spec.md §3.8](spec.md)."""
    async with AsyncSessionLocal() as session:
        user = await get_user_by_telegram_id(session, update.effective_user.id)
        if user is None:
            await _send_plain(update, "You're not registered. Use /start <code> first.")
            return

        sections: list[str] = []

        # Strava state
        if user.strava_token_expires_at is None:
            sections.append("🔴 *Strava disconnected*\n\nRe\\-link with /start")
        elif user.strava_token_expires_at < datetime.now(UTC):
            sections.append("🟡 *Strava token expired \\(refreshing on next run\\)*")
        else:
            sections.append("🟢 *Strava connected*")

        # Profile
        if _is_profile_complete(user):
            baseline = []
            if user.recent_5k_secs:
                baseline.append(f"5K {_fmt_pace_time(user.recent_5k_secs)}")
            if user.recent_10k_secs:
                baseline.append(f"10K {_fmt_pace_time(user.recent_10k_secs)}")
            baseline_str = " · ".join(baseline)
            days_str = ", ".join(user.available_days_json or [])
            sections.append(
                "*Profile*\n"
                f"Age {user.age} · Weight {user.weight_kg}kg · Max HR {user.max_hr}bpm\n"
                f"{escape_md_v2(baseline_str)} · Days: {escape_md_v2(days_str)}"
            )
        else:
            sections.append("*Profile*\nIncomplete — finish setup with /profile")

        # Goals
        goal_lines = ["*Goals*"]
        if user.weekly_volume_goal_km:
            goal_lines.append(f"Weekly {user.weekly_volume_goal_km} km")
        if user.race_date:
            race_line = f"{user.race_distance} race — {user.race_date.isoformat()}"
            if user.race_target_secs:
                race_line += f" (target {_fmt_duration(user.race_target_secs)})"
            goal_lines.append(escape_md_v2(race_line))
        if len(goal_lines) == 1:
            goal_lines.append("None set — use /goal")
        sections.append("\n".join(goal_lines))

        # This week's volume
        if user.weekly_volume_goal_km:
            today = datetime.now(ZoneInfo(user.timezone or "UTC")).date()
            week_start = today - timedelta(days=today.weekday())
            week_start_dt = datetime.combine(week_start, datetime.min.time(), tzinfo=UTC)
            week_runs = (
                await session.scalars(
                    select(Run).where(Run.user_id == user.id, Run.start_date >= week_start_dt)
                )
            ).all()
            done_km = sum(r.distance_m for r in week_runs) / 1000
            goal_km = user.weekly_volume_goal_km
            pct = round(done_km / goal_km * 100) if goal_km else 0
            sections.append(
                f"*This week*\n"
                f"{escape_md_v2(f'{done_km:.1f}')} / {escape_md_v2(f'{goal_km:.1f}')} km "
                f"\\({pct}%\\)"
            )

        # Injury
        if user.current_injury:
            sections.append(f"*Active injury*\n{escape_md_v2(user.current_injury)}")

        # Last run
        last_run = await session.scalar(
            select(Run).where(Run.user_id == user.id).order_by(desc(Run.start_date)).limit(1)
        )
        if last_run:
            tz = ZoneInfo(user.timezone or "UTC")
            local = last_run.start_date.astimezone(tz)
            sections.append(
                "*Last run*\n"
                f"{escape_md_v2(local.strftime('%a %d %b'))} · "
                f"{escape_md_v2(last_run.run_type or 'run')} · "
                f"{escape_md_v2(f'{last_run.distance_m / 1000:.1f}')}km"
            )

        # Next planned session
        if user.next_planned_session_json:
            ns = user.next_planned_session_json
            offset = ns.get("relative_offset_days", 0)
            day_word = "day" if offset == 1 else "days"
            sections.append(
                "*Next session*\n"
                f"{escape_md_v2(ns['scheduled_day_label'])} "
                f"{escape_md_v2(ns['scheduled_date'])} "
                f"\\(in {offset} {day_word}\\)\n"
                f"{escape_md_v2(ns['type'])} · "
                f"{escape_md_v2(f'{ns["distance_km"]}')}km · "
                f"{escape_md_v2(ns['target_pace_label'])} · "
                f"{escape_md_v2(ns['target_hr_zone'])}"
            )

        await update.message.reply_text("\n\n".join(sections), parse_mode=ParseMode.MARKDOWN_V2)


# ----------------------------------------------------------------- registration


def register_handlers(app: Application) -> None:
    """Register every command + conversation handler with the Application."""
    # Conversation handlers must be registered BEFORE the standalone /cancel
    # so they get first dibs on /cancel inside an active conversation.
    app.add_handler(build_profile_handler())
    app.add_handler(build_goal_handler())

    # Static commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    # Phase 9 commands
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("injury", injury_command))

    log.info("registered telegram bot handlers")
