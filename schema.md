# ClaudeCoach — Database Schema

**Final Schema · Apple Watch + Garmin · No power meter · April 2026**

Covers all four tables: `runs` (§4), `users` (§7), `invite_codes` (§8), and `oauth_states` (§9). Aligned with [spec.md](spec.md).

---

## 1. Scope & Design Decisions

All activities are runs. Coaching is derived purely from performance stats. The following fields were considered and deliberately excluded:

- `gear_id` — no shoe tracking
- `name` / `description` — stats only, no subjective notes
- `calories` — not a performance metric
- `suffer_score` — Strava's opaque estimate, not a real stat
- `perceived_exertion` — subjective, removed with description
- `elevation_profile_json` — visualisation only, no coaching value
- Power group (all fields) — no power meter in use

**Raw streams are kept.** Fetched at ingest and used immediately to compute all derived metrics, then stored as an archive. Never queried again in normal operation — but allow reprocessing with improved logic later without re-hitting the Strava API.

**Strava OAuth tokens are stored in plain text.** Personal-scale, single-tenant Supabase, no PCI/PHI data — encryption at rest is overkill. See [spec.md §11.3](spec.md). Revisit if the user base grows beyond a small invited circle.

---

## 2. Device Data Availability

Apple Watch and Garmin with optical HRM only. No chest strap, no power meter.

| Metric | Apple Watch | Garmin | Notes |
|---|---|---|---|
| Distance & Pace | ✅ | ✅ | Always present |
| Elevation & Grade | ✅ | ✅ | Barometric on Apple Watch Series 6+ |
| Pace splits (per km) | ✅ | ✅ | Computed from velocity_smooth stream |
| Heart rate (summary) | ✅ | ✅ | Optical only. Less accurate than chest strap. |
| HR streams | ⚠️ | ✅ | Apple Watch depends on sync method |
| HR zone breakdown | ⚠️ | ✅ | NULL if stream missing |
| Aerobic decoupling | ⚠️ | ✅ | Needs HR stream |
| Cadence | ⚠️ | ✅ | Apple Watch unreliable. Always nullable. |

### ⚠️ Apple Watch Stream Reliability

Stream availability depends on how the activity syncs to Strava:

- **Strava's Apple Watch app** → HR streams usually present, cadence often absent
- **Health app auto-sync** → summary data only, streams frequently missing entirely
- **HealthFit / RunGap export** → most complete streams, recommended for full coaching data

> Ingest code always checks stream presence before computing derived metrics. Claude's prompt builder skips any section where data is NULL.

---

## 3. Field Priority Tiers

| Tier | Nullable | Sent to Claude | Condition |
|---|---|---|---|
| **P1 — Core** | NOT NULL | Always | Available on every device, every sync method |
| **P2 — HR** | NULL | Only if HR stream present | Garmin reliable. Apple Watch depends on sync method. |
| **P3 — Cadence** | NULL | Only if cadence stream present | Garmin reliable. Apple Watch unreliable. |
| **Archive** | NULL | Never | Raw streams. Written once at ingest, never queried again. |

---

## 4. Full runs Table Schema

### A · Identity & Metadata `[P1 — Core]`

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `id` | INTEGER | NOT NULL | — | Primary key |
| `user_id` | INTEGER | NOT NULL | — | Foreign key → users table |
| `strava_activity_id` | TEXT | NOT NULL | `activity.id` | Prevents duplicate imports on re-sync |
| `start_date` | DATETIME | NOT NULL | `activity.start_date_local` | Local start time — used to schedule next session |
| `timezone` | TEXT | NOT NULL | `activity.timezone` | User timezone — critical for scheduling |
| `workout_type` | INTEGER | NOT NULL | `activity.workout_type` | Strava's coarse enum: 0=default run, 1=race, 2=long run, 3=workout. Raw input only. |
| `run_type` | TEXT | NULL | computed by Claude | Fixed taxonomy from [spec.md §4](spec.md): `easy`\|`long`\|`tempo`\|`intervals`\|`recovery`\|`race`. NULL until Claude classifies the run during ingest. Used by `/history` and as input for next-session reasoning. |

