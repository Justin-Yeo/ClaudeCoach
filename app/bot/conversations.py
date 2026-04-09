"""ConversationHandler definitions for `/profile` and `/goal`.

Phase 3 — these flows store data in `context.user_data` only (in-memory,
per-Telegram-user). Phase 4 swaps the in-memory storage for SQLAlchemy writes
to the `users` table.

Each flow walks the user through a sequence of fields one at a time, validates
the input, and re-prompts up to 3 times on invalid input before bailing out.
`/cancel` exits at any point with no partial state saved.

Validators are inline at the top of this module — if they grow, they can be
split into `app/bot/validators.py`.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from enum import IntEnum

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.services.hr_zones import compute_default_zones, estimate_max_hr
from app.services.telegram import escape_md_v2

log = logging.getLogger(__name__)

MAX_RETRIES = 3


# =============================================================================
# Validators
# =============================================================================


def parse_age(text: str) -> int:
    text = text.strip()
    try:
        age = int(text)
    except ValueError as exc:
        raise ValueError("Age must be a whole number, e.g. 28.") from exc
    if age < 10 or age > 100:
        raise ValueError("Age must be between 10 and 100.")
    return age


def parse_weight(text: str) -> float:
    text = text.strip().lower().rstrip("kg").strip()
    try:
        w = float(text)
    except ValueError as exc:
        raise ValueError("Weight must be a number in kg, e.g. 68.5.") from exc
    if w < 30 or w > 200:
        raise ValueError("Weight must be between 30 and 200 kg.")
    return w


def parse_max_hr(text: str) -> int | None:
    text = text.strip().lower()
    if text in ("skip", "-", "auto"):
        return None
    try:
        hr = int(text)
    except ValueError as exc:
        raise ValueError(
            "Max HR must be a whole number in bpm, e.g. 185. Type `skip` to use 220 - age."
        ) from exc
    if hr < 100 or hr > 220:
        raise ValueError("Max HR must be between 100 and 220 bpm.")
    return hr


def parse_pace_time(text: str) -> int | None:
    """Parse a `mm:ss` time string to seconds. Accept `skip` -> None."""
    text = text.strip().lower()
    if text in ("skip", "none", "-"):
        return None
    if ":" not in text:
        raise ValueError("Time format is `mm:ss`, e.g. `20:30`. Type `skip` to skip.")
    try:
        m_str, s_str = text.split(":", 1)
        m = int(m_str)
        s = int(s_str)
    except ValueError as exc:
        raise ValueError("Time format is `mm:ss`, e.g. `20:30`. Type `skip` to skip.") from exc
    if s >= 60 or m < 0 or s < 0:
        raise ValueError("Invalid time. Try again.")
    return m * 60 + s


_VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_DAY_LABELS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}


def parse_days(text: str) -> list[str]:
    parts = [p.strip().lower()[:3] for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("List at least one day, e.g. `Mon, Wed, Fri, Sun`.")
    days: list[str] = []
    for p in parts:
        if p not in _VALID_DAYS:
            raise ValueError(
                f"`{p}` isn't a day. Use abbreviations like Mon, Tue, Wed, Thu, Fri, Sat, Sun."
            )
        label = _DAY_LABELS[p]
        if label not in days:
            days.append(label)
    return days


def parse_injury(text: str) -> str | None:
    text = text.strip()
    if text.lower() in ("none", "no", "-", "skip", ""):
        return None
    if len(text) > 200:
        raise ValueError("Injury note must be 200 characters or less.")
    return text


def parse_weekly_km(text: str) -> float | None:
    text = text.strip().lower().rstrip("km").strip()
    if text in ("skip", "none", "-", ""):
        return None
    try:
        v = float(text)
    except ValueError as exc:
        raise ValueError(
            "Weekly volume must be a number in km, e.g. 40. Type `skip` to leave unset."
        ) from exc
    if v <= 0 or v > 250:
        raise ValueError("Weekly volume must be between 1 and 250 km.")
    return v


def parse_race_date(text: str) -> _date | None:
    text = text.strip().lower()
    if text in ("skip", "none", "-", ""):
        return None
    try:
        d = _date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "Date format is YYYY-MM-DD, e.g. 2026-06-15. Type `skip` to skip."
        ) from exc
    if d <= _date.today():
        raise ValueError("Race date must be strictly in the future.")
    return d


_VALID_RACE_DISTANCES = {
    "5k": "5K",
    "10k": "10K",
    "half": "Half",
    "marathon": "Marathon",
    "other": "other",
}


def parse_race_distance(text: str) -> str:
    t = text.strip().lower()
    if t not in _VALID_RACE_DISTANCES:
        raise ValueError("Distance must be one of: 5K, 10K, Half, Marathon, other.")
    return _VALID_RACE_DISTANCES[t]


def parse_race_target_secs(text: str) -> int | None:
    """Parse `mm:ss` or `h:mm:ss` to seconds. Accept `skip` -> None."""
    text = text.strip().lower()
    if text in ("skip", "none", "-", ""):
        return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError as exc:
        raise ValueError(
            "Time format is `mm:ss` or `h:mm:ss`. Type `skip` for no time goal."
        ) from exc
    if len(nums) == 2:
        m, s = nums
        if s >= 60 or m < 0 or s < 0:
            raise ValueError("Invalid time.")
        return m * 60 + s
    if len(nums) == 3:
        h, m, s = nums
        if h < 0 or m < 0 or s < 0 or m >= 60 or s >= 60:
            raise ValueError("Invalid time.")
        return h * 3600 + m * 60 + s
    raise ValueError("Time format is `mm:ss` or `h:mm:ss`.")


# =============================================================================
# Send helpers
# =============================================================================


async def _send(update: Update, text: str) -> None:
    """Send a plain-text message escaped for MarkdownV2."""
    await update.message.reply_text(
        escape_md_v2(text),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _retry(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    retries_key: str,
    error_msg: str,
    current_state: int,
) -> int:
    """Increment the retry counter; if exceeded, end conversation. Otherwise re-prompt."""
    retries = context.user_data.get(retries_key, 0) + 1
    if retries >= MAX_RETRIES:
        await _send(
            update,
            f"{error_msg}\n\nToo many invalid inputs ({retries}/{MAX_RETRIES}). "
            "Cancelled — run the command again to start over.",
        )
        context.user_data.pop(retries_key, None)
        return ConversationHandler.END
    context.user_data[retries_key] = retries
    await _send(update, f"{error_msg}\n\nTry again ({retries}/{MAX_RETRIES}):")
    return current_state


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fallback /cancel inside a ConversationHandler."""
    # Clean up any draft state for this conversation
    for key in list(context.user_data.keys()):
        if key.endswith("_draft") or key.endswith("_retries"):
            context.user_data.pop(key, None)
    await _send(update, "Cancelled. No changes saved.")
    return ConversationHandler.END


