"""Claude coaching service — prompt builder + API call.

Two public entry points:

- `build_user_prompt(...)` — fills the `USER_TEMPLATE` with a user profile,
  goals, recent runs, and the current run's metrics. Pure function, no I/O.
- `call_claude_coaching(...)` — async. Builds the prompt, calls Claude Sonnet
  via tool-use, validates the tool call against the schema, returns the parsed
  tool input dict.

The orchestrator in `app/services/coaching.py` (phase 5) is the only caller of
`call_claude_coaching` in production. Phase 2 uses it directly from
`scripts/phase2_demo.py`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from anthropic import AsyncAnthropic
from jsonschema import ValidationError, validate

from app.prompts.v1 import COACHING_TOOL, SYSTEM_PROMPT, USER_TEMPLATE
from app.services.pace import format_pace_min

# --------------------------------------------------------------- formatters


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    hours = secs // 3600
    minutes = (secs % 3600) // 60
    seconds = secs % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _fmt_pace_or_na(pace_min: float | None) -> str:
    return format_pace_min(pace_min) if pace_min else "n/a"


def _fmt_hr_or_na(hr: float | None) -> str:
    return f"{round(hr)}" if hr else "n/a"


def _fmt_baseline(user: dict) -> str:
    parts = []
    if user.get("recent_5k_secs"):
        s = int(user["recent_5k_secs"])
        parts.append(f"5K in {s // 60}:{s % 60:02d}")
    if user.get("recent_10k_secs"):
        s = int(user["recent_10k_secs"])
        parts.append(f"10K in {s // 60}:{s % 60:02d}")
    return ", ".join(parts) if parts else "not provided"


def _fmt_days(days: list[str] | None) -> str:
    return ", ".join(days) if days else "none set"


def _fmt_race_target(secs: int | None) -> str:
    if not secs:
        return " (no time goal)"
    hours = secs // 3600
    mins = (secs % 3600) // 60
    sec = secs % 60
    if hours:
        return f" (target {hours}:{mins:02d}:{sec:02d})"
    return f" (target {mins}:{sec:02d})"


# ---------------------------------------------------------- block builders


def build_goals_block(goals: dict) -> str:
    """Render the Goals section for the prompt."""
    lines = []
    if weekly := goals.get("weekly_volume_goal_km"):
        lines.append(f"- Weekly volume target: {weekly} km")
    if race_date := goals.get("race_date"):
        dist = goals.get("race_distance", "?")
        target = _fmt_race_target(goals.get("race_target_secs"))
        lines.append(f"- Race goal: {dist} on {race_date}{target}")
    return "\n".join(lines) if lines else "- No goals set"


def build_recent_runs_block(recent_runs: list[dict]) -> str:
    """Render the last 4 weeks of runs as a compact bullet list."""
    if not recent_runs:
        return (
            "- No runs in the last 4 weeks. This is a cold start — lean on the "
            "runner's baseline times to anchor pace recommendations."
        )
    lines = []
    for run in recent_runs:
        d = run.get("date", "?")
        rtype = run.get("run_type", "?")
        dist = run.get("distance_km", 0)
        pace = run.get("avg_pace_min_km")
        pace_str = format_pace_min(pace) if pace else "n/a"
        hr = run.get("avg_hr")
        hr_str = f"{round(hr)}bpm" if hr else "—"
        lines.append(f"- {d} · {rtype} · {dist:.1f}km · {pace_str} · {hr_str}")
    return "\n".join(lines)


def build_hr_block(run: dict) -> str:
    """Render the HR section of the current run. Empty string if no HR data."""
    if not run.get("avg_hr"):
        return ""
    lines = ["**Heart rate**:"]
    lines.append(f"- Average: {round(run['avg_hr'])} bpm")
    if run.get("max_hr"):
        lines.append(f"- Max: {round(run['max_hr'])} bpm")
    if zones := run.get("hr_zones_secs"):
        total = sum(zones)
        for i, secs in enumerate(zones):
            pct = round(secs / total * 100) if total else 0
            lines.append(f"- Z{i + 1}: {_fmt_duration(secs)} ({pct}%)")
    if (drift := run.get("cardiac_drift_bpm")) is not None:
        lines.append(f"- Cardiac drift: {drift:+.1f} bpm")
    if (dec := run.get("aerobic_decoupling")) is not None:
        lines.append(f"- Aerobic decoupling: {dec:+.2f}%")
    if (ef := run.get("efficiency_factor")) is not None:
        lines.append(f"- Efficiency factor: {ef}")
    return "\n".join(lines)


def build_cadence_block(run: dict) -> str:
    """Render the cadence section of the current run. Empty string if no cadence data."""
    if not run.get("cadence_avg"):
        return ""
    lines = ["**Cadence**:"]
    lines.append(f"- Average: {run['cadence_avg']} spm")
    if (std := run.get("cadence_std_dev")) is not None:
        lines.append(f"- Std dev: {std} spm")
    if (pct := run.get("cadence_under170_pct")) is not None:
        lines.append(f"- Under 170 spm: {pct}% of run")
    return "\n".join(lines)


def build_pace_splits_compact(splits: list[dict]) -> str:
    """Render pace splits as `1:4:30/km, 2:4:25/km, ...`."""
    if not splits:
        return "n/a"
    return ", ".join(f"km{s['km']} {format_pace_min(s['pace_min'])}" for s in splits)


def build_grade_splits_compact(splits: list[dict]) -> str:
    """Render grade splits as `km1 +0.2%, km2 -0.1%, ...`."""
    if not splits:
        return "n/a"
    return ", ".join(f"km{s['km']} {s['grade']:+.1f}%" for s in splits)


# ---------------------------------------------------------- helper: next available day

_WEEKDAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def compute_next_available_day(today: date, available_days: list[str]) -> date:
    """Find the next day strictly in the future that's listed in `available_days`.

    `available_days` is a list of 3-letter weekday codes (`Mon`, `Tue`, ...).
    Raises ValueError if `available_days` is empty.
    """
    if not available_days:
        raise ValueError("available_days must not be empty")
    allowed = {_WEEKDAY_MAP[d] for d in available_days}
    for offset in range(1, 8):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in allowed:
            return candidate
    raise ValueError("no available day found in the next 7 days")


# ---------------------------------------------------------- main prompt builder


def build_user_prompt(
    *,
    user: dict,
    goals: dict,
    current_run: dict,
    recent_runs: list[dict],
    today_local: date,
    weekly_volume_done_km: float,
    next_available_day: date,
) -> str:
    """Fill `USER_TEMPLATE` with all the data Claude needs to coach.

    See docstring at the top of this module for the expected shapes of each
    argument.
    """
    weekly_goal = goals.get("weekly_volume_goal_km") or 0
    weekly_remaining = max(0.0, weekly_goal - weekly_volume_done_km) if weekly_goal else 0.0
    offset_days = (next_available_day - today_local).days

    z1 = user["hr_zone1_max"]
    z2 = user["hr_zone2_max"]
    z3 = user["hr_zone3_max"]
    z4 = user["hr_zone4_max"]

    context: dict[str, Any] = {
        # --- Top
        "today_local": today_local.strftime("%A, %d %B %Y"),
        "next_available_day_label": next_available_day.strftime("%a"),
        "next_available_day_iso": next_available_day.isoformat(),
        "next_available_day_offset": offset_days,
        # --- Runner profile
        "age": user["age"],
        "weight_kg": user["weight_kg"],
        "max_hr": user["max_hr"],
        "baseline_summary": _fmt_baseline(user),
        "available_days": _fmt_days(user.get("available_days")),
        "current_injury_or_none": user.get("current_injury") or "none",
        # --- HR zones (and `_plus_1` offsets for the "next zone starts at" bounds)
        "hr_zone1_max": z1,
        "hr_zone1_max_plus_1": z1 + 1,
        "hr_zone2_max": z2,
        "hr_zone2_max_plus_1": z2 + 1,
        "hr_zone3_max": z3,
        "hr_zone3_max_plus_1": z3 + 1,
        "hr_zone4_max": z4,
        "hr_zone4_max_plus_1": z4 + 1,
        # --- Goals
        "goals_block": build_goals_block(goals),
        # --- Weekly volume
        "weekly_volume_done_km": f"{weekly_volume_done_km:.1f}",
        "weekly_volume_goal_km": f"{weekly_goal:.1f}" if weekly_goal else "not set",
        "weekly_volume_remaining_km": (f"{weekly_remaining:.1f}" if weekly_goal else "n/a"),
        # --- Recent runs (last 4 weeks)
        "recent_runs_block": build_recent_runs_block(recent_runs),
        # --- Current run — summary
        "run_date": current_run.get("start_date_local", "today"),
        "distance_km": f"{current_run['distance_m'] / 1000:.2f}",
        "duration_label": _fmt_duration(current_run["duration_secs"]),
        "avg_pace_label": _fmt_pace_or_na(current_run.get("avg_pace_min_km")),
        "avg_hr_or_na": _fmt_hr_or_na(current_run.get("avg_hr")),
        "max_hr_or_na": _fmt_hr_or_na(current_run.get("max_hr")),
        "elevation_gain_m": f"{current_run.get('elevation_gain_m', 0):.0f}",
        "grade_avg_pct": f"{current_run.get('grade_avg_pct', 0):+.1f}",
        # --- Current run — pace analysis
        "pace_first_half_label": _fmt_pace_or_na(current_run.get("pace_first_half_min")),
        "pace_second_half_label": _fmt_pace_or_na(current_run.get("pace_second_half_min")),
        "fastest_km_pace_label": _fmt_pace_or_na(current_run.get("fastest_km_pace_min")),
        "slowest_km_pace_label": _fmt_pace_or_na(current_run.get("slowest_km_pace_min")),
        "pace_std_dev_min": f"{current_run.get('pace_std_dev_min', 0):.2f}",
        "gap_label": _fmt_pace_or_na(current_run.get("gap_min_km")),
        "pace_splits_compact": build_pace_splits_compact(current_run.get("pace_splits", [])),
        # --- Conditional blocks
        "hr_block": build_hr_block(current_run),
        "cadence_block": build_cadence_block(current_run),
        # --- Terrain
        "flat_distance_km": f"{current_run.get('flat_distance_m', 0) / 1000:.2f}",
        "uphill_distance_km": f"{current_run.get('uphill_distance_m', 0) / 1000:.2f}",
        "downhill_distance_km": f"{current_run.get('downhill_distance_m', 0) / 1000:.2f}",
        "grade_splits_compact": build_grade_splits_compact(current_run.get("grade_splits", [])),
    }

    return USER_TEMPLATE.format(**context)


# ---------------------------------------------------------- Claude API call


def validate_tool_response(data: dict) -> None:
    """Raise ValueError if `data` doesn't match the `submit_coaching` tool schema."""
    try:
        validate(instance=data, schema=COACHING_TOOL["input_schema"])
    except ValidationError as exc:
        raise ValueError(
            f"Claude response did not match tool schema: {exc.message} "
            f"(path: {list(exc.absolute_path)})"
        ) from exc