---

### B · Summary Stats `[P1 — Core]`

From `GET /activities/{id}`. Always present regardless of device or sync method.

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `distance_m` | FLOAT | NOT NULL | `activity.distance` | Total distance in metres |
| `duration_secs` | INTEGER | NOT NULL | `activity.moving_time` | Moving time — excludes pauses |
| `elapsed_secs` | INTEGER | NOT NULL | `activity.elapsed_time` | Wall-clock time. Gap vs duration = total rest stops. |
| `elevation_gain_m` | FLOAT | NOT NULL | `activity.total_elevation_gain` | Total ascent — essential for effort normalisation |
| `elevation_high_m` | FLOAT | NOT NULL | `activity.elev_high` | Highest point reached |
| `elevation_low_m` | FLOAT | NOT NULL | `activity.elev_low` | Lowest point reached |
| `avg_pace_min_km` | FLOAT | NOT NULL | computed: distance ÷ time | Avg pace in **minutes/km** (e.g. `4.5` = 4:30/km) — primary effort metric. See [spec.md §11.18](spec.md). |
| `avg_speed_ms` | FLOAT | NOT NULL | `activity.average_speed` | Raw speed m/s from Strava |
| `max_speed_ms` | FLOAT | NOT NULL | `activity.max_speed` | Peak speed — sprint or downhill capacity |

---

### C · Pace Analysis `[P1 — Core]`

Computed from the `velocity_smooth` stream. Reliable on both Apple Watch and Garmin.

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `pace_first_half_min` | FLOAT | NOT NULL | velocity_smooth stream | Avg pace for first half of run, **minutes/km**. |
| `pace_second_half_min` | FLOAT | NOT NULL | velocity_smooth stream | Avg pace for second half, minutes/km. Positive split = faded. |
| `pace_std_dev_min` | FLOAT | NOT NULL | velocity_smooth stream | Pace consistency in minutes/km. High = erratic. Low = controlled. |
| `fastest_km_pace_min` | FLOAT | NOT NULL | velocity_smooth stream | Best km split, minutes/km — sprint/surge capacity. |
| `slowest_km_pace_min` | FLOAT | NOT NULL | velocity_smooth stream | Worst km split, minutes/km — where fatigue peaked. |
| `pace_splits_json` | JSON | NOT NULL | velocity_smooth stream | `[{km:1, pace_min:5.2}, ...]` — per-km breakdown, pace in minutes/km. |
| `gap_min_km` | FLOAT | NULL | computed: pace + grade | Grade-Adjusted Pace in minutes/km — flat-equivalent effort. |

> **Pace representation**: All `*_min` columns and `pace_splits_json[].pace_min` use **float minutes/km** (e.g. `4.5` = 4:30/km). Display formatting via `app/services/pace.py`. See [spec.md §11.18](spec.md) for the rationale and helper signatures.

---

### D · Elevation & Grade `[P1 — Core]`

From `altitude` and `grade_smooth` streams. Reliable on both devices.

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `grade_avg_pct` | FLOAT | NOT NULL | altitude stream | Average gradient across the full run |
| `flat_distance_m` | FLOAT | NOT NULL | grade_smooth stream | Distance on flat sections (<1% grade) |
| `uphill_distance_m` | FLOAT | NOT NULL | grade_smooth stream | Distance on uphill sections (>2% grade) |
| `downhill_distance_m` | FLOAT | NOT NULL | grade_smooth stream | Distance on downhill sections (<-2% grade) |
| `grade_splits_json` | JSON | NOT NULL | grade_smooth stream | `[{km:1, grade:2.3}, ...]` — hill profile per km |

---

### E · Heart Rate `[P2 — nullable]`

