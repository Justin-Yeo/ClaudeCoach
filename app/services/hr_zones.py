"""Heart rate zone computation.

Uses the simple percentage-of-max-HR model from [spec.md §11.17](spec.md):

    Z1: 0%   – 60%  max_hr
    Z2: 60%  – 70%
    Z3: 70%  – 80%
    Z4: 80%  – 90%
    Z5: 90%  – 100%+

`compute_default_zones()` returns the four zone upper bounds; the metrics
module uses these to bucket the HR stream into zone-times.
"""

from __future__ import annotations


def compute_default_zones(max_hr: int) -> tuple[int, int, int, int]:
    """Compute the four zone upper bounds from a user's max HR.

    Returns `(z1_max, z2_max, z3_max, z4_max)` in bpm. Anything above
    `z4_max` is Z5. These are stored on the `users` table as
    `hr_zone1_max` … `hr_zone4_max` and can be overridden per user later
    without recomputing.

    Raises ValueError if max_hr is not strictly positive.
    """
    if max_hr <= 0:
        raise ValueError(f"max_hr must be positive, got {max_hr}")

    return (
        round(max_hr * 0.60),
        round(max_hr * 0.70),
        round(max_hr * 0.80),
        round(max_hr * 0.90),
    )


def estimate_max_hr(age: int) -> int:
    """Rough estimate of max HR from age using the classic `220 − age` formula.

    Used as a fallback when the user hasn't provided a `max_hr` in their
    profile. Not particularly accurate for individuals but fine as a default.
    """
    if age <= 0:
        raise ValueError(f"age must be positive, got {age}")
    return 220 - age