# =============================================================================
# /profile flow
# =============================================================================


class ProfileState(IntEnum):
    AGE = 1
    WEIGHT = 2
    MAX_HR = 3
    RECENT_5K = 4
    RECENT_10K = 5
    DAYS = 6
    INJURY = 7


async def _profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /profile."""
    context.user_data["profile_draft"] = {}
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        "Let's set up your runner profile.\n\nWhat's your age? (whole number)",
    )
    return ProfileState.AGE


async def _profile_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = parse_age(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.AGE)
    context.user_data["profile_draft"]["age"] = age
    context.user_data["profile_retries"] = 0
    await _send(update, "Got it. Weight in kg? (e.g. 68.5)")
    return ProfileState.WEIGHT


async def _profile_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        w = parse_weight(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.WEIGHT)
    context.user_data["profile_draft"]["weight_kg"] = w
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        "Max heart rate in bpm? (e.g. 185)\n\nType `skip` and I'll estimate it as 220 - age.",
    )
    return ProfileState.MAX_HR


async def _profile_max_hr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        hr = parse_max_hr(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.MAX_HR)
    if hr is None:
        # Estimate from age
        hr = estimate_max_hr(context.user_data["profile_draft"]["age"])
    context.user_data["profile_draft"]["max_hr"] = hr
    z1, z2, z3, z4 = compute_default_zones(hr)
    context.user_data["profile_draft"]["hr_zone1_max"] = z1
    context.user_data["profile_draft"]["hr_zone2_max"] = z2
    context.user_data["profile_draft"]["hr_zone3_max"] = z3
    context.user_data["profile_draft"]["hr_zone4_max"] = z4
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        f"Max HR set to {hr}. Zones computed automatically.\n\n"
        "Recent 5K time? (mm:ss, e.g. `22:30`)\n\nType `skip` if you don't have one.",
    )
    return ProfileState.RECENT_5K


async def _profile_5k(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        secs = parse_pace_time(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.RECENT_5K)
    context.user_data["profile_draft"]["recent_5k_secs"] = secs
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        "Recent 10K time? (mm:ss, e.g. `46:00`)\n\nType `skip` if you don't have one.",
    )
    return ProfileState.RECENT_10K


async def _profile_10k(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        secs = parse_pace_time(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.RECENT_10K)
    draft = context.user_data["profile_draft"]
    draft["recent_10k_secs"] = secs
    # At least one baseline must be set
    if draft.get("recent_5k_secs") is None and secs is None:
        return await _retry(
            update,
            context,
            "profile_retries",
            "You need at least one of 5K or 10K time so I can anchor pace recommendations.",
            ProfileState.RECENT_10K,
        )
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        "Which days of the week can you run?\n\nComma-separated, e.g. `Mon, Wed, Fri, Sun`",
    )
    return ProfileState.DAYS


async def _profile_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = parse_days(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.DAYS)
    context.user_data["profile_draft"]["available_days"] = days
    context.user_data["profile_retries"] = 0
    await _send(
        update,
        "Any current injuries or niggles I should factor in?\n\n"
        "Free text (max 200 chars), or type `none`.",
    )
    return ProfileState.INJURY


async def _profile_injury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        injury = parse_injury(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "profile_retries", str(exc), ProfileState.INJURY)
    context.user_data["profile_draft"]["current_injury"] = injury
    # Commit
    context.user_data["profile"] = context.user_data.pop("profile_draft")
    context.user_data.pop("profile_retries", None)

    p = context.user_data["profile"]
    summary = (
        "Profile saved.\n\n"
        f"- Age: {p['age']}\n"
        f"- Weight: {p['weight_kg']} kg\n"
        f"- Max HR: {p['max_hr']} bpm\n"
        f"- 5K: {_fmt_pace_time(p.get('recent_5k_secs'))}\n"
        f"- 10K: {_fmt_pace_time(p.get('recent_10k_secs'))}\n"
        f"- Days: {', '.join(p['available_days'])}\n"
        f"- Injury: {p.get('current_injury') or 'none'}\n\n"
        "Set a goal next with /goal."
    )
    await _send(update, summary)
    return ConversationHandler.END


def _fmt_pace_time(secs: int | None) -> str:
    if secs is None:
        return "not set"
    return f"{secs // 60}:{secs % 60:02d}"


def _text(callback) -> MessageHandler:
    """Shorthand for a non-command text MessageHandler."""
    return MessageHandler(filters.TEXT & ~filters.COMMAND, callback)


def build_profile_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("profile", _profile_start)],
        states={
            ProfileState.AGE: [_text(_profile_age)],
            ProfileState.WEIGHT: [_text(_profile_weight)],
            ProfileState.MAX_HR: [_text(_profile_max_hr)],
            ProfileState.RECENT_5K: [_text(_profile_5k)],
            ProfileState.RECENT_10K: [_text(_profile_10k)],
            ProfileState.DAYS: [_text(_profile_days)],
            ProfileState.INJURY: [_text(_profile_injury)],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="profile",
        persistent=False,
    )


# =============================================================================
# /goal flow
# =============================================================================


class GoalState(IntEnum):
    WEEKLY = 1
    RACE_DATE = 2
    RACE_DISTANCE = 3
    RACE_TARGET = 4


async def _goal_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for /goal."""
    context.user_data["goal_draft"] = {}
    context.user_data["goal_retries"] = 0
    await _send(
        update,
        "Let's set your training goals. You need at least one of:\n"
        "- Weekly volume target\n"
        "- Race goal (date + distance + optional time)\n\n"
        "Weekly volume target in km? (e.g. 40)\n\n"
        "Type `skip` to skip this and use a race goal instead.",
    )
    return GoalState.WEEKLY