All NULL if HR stream is absent. Prompt builder checks `avg_hr` first — if NULL, entire section is skipped.

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `avg_hr` | INTEGER | NULL | `activity.average_heartrate` | Avg HR — also used as stream availability check |
| `max_hr` | INTEGER | NULL | `activity.max_heartrate` | Peak HR — cardiovascular ceiling for the session |
| `hr_zone1_secs` | INTEGER | NULL | heartrate stream | Seconds in Zone 1 — very easy, recovery |
| `hr_zone2_secs` | INTEGER | NULL | heartrate stream | Seconds in Zone 2 — aerobic base |
| `hr_zone3_secs` | INTEGER | NULL | heartrate stream | Seconds in Zone 3 — moderate aerobic |
| `hr_zone4_secs` | INTEGER | NULL | heartrate stream | Seconds in Zone 4 — lactate threshold |
| `hr_zone5_secs` | INTEGER | NULL | heartrate stream | Seconds in Zone 5 — VO2max / anaerobic |
| `cardiac_drift_bpm` | FLOAT | NULL | heartrate stream | HR rise first→last 10min. >6 bpm = fatigue signal. |
| `aerobic_decoupling` | FLOAT | NULL | hr + velocity streams | % HR drift vs pace. <5% = good. >10% = fatigue. |
| `efficiency_factor` | FLOAT | NULL | hr + velocity streams | Pace ÷ avg HR — tracks aerobic fitness over time |
| `hr_vs_pace_json` | JSON | NULL | hr + velocity streams | `[{km:1, hr:145, pace_min:5.33}, ...]` — the HR/distance chart, pace in minutes/km. |
| `hr_zone_source` | TEXT | NULL | activity or computed | `'strava_custom'` or `'computed_from_max_hr'` |

---

### F · Cadence `[P3 — nullable]`

Reliable on Garmin. Unreliable on Apple Watch. Silently skipped when stream is absent.

| Column | Type | Nullable | Strava Source | Purpose |
|---|---|---|---|---|
| `cadence_avg` | INTEGER | NULL | cadence stream (×2) | Avg steps/min. Strava reports one foot — ×2 for SPM. |
| `cadence_std_dev` | FLOAT | NULL | cadence stream | Consistency. High = terrain response or fatigue. |
| `cadence_under170_pct` | FLOAT | NULL | cadence stream | % of run below 170 spm — overstriding flag |
| `cadence_splits_json` | JSON | NULL | cadence stream | `[{km:1, cadence:172}, ...]` — per-km cadence |

---

### G · Raw Stream Archive `[Archive — never sent to Claude]`

Written once at ingest. Never queried again. Kept for future reprocessing without re-fetching from Strava API.

| Column | Type | Nullable | Strava Source | Contents |
|---|---|---|---|---|
| `stream_time_json` | JSON | NULL | streams: time | Elapsed seconds `[0, 1, 2, ...]` |
| `stream_distance_json` | JSON | NULL | streams: distance | Cumulative distance in metres |
| `stream_hr_json` | JSON | NULL | streams: heartrate | HR per second |
| `stream_velocity_json` | JSON | NULL | streams: velocity_smooth | Smoothed speed m/s |
| `stream_altitude_json` | JSON | NULL | streams: altitude | Altitude in metres |
| `stream_cadence_json` | JSON | NULL | streams: cadence | One-foot cadence per second |
| `stream_grade_json` | JSON | NULL | streams: grade_smooth | Gradient % per data point |
| `stream_resolution` | TEXT | NULL | streams response | `'low'`, `'medium'`, or `'high'` |

---

### H · Claude Output `[P1 — Core]`

Stored after every run. Becomes the long-term memory layer for coaching continuity.

