"""Pace format helpers.

Internal pace representation: **float minutes per km** (e.g. `4.5` = 4:30/km).
Display representation: `m:ss/km` string (e.g. `"4:30/km"`).

Every place that needs to show a pace to a user goes through these helpers so
there's one canonical format throughout the bot, prompts, and history views.
See [spec.md §11.18](spec.md) for the trade-off rationale.
"""

from __future__ import annotations

import re

# Accept "4:30", "4:30/km", or "4:30 /km" with optional surrounding whitespace
_PACE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(?:/km)?\s*$")


def format_pace_min(pace_min_km: float) -> str:
    """Format a float pace as `m:ss/km`.

    Rounds to the nearest second.

        >>> format_pace_min(4.5)
        '4:30/km'
        >>> format_pace_min(4.083)
        '4:05/km'
        >>> format_pace_min(10.0)
        '10:00/km'

    Raises ValueError if pace is not strictly positive.
    """
    if pace_min_km <= 0:
        raise ValueError(f"pace must be positive, got {pace_min_km}")

    total_secs = round(pace_min_km * 60)
    minutes = total_secs // 60
    seconds = total_secs % 60
    return f"{minutes}:{seconds:02d}/km"


def parse_pace_str(pace_str: str) -> float:
    """Parse a `m:ss` or `m:ss/km` string to float minutes/km.

        >>> parse_pace_str("4:30/km")
        4.5
        >>> parse_pace_str("4:30")
        4.5
        >>> parse_pace_str("10:00/km")
        10.0

    Raises ValueError on invalid input.
    """
    match = _PACE_RE.match(pace_str)
    if not match:
        raise ValueError(f"invalid pace string: {pace_str!r}")

    minutes = int(match.group(1))
    seconds = int(match.group(2))
    if seconds >= 60:
        raise ValueError(f"seconds must be < 60: {pace_str!r}")

    return round(minutes + seconds / 60, 3)


def secs_to_pace_min(total_secs: float, distance_m: float) -> float | None:
    """Compute pace in minutes/km from raw time (seconds) and distance (metres).

    Returns None if distance or time is non-positive.

        >>> secs_to_pace_min(1200, 5000)  # 5 km in 20 min = 4:00/km
        4.0
    """
    if distance_m <= 0 or total_secs <= 0:
        return None
    return round((total_secs / 60) / (distance_m / 1000), 3)