async def _goal_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        km = parse_weekly_km(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "goal_retries", str(exc), GoalState.WEEKLY)
    context.user_data["goal_draft"]["weekly_volume_goal_km"] = km
    context.user_data["goal_retries"] = 0
    await _send(
        update,
        "Race goal date? (YYYY-MM-DD, e.g. `2026-06-15`)\n\n"
        "Type `skip` if you have no race goal right now.",
    )
    return GoalState.RACE_DATE


async def _goal_race_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        d = parse_race_date(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "goal_retries", str(exc), GoalState.RACE_DATE)
    draft = context.user_data["goal_draft"]
    if d is None:
        # No race goal — finalise
        return await _finalise_goal(update, context)
    draft["race_date"] = d.isoformat()
    context.user_data["goal_retries"] = 0
    await _send(
        update,
        "Race distance? One of: `5K`, `10K`, `Half`, `Marathon`, `other`",
    )
    return GoalState.RACE_DISTANCE


async def _goal_race_distance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dist = parse_race_distance(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "goal_retries", str(exc), GoalState.RACE_DISTANCE)
    context.user_data["goal_draft"]["race_distance"] = dist
    context.user_data["goal_retries"] = 0
    await _send(
        update,
        "Target finish time? (mm:ss for short races, h:mm:ss for marathon)\n\n"
        "Type `skip` if you have no time goal — just want to complete it.",
    )
    return GoalState.RACE_TARGET