| Column | Type | Nullable | Source | Purpose |
|---|---|---|---|---|
| `claude_post_run_review` | TEXT | NULL | rendered from Claude | Sections 1–3 of [spec.md §3.4](spec.md): Run Summary, What Went Well, What to Watch — joined into a single rendered text block ready to send as Telegram **Message 1**. |
| `claude_digest` | TEXT | NULL | parsed from Claude | One-line digest (≤140 chars) extracted from the `submit_coaching` tool call's `post_run_review.digest` field. Used by `/history` to render the per-run one-liner. |
| `claude_next_session` | JSON | NULL | parsed from Claude | Section 4: structured workout — see canonical shape in §4H.1 below. Sent as Telegram **Message 2**. Immutable per-run history (also mirrored to `users.next_planned_session_json`). |
| `claude_load_rating` | TEXT | NULL | parsed from Claude | `'easy'` \| `'moderate'` \| `'hard'` \| `'very_hard'` |
| `claude_flags` | JSON | NULL | parsed from Claude | e.g. `['high cardiac drift', 'pace fade km6+']` |
| `prompt_version` | TEXT | NULL | backend constant | Short identifier of the prompt template used for this call (e.g. `v1`, `v2.coaching-tightening`). Lets you correlate response quality with prompt iterations later. See [spec.md §11.6](spec.md). |
| `processed_at` | DATETIME | NULL | — | When row was written — for debugging webhook delays |

#### §4H.1 · `claude_next_session` JSON shape

Same shape used by `runs.claude_next_session` and `users.next_planned_session_json` (see §7). Fields map directly to the "Next Session" output defined in [spec.md §3.4](spec.md).

```json
{
  "type": "intervals",
  "scheduled_date": "2026-04-12",
  "scheduled_day_label": "Sun",
  "relative_offset_days": 3,
  "distance_km": 10.0,
  "target_pace_min_km": 4.5,
  "target_pace_label": "4:30/km",
  "target_hr_zone": "Z4",
  "workout": {
    "warmup":   "2km easy @ 6:00/km",
    "main":     "6×800m @ 4:00/km w/ 90s jog rest",
    "cooldown": "2km easy @ 6:00/km"
  },
  "notes": "Ease back if achilles still feels off — swap to easy 8K instead."
}
```