async def call_claude_coaching(
    *,
    user: dict,
    goals: dict,
    current_run: dict,
    recent_runs: list[dict],
    today_local: date,
    weekly_volume_done_km: float,
    next_available_day: date,
) -> dict:
    """Build the prompt, call Claude with forced tool use, return the parsed tool input.

    Uses `claude-sonnet-4-6` by default; overridable via `CLAUDE_MODEL` env var.
    Reads `ANTHROPIC_API_KEY` via the SDK's default resolution.
    """
    prompt = build_user_prompt(
        user=user,
        goals=goals,
        current_run=current_run,
        recent_runs=recent_runs,
        today_local=today_local,
        weekly_volume_done_km=weekly_volume_done_km,
        next_available_day=next_available_day,
    )

    # Read all Claude settings from `app.config.Settings` rather than `os.environ`,
    # because Pydantic Settings populates the Settings object but does NOT export to
    # os.environ. Passing api_key explicitly avoids "Could not resolve authentication
    # method" when the FastAPI process starts without load_dotenv being called.
    from app.config import get_settings

    settings = get_settings()
    model = settings.CLAUDE_MODEL
    max_tokens = settings.CLAUDE_MAX_OUTPUT_TOKENS
    max_retries = settings.CLAUDE_MAX_RETRIES

    # max_retries=5 absorbs transient 529/503 outages and 429 rate-limit blips.
    # The SDK applies exponential backoff between retries.
    async with AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        max_retries=max_retries,
    ) as client:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            tools=[COACHING_TOOL],
            tool_choice={"type": "tool", "name": "submit_coaching"},
        )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_coaching":
            data = dict(block.input)
            validate_tool_response(data)
            return data

    block_types = [getattr(b, "type", "?") for b in response.content]
    raise RuntimeError(
        "Claude response did not include a submit_coaching tool call. "
        f"stop_reason={response.stop_reason!r} blocks={block_types}"
    )