async def _goal_race_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        secs = parse_race_target_secs(update.message.text)
    except ValueError as exc:
        return await _retry(update, context, "goal_retries", str(exc), GoalState.RACE_TARGET)
    context.user_data["goal_draft"]["race_target_secs"] = secs
    return await _finalise_goal(update, context)


async def _finalise_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the constraint (≥1 goal set) and commit."""
    draft = context.user_data.get("goal_draft", {})
    has_weekly = draft.get("weekly_volume_goal_km") is not None
    has_race = draft.get("race_date") is not None

    if not has_weekly and not has_race:
        await _send(
            update,
            "You need at least one goal — weekly volume OR race goal. Run /goal again to set one.",
        )
        context.user_data.pop("goal_draft", None)
        context.user_data.pop("goal_retries", None)
        return ConversationHandler.END

    context.user_data["goal"] = context.user_data.pop("goal_draft")
    context.user_data.pop("goal_retries", None)

    g = context.user_data["goal"]
    lines = ["Goals saved.\n"]
    if g.get("weekly_volume_goal_km"):
        lines.append(f"- Weekly volume: {g['weekly_volume_goal_km']} km")
    if g.get("race_date"):
        race_line = f"- Race: {g.get('race_distance')} on {g['race_date']}"
        if g.get("race_target_secs"):
            secs = g["race_target_secs"]
            h = secs // 3600
            m = (secs % 3600) // 60
            s = secs % 60
            time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            race_line += f" (target {time_str})"
        lines.append(race_line)
    await _send(update, "\n".join(lines))
    return ConversationHandler.END


def build_goal_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("goal", _goal_start)],
        states={
            GoalState.WEEKLY: [_text(_goal_weekly)],
            GoalState.RACE_DATE: [_text(_goal_race_date)],
            GoalState.RACE_DISTANCE: [_text(_goal_race_distance)],
            GoalState.RACE_TARGET: [_text(_goal_race_target)],
        },
        fallbacks=[CommandHandler("cancel", _cancel)],
        name="goal",
        persistent=False,
    )
