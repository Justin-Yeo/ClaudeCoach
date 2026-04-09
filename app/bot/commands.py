"""Telegram bot static commands and handler registration.

Wires `/start`, `/help`, `/cancel` plus the `/profile` and `/goal` conversation
handlers (defined in `app/bot/conversations.py`) to the `Application`.

Phase 3: `/start` is a stub that just echoes the user's id and args.
Phase 8 replaces it with the full invite-code + OAuth onboarding flow.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from app.bot.conversations import build_goal_handler, build_profile_handler
from app.services.telegram import escape_md_v2

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
    for cmd, desc in HELP_LINES:
        lines.append(f"`{escape_md_v2(cmd)}` — {escape_md_v2(desc)}")
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

    log.info("registered telegram bot handlers")
