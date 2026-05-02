"""Telegram message helpers.

Phase 3: `escape_md_v2()` for safe MarkdownV2 interpolation.
Phase 5: message renderers + send helpers used by the coaching pipeline.

All bot messages use `parse_mode='MarkdownV2'` per [spec.md §11.19](spec.md).
The renderers in this module hand-author the formatting markers (bold headers,
emoji) and pass dynamic content through `escape_md_v2()`.
"""

from __future__ import annotations

from typing import Any

from telegram import Bot

from app.services.pace import format_pace_min

# MarkdownV2 special characters per https://core.telegram.org/bots/api#markdownv2-style
_MD_V2_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")


def escape_md_v2(text: str) -> str:
    """Escape every MarkdownV2 special character in `text` with a backslash.

    Use on ALL dynamic content before interpolating it into a MarkdownV2
    message template. Author-controlled static strings are escaped once at
    write time.

    NOT idempotent: escaping twice doubles the backslashes.
    """
    return "".join("\\" + c if c in _MD_V2_SPECIAL else c for c in text)


# ============================================================================
# Renderers — produce ready-to-send MarkdownV2 strings
# ============================================================================


def render_post_run_review(review: dict[str, str]) -> str:
    """Render the 3-section post-run review for Telegram Message 1.

    `review` is the `post_run_review` field from a `submit_coaching` tool call:
    `{run_summary, went_well, to_watch, digest}`.
    """
    return (
        f"*🏃 Run Summary*\n"
        f"{escape_md_v2(review['run_summary'])}\n\n"
        f"*✅ What Went Well*\n"
        f"{escape_md_v2(review['went_well'])}\n\n"
        f"*👀 What to Watch*\n"
        f"{escape_md_v2(review['to_watch'])}"
    )


def render_next_session(ns: dict[str, Any]) -> str:
    """Render the structured next-session workout for Telegram Message 2.

    `ns` is the `next_session` JSON from a `submit_coaching` tool call —
    matches the shape in [schema.md §4H.1](schema.md).
    """
    type_label = ns["type"].upper()
    when = (
        f"{escape_md_v2(ns['scheduled_day_label'])} "
        f"{escape_md_v2(ns['scheduled_date'])} "
        f"\\(in {ns['relative_offset_days']} day"
        f"{'s' if ns['relative_offset_days'] != 1 else ''}\\)"
    )

    workout = ns["workout"]
    workout_lines = []
    if warmup := workout.get("warmup"):
        workout_lines.append(f"🔥 *Warmup*: {escape_md_v2(warmup)}")
    workout_lines.append(f"💪 *Main*: {escape_md_v2(workout['main'])}")
    if cooldown := workout.get("cooldown"):
        workout_lines.append(f"🧊 *Cooldown*: {escape_md_v2(cooldown)}")

    out = (
        f"*⏭️ Next Session — {escape_md_v2(type_label)}*\n"
        f"{when}\n\n"
        f"📏 {escape_md_v2(f'{ns["distance_km"]}')}km  ·  "
        f"🎯 {escape_md_v2(ns['target_pace_label'])}  ·  "
        f"{escape_md_v2(ns['target_hr_zone'])}\n\n" + "\n".join(workout_lines)
    )

    if notes := ns.get("notes"):
        out += f"\n\n_📝 {escape_md_v2(notes)}_"

    return out


# ============================================================================
# Send helpers
# ============================================================================


async def send_message(bot: Bot, chat_id: int, text: str) -> None:
    """Send a MarkdownV2 message. Caller must have escaped dynamic content."""
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="MarkdownV2")


async def send_post_run_review(bot: Bot, chat_id: int, review: dict[str, str]) -> None:
    """Send Telegram Message 1 — sections 1–3 of the coaching response."""
    await send_message(bot, chat_id, render_post_run_review(review))


async def send_next_session(bot: Bot, chat_id: int, next_session: dict[str, Any]) -> None:
    """Send Telegram Message 2 — the structured workout."""
    await send_message(bot, chat_id, render_next_session(next_session))


# ============================================================================
# Operational DMs (errors, completeness gates, race-day cleanup)
# ============================================================================


async def dm_reconnect_link(bot: Bot, chat_id: int, oauth_url: str | None = None) -> None:
    """Used when Strava has rejected the user's refresh token. See spec.md §3.4."""
    if oauth_url:
        msg = f"⚠️ *Strava connection expired*\n\nTap to reconnect: {escape_md_v2(oauth_url)}"
    else:
        msg = "⚠️ *Strava connection expired*\n\nRun /start to reconnect\\."
    await send_message(bot, chat_id, msg)


async def dm_setup_incomplete(bot: Bot, chat_id: int) -> None:
    """Sent when a run comes in but profile or goal is missing."""
    await send_message(
        bot,
        chat_id,
        "✅ Run logged\\.\n\n"
        "Finish your profile with /profile and set a goal with /goal "
        "to start receiving coaching\\.",
    )


async def dm_race_day_cleanup(bot: Bot, chat_id: int) -> None:
    """Sent when the race goal is auto-cleared after race day passes."""
    await send_message(
        bot,
        chat_id,
        "🏁 Your race goal date has passed — race goal cleared\\. "
        "Set a new one anytime with /goal\\.",
    )


async def dm_coaching_unreachable(bot: Bot, chat_id: int) -> None:
    """Sent when the Claude API call fails. Spec §3.4 error UX."""
    await send_message(
        bot,
        chat_id,
        "⚠️ Coaching engine unreachable — your run is saved, "
        "I'll retry on your next upload\\. Or run /plan to retry now\\.",
    )


# Helper used by renderers to format pace floats consistently.
def fmt_pace(pace_min: float | None) -> str:
    return format_pace_min(pace_min) if pace_min else "n/a"