- `scheduled_date` is the absolute date; `relative_offset_days` is "3 days from now". Both are stored so the Telegram message can render the [spec.md:130](spec.md#L130) format `3 days from now (Sun, 12 April 2026)` without recomputing.
- `scheduled_date` MUST fall on a day listed in `users.available_days_json`.
- `workout.warmup` and `workout.cooldown` may be empty strings for `easy`/`recovery` runs that have no structured breakdown.
- `notes` is freeform — typically used to acknowledge an injury or recent fatigue signal.

---

## 5. Ingest Flow

```
=== SYNCHRONOUS (FastAPI route, must return HTTP 200 in <2s) ===

on webhook event (athlete_id, activity_id, aspect_type):

  S1. validate aspect_type == 'create' (skip 'update'/'delete')
  S2. look up user by athlete_id in DB
      → if not found: return 200 and stop
  S3. dispatch BackgroundTask(ingest_run, user, activity_id)
  S4. return HTTP 200

=== ASYNCHRONOUS (BackgroundTask) ===

ingest_run(user, activity_id):

  1. GET /activities/{activity_id}
     → on 401: refresh strava token via refresh_token
     → on refresh failure: DM user reconnect link, mark
        strava_token_expires_at = NULL (sentinel for disconnected),
        stop
     → extract P1 summary fields
     → check activity type == 'Run' EXACTLY
     → skip if 'TrailRun', 'VirtualRun', 'Walk', 'Hike', 'Ride', etc.
     → no DB write, no Claude call, no Telegram message on skip
     → see [spec.md §3.4](spec.md)

  2. GET /activities/{activity_id}/streams
        ?keys=time,distance,heartrate,velocity_smooth,
              altitude,cadence,grade_smooth

  3. compute derived metrics:
       always:     pace splits, elevation splits, grade splits, GAP
       if hr:      hr zones, cardiac drift, aerobic decoupling,
                   efficiency factor, hr_vs_pace_json
       if cadence: cadence splits, std dev, under170 pct

  4. BEGIN TRANSACTION:
       store raw streams → stream_*_json columns
       store new run row + derived metrics
       set processed_at = NOW()
     COMMIT
     → on IntegrityError (duplicate strava_activity_id): rollback,
        log, return 200 (idempotency)

  5. completeness gate:
       if profile incomplete OR no goal set:
         DM user "Run logged — finish setup with /profile and /goal"
         stop
       (run row stays, just no coaching this time)

  6. fetch last 4 weeks of run history (per spec §3.4)

  7. build Claude prompt (adaptive — skips NULL sections, includes
     today_local from users.timezone)

  8. POST to Claude API (claude-sonnet-4-6) via tool-use → parse
     structured response
     → BEGIN TRANSACTION:
         store claude_post_run_review, claude_next_session,
              claude_load_rating, claude_flags, prompt_version
         mirror claude_next_session to users.next_planned_session_json
              (source='post_run', run_id=this run's id)
       COMMIT

  9. if race_date < today (in users.timezone):
       NULL all race_* columns
       prepend congratulations note to msg 1

  10. send TWO Telegram messages to user's chat_id:
       msg 1: claude_post_run_review (Run Summary + Went Well + To Watch)
       msg 2: rendered claude_next_session (structured workout)

  11. on Claude/Telegram failure: log, DM "coaching engine
      unreachable — try /plan", do not retry inline
```

---

## 6. Adaptive Prompt Builder

Claude only receives sections where real data exists. NULL fields are never passed.

```python
def build_prompt(run, user, recent_runs):
    sections = []

    # Always included — P1 data guaranteed
    sections.append(build_header(user))
    sections.append(build_summary(run))        # distance, pace, splits
    sections.append(build_elevation(run))      # gain, grade splits, GAP

    # Only if HR stream was present
    if run.avg_hr is not None:
        sections.append(build_hr(run))         # zones, drift, decoupling
        sections.append(build_hr_vs_pace(run)) # per-km HR + pace table

    # Only if cadence stream was present
    if run.cadence_avg is not None:
        sections.append(build_cadence(run))

    # Always included — longitudinal context
    sections.append(build_recent_history(recent_runs))   # last 4 weeks (per spec §3.4)
    sections.append(build_last_recommendation(recent_runs))

    sections.append(COACHING_INSTRUCTION)  # fixed instruction block

    return "\n\n".join(sections)
```

---

## 7. users Table

Holds identity, Strava OAuth state, profile, HR zones, goals, and the canonical "what's next" pointer for each user. Aligned with [spec.md §3.1, §3.2, §3.3, §3.7, §3.8](spec.md).

### A · Identity & Auth `[P1 — Core]`

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `id` | INTEGER | NOT NULL | Primary key. Referenced by `runs.user_id` and `invite_codes.created_by` / `used_by`. |
| `telegram_user_id` | INTEGER | NOT NULL | UNIQUE. Identifies the sender of every command. |
| `telegram_chat_id` | INTEGER | NOT NULL | Where coaching messages are delivered. |
| `athlete_id` | INTEGER | NOT NULL | UNIQUE. Strava athlete ID — the webhook lookup key. |
| `strava_access_token` | TEXT | NOT NULL | Encrypted at rest. Refreshed when expired. |
| `strava_refresh_token` | TEXT | NOT NULL | Used to obtain a new access token. |
| `strava_token_expires_at` | DATETIME | NOT NULL | When the access token needs refreshing. |
| `is_admin` | BOOLEAN | NOT NULL | Default `false`. Admins can use `/invite`. Auto-set to `true` for the user matching `BOOTSTRAP_ADMIN_TELEGRAM_USER_ID` env var (see [spec.md §3.1, §11.2](spec.md)). |
| `timezone` | TEXT | NOT NULL | IANA timezone name (e.g. `Asia/Singapore`). Used to compute "today" for the Claude prompt and to schedule next sessions on the correct local day. Defaults from Strava profile timezone if available, otherwise prompted at onboarding. |
| `created_at` | DATETIME | NOT NULL | Onboarding timestamp. |
| `updated_at` | DATETIME | NOT NULL | Last profile/goal update. |

---

### B · Profile `[P1 — Core]`

Set via `/profile` (see [spec.md §3.2](spec.md)). All fields except `current_injury` must be populated before coaching is generated.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `age` | INTEGER | NULL | Required. Used for HR zone defaults if `max_hr` not set. |
| `weight_kg` | FLOAT | NULL | Required. Effort and load estimation. |
| `recent_5k_secs` | INTEGER | NULL | Best-effort recent 5K time in seconds. **At least one of `recent_5k_secs` / `recent_10k_secs` is required** — anchors target paces from day one (no cold start). |
| `recent_10k_secs` | INTEGER | NULL | Best-effort recent 10K time in seconds. See above. |
| `available_days_json` | TEXT (JSON) | NULL | Required. JSON array e.g. `["Mon","Wed","Fri","Sun"]`. Claude **must not** schedule sessions on days outside this set. |
| `current_injury` | TEXT | NULL | Free-text injury/niggle note from `/injury` or `/profile`. Always passed to Claude when present. |

---

### C · HR Zones `[P1 — Core]`

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `max_hr` | INTEGER | NULL | Required. Set via the conversational `/profile` flow ([spec.md §3.2](spec.md)). Defaults to `220 − age` if user skips. |
| `hr_zone1_max` | INTEGER | NULL | Top of Z1 in bpm. Defaults to 60% `max_hr`. |
| `hr_zone2_max` | INTEGER | NULL | Top of Z2 in bpm. Defaults to 70% `max_hr`. |
| `hr_zone3_max` | INTEGER | NULL | Top of Z3 in bpm. Defaults to 80% `max_hr`. |
| `hr_zone4_max` | INTEGER | NULL | Top of Z4 in bpm. Defaults to 90% `max_hr`. (Z5 is anything above this.) |

---

### D · Goals `[P1 — Core]`

Set via `/goal` (see [spec.md §3.3](spec.md)).

> **Constraint**: At least one of `weekly_volume_goal_km` OR `race_date` must be set before coaching is generated. Both can coexist — when both are set, weekly volume is the budget and race goal shapes how it's spent. Enforced at the application layer.

> **Race day cleanup**: On the first webhook event after `race_date < today`, the backend NULLs all four race goal columns and prepends a one-line congratulations note to the next coaching message. See [spec.md §3.3](spec.md).

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `weekly_volume_goal_km` | FLOAT | NULL | Target weekly km. Claude balances next-session recommendations against remaining volume. |
| `race_date` | DATE | NULL | Race day. NULL = no race goal active. |
| `race_distance` | TEXT | NULL | Enum: `5K` \| `10K` \| `Half` \| `Marathon` \| `other`. |
| `race_distance_m` | FLOAT | NULL | Distance in metres. Required only if `race_distance = 'other'`. |
| `race_target_secs` | INTEGER | NULL | Target finish time in seconds. NULL = "just complete it" — no time goal. |

---

### E · Coaching State `[P1 — Core]`

The canonical "what's next" pointer. Read by `/status` and `/plan`. Written by both automatic post-run coaching ([spec.md §3.4](spec.md)) and on-demand `/plan` calls ([spec.md §3.5](spec.md)).

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `next_planned_session_json` | JSON | NULL | Latest next-session recommendation. Same shape as `runs.claude_next_session` (see §4H.1). NULL until the first run is ingested or `/plan` is called. |
| `next_planned_session_updated_at` | DATETIME | NULL | When the current plan was generated. |
| `next_planned_session_source` | TEXT | NULL | `'post_run'` (auto-generated after a Strava upload, references `runs.id`) \| `'plan_command'` (generated by `/plan`). |
| `next_planned_session_run_id` | INTEGER | NULL | FK → `runs.id`. Set when `source = 'post_run'`. NULL when `source = 'plan_command'`. |

---

## 8. invite_codes Table

Single-use codes generated by `/invite` (admin only) and consumed by `/start <code>` during onboarding. See [spec.md §3.1, §3.9](spec.md).

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `code` | TEXT | NOT NULL | Primary key. The opaque token shown to the friend (e.g. URL-safe random 12 chars). |
| `created_by` | INTEGER | NOT NULL | FK → `users.id`. The admin who issued it. |
| `created_at` | DATETIME | NOT NULL | When the code was generated. |
| `used_by` | INTEGER | NULL | FK → `users.id`. NULL = unused. |
| `used_at` | DATETIME | NULL | When the code was redeemed. NULL = unused. |
| `expires_at` | DATETIME | NULL | Optional expiry. NULL = no expiry. |

**Validation rules**:
- `/start <code>` accepts only codes where `used_by IS NULL` AND (`expires_at IS NULL OR expires_at > NOW()`).
- On successful Strava OAuth completion, the backend sets `used_by = <new_user.id>` and `used_at = NOW()` in the same DB transaction that creates the user row.

---

## 9. oauth_states Table

CSRF protection for the Strava OAuth callback. The bot generates a random `state` token whenever it issues an OAuth link via `/start`, and the `/auth/strava/callback` endpoint validates that the returned `state` matches a real, unexpired row before exchanging the code for tokens. Without this, an attacker could trick a victim into clicking a crafted callback URL that links the victim's Telegram account to the attacker's Strava account.

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `state` | TEXT | NOT NULL | Primary key. URL-safe random token (~32 chars), generated with `secrets.token_urlsafe(24)`. Sent as the `state` query parameter in the Strava OAuth URL. |
| `telegram_user_id` | BIGINT | NOT NULL | The Telegram user who initiated the OAuth flow. The callback associates the Strava account with this Telegram ID. |
| `invite_code` | TEXT | NULL | FK → `invite_codes.code`. The code the user supplied via `/start <code>`. NULL for the admin bootstrap path (admin skips the invite-code requirement, see [spec.md §3.1](spec.md)). |
| `created_at` | DATETIME | NOT NULL | When the state was generated. |
| `expires_at` | DATETIME | NOT NULL | `created_at + 5 minutes`. Tightly bounded — OAuth is meant to complete in seconds, not minutes. |

**Lifecycle**:
1. **Issued**: When the bot handles `/start` (with a valid invite code, or as admin), it inserts a row with a fresh `state`, the user's `telegram_user_id`, the invite code (if any), and a 5-minute expiry. It then sends the user a Strava OAuth URL with the `state` query parameter.
2. **Validated**: When `/auth/strava/callback?code=...&state=...` fires, the backend:
   - `SELECT FROM oauth_states WHERE state = ? AND expires_at > NOW()`
   - If no row: 400 with a friendly error page ("OAuth session expired or invalid — try /start again")
   - Otherwise: extract `telegram_user_id` and `invite_code`, exchange the Strava code for tokens, and proceed to user creation
3. **Consumed**: In the same DB transaction that creates the new `users` row and consumes the invite code (if any), the `oauth_states` row is **deleted**. One-shot, never reused.

**Cleanup of expired rows**: Lazy. Each insert is preceded by `DELETE FROM oauth_states WHERE expires_at < NOW()`. At personal scale this keeps the table at single-digit row counts. No background job needed.

**Why a table and not stateless HMAC**: a DB-backed state is simpler to reason about, easier to inspect during debugging, and naturally one-shot (deleting the row guarantees a state can't be replayed). The cost — one extra small table — is negligible.