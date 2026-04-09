# ClaudeCoach

**AI-Powered Running Coach — Backend + Telegram**
*Private use · Apple Watch + Garmin · April 2026*

---

## 1. What Is ClaudeCoach

ClaudeCoach is a personal, automated running coach that connects your Strava account to Claude AI. Every time you finish a run and upload it to Strava, the system automatically analyses your performance and sends you a personalised coaching message via Telegram — what went well, what to watch, and exactly what your next workout should be.

No webapp. No frontend. No manual input required after initial setup. Telegram is the entire UI.

**Users**: Me and a small invited group of friends. Each user has their own profile, goals, and run history.

---

## 2. Core Loop

```
You run → Strava upload → Webhook → Claude analyses → Telegram coaching message
```

1. You finish a run and your watch syncs to Strava
2. Strava fires a webhook to the ClaudeCoach backend
3. Backend looks up the user, fetches the activity + streams + last 4 weeks of history
4. Backend builds an adaptive prompt with the user's profile, goals, history, and current run
5. Claude returns a structured coaching response
6. Backend sends the response to the user's Telegram chat

The bot is **purely reactive** — it never sends unsolicited messages. Coaching only happens in response to a run upload or an explicit command.

---

## 3. Features

### 3.1 Onboarding (Invite-Gated)

**Goal**: A friend goes from zero to receiving coaching in under 5 minutes, with no manual work from the admin beyond issuing an invite code.

**Flow**:
1. Admin runs `/invite` in their own Telegram chat → bot returns a single-use invite code
2. Friend opens the bot and sends `/start <code>`
3. Bot validates the code. If invalid or used, the bot refuses politely and stops.
4. Bot generates a random `state` token for CSRF protection (stored in `oauth_states` with a 5-min expiry, see [schema.md §9](schema.md)) and replies with a Strava OAuth link carrying the `state` query parameter.
5. Friend taps the link, logs into Strava, clicks Authorize
6. Strava redirects to the backend `/auth/strava/callback` with an auth code **and** the `state` token
7. Backend looks up the `state` row; if missing or expired, rejects the callback. Otherwise, exchanges the code for access + refresh tokens. In a single DB transaction: creates the user record (linking `athlete_id` ↔ `telegram_chat_id`), marks the invite code as consumed, and deletes the `oauth_states` row.
8. Bot confirms onboarding and prompts the user to fill out their profile via `/profile` and set a goal via `/goal`
9. Once the user has at least a profile baseline **and** a goal (weekly volume or race), the bot is ready. Future Strava uploads trigger automatic coaching.

**Privacy**: The bot ignores any message from a Telegram user not in the database, except `/start <code>`.

**Admin bootstrap**: When a new user's `telegram_user_id` matches the `BOOTSTRAP_ADMIN_TELEGRAM_USER_ID` env var, the user is created with `is_admin = true` and **skips the invite-code requirement** on their `/start`. This is how the first admin (the project owner) gets onto a fresh deployment without needing a pre-existing invite code.

---

### 3.2 Profile Management — `/profile`

Users provide best-effort baseline data so Claude can anchor coaching from day one (no cold-start problem).

**Fields**:
| Field | Required | Used For |
|---|---|---|
| Age | Yes | HR zone defaults, load estimation |
| Weight (kg) | Yes | Load and effort estimation |
| Max HR (bpm) | Yes | HR zone computation (Z1–Z5) |
| Recent 5K time | One of 5K/10K required | Anchors target paces for easy/tempo/interval sessions |
| Recent 10K time | One of 5K/10K required | Same |
| Available days | Yes | Days of the week the user can run (e.g. Mon/Wed/Fri/Sun). Claude only schedules sessions on these days. |
| Current injuries / niggles | Optional | Free-text. Claude factors this in when planning the next session. |

**Interaction** — **conversational flow** via python-telegram-bot's `ConversationHandler`:

- `/profile` with no arguments: shows current values **and** offers an inline button to edit. Tapping a field starts the conversational flow for that single field.
- `/profile` on first use (or with missing required fields): bot walks the user through each field one at a time, asking a question and waiting for a free-text reply. e.g.
  - Bot: *"What's your age?"* → user: `28` → bot: *"Weight in kg?"* → user: `68.5` → bot: *"Max HR? (or skip — I'll use 220 − age)"* → user: `185` → ... and so on through 5K/10K, available days, injury notes.
- `/cancel` exits the conversation at any point. Partial data is **not** saved — the user must complete a field's flow for the value to persist.
- Input formats accepted by the parser:
  - Age: integer
  - Weight: float, kg
  - Max HR: integer, bpm
  - 5K / 10K time: `mm:ss` (e.g. `20:30`)
  - Available days: comma-separated abbreviations (e.g. `Mon,Wed,Fri,Sun`) — case-insensitive
  - Injury: free text, max 200 chars
- Invalid input within a step: the bot replies with the expected format and re-prompts. After 3 failed attempts, the conversation is cancelled with a "try again later" message.

Free-text replies are only meaningful **inside** an active conversation. Outside of one, the bot still ignores free text per [§5](spec.md).

**Why baselines matter**: ClaudeCoach has no cold-start problem — even on the very first ingested run, Claude has the user's stated 5K/10K time as a fitness anchor and can give a calibrated next-session recommendation immediately.

---

### 3.3 Goal Management — `/goal`

Users must have **at least one** of the following two goal types active. They can have both.

**Weekly Volume Goal**:
- Single number: target km per week (e.g. `40`)
- Claude reconciles next-session recommendations against the remaining weekly volume

**Race Goal**:
- Race date (e.g. `2026-06-15`)
- Race distance (e.g. `10K`, `Half`, `Marathon`)
- Target finish time (e.g. `45:00`)
- Claude periodises training around the race date — base building, sharpening, taper

**When both are set**: Claude must recommend run types that **fit inside the weekly volume target** *and* **progress the user toward the race goal**. Weekly volume is the budget; race goal shapes how that budget is spent (e.g. allocating intervals vs long runs vs easy mileage).

**Interaction** — **conversational flow** like `/profile` (consistent UX):

- `/goal` with no arguments: shows current goals **and** offers inline buttons to edit or clear each goal type.
- Editing weekly volume: bot asks *"Target km per week?"* → user replies `40` → saved.
- Editing race goal: bot walks through each race field one at a time:
  1. *"Race date? (YYYY-MM-DD)"* → user: `2026-06-15`
  2. *"Race distance? (5K / 10K / Half / Marathon / other)"* → user: `10K`
  3. (if `other`) *"Distance in metres?"* → user: `15000`
  4. *"Target finish time? (mm:ss or h:mm:ss, or `skip` if no time goal)"* → user: `45:00`
- `/cancel` exits at any point. Partial data is not saved.
- Clearing a goal goes through a confirmation step ("Clear weekly volume goal? yes/no") before applying, since clearing both would leave the user without coaching.

**Validation rules** (enforced when `/goal` is used; reject the command with a clear error message on failure):

| Field | Rule |
|---|---|
| `weekly_volume_goal_km` | Must be `> 0`. Reasonable upper bound: `≤ 250` (anything higher is almost certainly a typo). |
| `race_date` | Must be **strictly in the future** (`> today` in the user's timezone). |
| `race_distance` | Must be one of `5K` \| `10K` \| `Half` \| `Marathon` \| `other`. |
| `race_distance_m` | Required if `race_distance = 'other'`. Must be `> 0`. Reasonable upper bound: `≤ 200_000` (200 km). |
| `race_target_secs` | If provided, must be `> 0`. Optional — NULL means "just complete it, no time goal". |
| Constraint | After the command runs, **at least one** of `weekly_volume_goal_km` or `race_date` must still be set. `/goal clear` commands that would leave both NULL are rejected. |

**Race day cleanup**: On the first webhook event after `race_date < today`, the backend auto-clears the race goal fields (`race_date`, `race_distance`, `race_distance_m`, `race_target_secs` → NULL). The user gets a one-line congratulations note prepended to their next coaching message: *"🏁 Your race goal date has passed — race goal cleared. Set a new one anytime with `/goal race ...`."* If `weekly_volume_goal_km` is also unset at that point, the bot prompts the user to set a new goal before generating further recommendations.

---

### 3.4 Automatic Post-Run Coaching (the centrepiece)

**Trigger**: Strava webhook fires when a user uploads an activity. The webhook payload includes `athlete_id`, which the backend uses to look up the user.

**Backend pipeline** — split into a **synchronous webhook handler** (must return HTTP 200 in <2s) and an **asynchronous background task** (does the heavy work):

**Synchronous** (in the FastAPI route handler):
1. Receive webhook → validate `aspect_type == 'create'` (ignore `update`/`delete`) → look up user by `athlete_id`. If unknown, return 200 and stop.
2. Schedule the background task via FastAPI's `BackgroundTasks` and return HTTP 200 immediately.

**Asynchronous** (background task, runs after the 200 response):
3. `GET /activities/{id}` for summary stats. If a 401 comes back, refresh the Strava token and retry. If refresh also fails (refresh token expired or revoked), DM the user *"Your Strava connection has expired — tap here to reconnect: <oauth_link>"*, mark the user as disconnected, and stop.
4. **Filter on activity type**: only `Run` is processed. `TrailRun`, `VirtualRun`, `Walk`, `Hike`, `Ride`, etc. are silently ignored — no DB write, no Claude call, no Telegram message.
5. `GET /activities/{id}/streams` for raw stream arrays (HR, pace, cadence, altitude over time)
6. Compute derived metrics from streams: HR drift, pace splits, time in HR zones, normalised pace, etc.
7. Persist the new run + computed metrics to the DB.
8. **Completeness gate**: if the user's profile is incomplete OR they have no goal set, DM *"Run logged — finish your profile (`/profile`) and set a goal (`/goal`) to start receiving coaching."* and stop. The run stays in the DB so history isn't lost; the next ingested run will trigger normal coaching once setup is finished.
9. Fetch the user's run history from the **last 4 weeks** (typically 12–20 runs).
10. Build the adaptive Claude prompt (see below) and call the Claude API.
11. Parse Claude's structured response and send it as **two Telegram messages** (post-run review, then next session).
12. Mirror `claude_next_session` into `users.next_planned_session_json` (source = `'post_run'`).

**Trade-off of `BackgroundTasks`**: if Render restarts mid-task, that single webhook is lost (Strava won't retry because we already returned 200). At personal scale this is acceptable — Render free-tier restarts are rare and a missed coaching message is recoverable via `/plan`.

**Prompt input to Claude includes**:
- User profile (age, weight, max HR, baseline 5K/10K, available days, current injuries)
- Active goals (weekly volume, race goal if set)
- Weekly volume run so far this week vs target
- Last 4 weeks of run history (date, type, distance, time, avg HR, avg pace, perceived effort if available)
- Current run: full summary stats + computed stream metrics
- Today's date and the next available run day from the user's availability

**Claude's response (every coaching message contains all four sections)**:

1. **Run Summary** — distance, moving time, average pace, average HR, elevation gain, time in HR zones. The raw "what happened".
2. **What Went Well** — positive observations: pacing, HR control, consistency, executing the planned workout, etc.
3. **What to Watch** — concerns: HR drift, positive splits, overreach signals, fatigue accumulation, injury risk indicators.
4. **Next Session** — a **structured workout** scheduled for the next available run day:
   - Run type (from the fixed taxonomy in §4)
   - **When**: phrased as both a relative offset and an absolute date, e.g. `3 days from now (Sun, 12 April 2026)`. The day must match an available day in the user's profile.
   - Target distance
   - Target pace (or pace range)
   - Target HR zone
   - **Full workout breakdown**: warmup → main set → cooldown
     - Example: `2km easy WU @ 6:00/km · 6×800m @ 4:00/km w/ 90s jog rest · 2km easy CD`
     - Easy/recovery runs may be a single block: `8km easy @ 5:45–6:00/km, Z2`

> The backend passes today's date into the Claude prompt so the model can compute the relative offset accurately. The structured next session is persisted to **two** places: `runs.claude_next_session` (per-run history, immutable) and `users.next_planned_session_json` (the canonical "what's next" pointer, overwritten on each new run or `/plan` call). `/status` reads from the latter.

**Delivery**: **Two Telegram messages sent in sequence** from a single Claude API call:

1. **Message 1 — Post-Run Review**: contains the first three sections (Run Summary, What Went Well, What to Watch). Stored in `runs.claude_post_run_review`.
2. **Message 2 — Next Session**: contains the structured workout from section 4. Stored in `runs.claude_next_session` (JSON) and mirrored to `users.next_planned_session_json`.

The Claude response is structured (JSON output) so the backend can split it cleanly into the two parts. Both messages are Markdown-formatted for mobile readability. Splitting into two messages keeps each one focused and avoids hitting Telegram's 4096-char per-message limit even on long analyses.

---

### 3.5 On-Demand Plan — `/plan`

**Trigger**: User sends `/plan` at any time, even without a new run upload.

**Behaviour**: Same as the "Next Session" portion of automatic coaching, but generated on demand. Useful when:
- The user wants to know what's next before heading out
- The user skipped a recommended run and wants a fresh plan
- The user wants to see the recommendation again

**Backend**: Fetches the user's last 4 weeks of history (no new run to analyse), runs Claude with the same context, returns only the structured next-session block. The new plan **overwrites** `users.next_planned_session_json` so `/status` and future `/plan` calls reflect the latest recommendation.

---

### 3.6 Run History — `/history`

**Trigger**: `/history` shows the user's last 5 runs along with the coaching summaries that were sent at the time.

**Each entry shows**:
- Date (e.g. `Sun 12 Apr`) and run type
- Stats line: distance · moving time · avg pace · avg HR
- One-line digest of the original coaching message (what went well / what to watch combined)

**Optional**: `/history N` to fetch the last N runs (cap at 20).

**Format** — compact card per run, sent as one Telegram message containing all entries (MarkdownV2):

```
*Sun 12 Apr* · Easy
8.0km · 45:12 · 4:30/km · 142bpm
"Solid easy run — HR controlled, watch fatigue building from Wed."

*Fri 10 Apr* · Intervals
10.0km · 47:30 · 4:45/km · 162bpm
"Strong session — hit all reps cleanly."

*Wed 8 Apr* · Long
18.0km · 1:42:00 · 5:40/km · 148bpm
"Good endurance work, slight HR drift in km14+."
```

The one-line digest is generated by Claude as part of the `submit_coaching` tool call (the `post_run_review.digest` field, ≤140 chars) and stored in `runs.claude_digest`. See [app/prompts/coaching_tool_schema.json](app/prompts/coaching_tool_schema.json).

If the user has zero runs, the bot replies: *"No runs yet — upload one to Strava and I'll send your first coaching message automatically."*

---

### 3.7 Injury Reporting — `/injury`

**Trigger**: User flags a current injury or niggle that should affect upcoming sessions.

**Interaction**:
- `/injury <free text>` — adds or replaces the user's current injury note (e.g. "left achilles sore, 3/10")
- `/injury clear` — removes the note

**Effect**: The injury note is included in the Claude prompt for both automatic post-run coaching and `/plan`. Claude is instructed to factor it in — typically by reducing intensity, swapping intervals for easy runs, or recommending rest.

**Note**: This is the same field as `current injuries / niggles` in the profile. `/injury` is a fast-path command for the common case of "I tweaked something today, take it into account for my next run."

---

### 3.8 Status Check — `/status`

Shows the user a snapshot of their current setup:
- Strava connection state (connected / token expired / disconnected)
- Profile completeness
- Active goals (weekly volume, race goal)
- Current week's volume vs target
- Active injury note, if any
- Date of most recent ingested run
- Next planned session (type, scheduled day, distance) — read from `users.next_planned_session_json`

**Format** — sectioned with bold headers (MarkdownV2):

```
🟢 *Strava connected*

*Profile*
Age 28 · Weight 68kg · Max HR 185
5K 20:30 · Days: Mon Wed Fri Sun

*Goals*
Weekly 40 km
10K race — 15 Jun (target 45:00)

*This week*
24 / 40 km (60%)

*Last run*
Sun 12 Apr · Easy · 8.0km

*Next session*
Tue 14 Apr (in 2 days)
Intervals · 10.0km · 4:30/km · Z4
```

The Strava emoji at the top changes with state: 🟢 connected · 🟡 token expired (auto-refreshing) · 🔴 disconnected (re-link required). Sections are omitted entirely if not applicable (no injury → no injury section, no race goal → no race line under Goals).

---

### 3.9 Admin: Invite Generation — `/invite`

**Restricted to admin user(s)** identified by Telegram user ID in config.

**Behaviour**: `/invite` generates a single-use invite code, stores it in the DB with `created_at` and `used_by = NULL`, and replies with the code. The admin shares the code with a friend out-of-band. When the friend completes `/start <code>`, the code is marked consumed.

---

## 4. Run Type Taxonomy

Claude classifies every run (and every recommendation) into a **fixed taxonomy**. Stored as an enum in the DB so runs can be filtered, charted, and aggregated cleanly.

| Type | Purpose |
|---|---|
| `easy` | Aerobic base, conversational pace, Z2 |
| `long` | Endurance development, sustained sub-threshold effort |
| `tempo` | Lactate threshold, comfortably hard, ~Z3–low Z4 |
| `intervals` | VO2max / speed, structured reps with rest, Z4–Z5 |
| `recovery` | Active recovery, very easy, Z1 |
| `race` | Race day or race-pace effort |

When ingesting a Strava run, Claude assigns the most likely type based on pace, HR distribution, and structure. When recommending the next session, Claude picks from the same taxonomy.

---

## 5. Bot Command Reference

| Command | Who | What It Does |
|---|---|---|
| `/start <code>` | New users | Begin onboarding with an invite code, returns a Strava OAuth link. Admin user (matching `BOOTSTRAP_ADMIN_TELEGRAM_USER_ID`) skips the invite-code requirement. |
| `/help` | All users | Lists every command with one-line descriptions. Static content, no Claude call. |
| `/profile` | All users | View profile, then offer inline buttons to edit. Editing starts a conversational flow (see [§3.2](spec.md)). |
| `/goal` | All users | View current goals, then offer inline buttons to edit/clear. Editing starts a conversational flow (see [§3.3](spec.md)). |
| `/plan` | All users | Get next-session recommendation on demand. Overwrites `users.next_planned_session_json`. |
| `/history [N]` | All users | Show last N runs and their coaching summaries (default 5, max 20) |
| `/injury <text>` | All users | Set current injury / niggle note (one-shot, not conversational — fast-path for the common case) |
| `/injury clear` | All users | Remove injury note |
| `/status` | All users | Connection state, goals, weekly progress, injury, last run, next planned session |
| `/cancel` | All users | Exit any active conversational flow without saving partial data |
| `/invite` | Admin only | Generate a single-use invite code |

**Free-text messages**: ignored **outside** of an active conversational flow. Inside a conversation (started by `/profile` or `/goal`), free-text replies are interpreted as answers to the bot's prompts.

**Proactive messaging**: none. The bot only sends messages in response to a Strava webhook or a user command. The single exception is the Strava reconnect DM ([§3.4](spec.md) step 3) and the race-day cleanup congratulations note ([§3.3](spec.md)) — both attached to a Strava webhook event, not unsolicited.

---

## 6. System Architecture

Single Python backend server. Telegram handles all user interaction.

```
[User's Watch/Phone]
       | syncs run
       v
[Strava App] ──────────────────────────────────────────────
       | fires webhook event (includes athlete_id)         |
       v                                          [Strava OAuth]
[ClaudeCoach Backend Server] <── /start via Telegram ──────┘
       |
       | === SYNCHRONOUS (FastAPI route, must return 200 in <2s) ===
       | 1. receive webhook → validate aspect_type=='create'
       | 2. look up user by athlete_id (in-memory cache or fast SELECT)
       | 3. dispatch BackgroundTask, return HTTP 200
       |
       | === ASYNCHRONOUS (BackgroundTask, runs after the 200) ===
       | 4. GET /activities/{id} → on 401, refresh token; on refresh fail,
       |    DM reconnect link, mark disconnected, stop
       | 5. filter: skip unless activity.type == 'Run'
       | 6. GET /activities/{id}/streams → raw stream arrays
       | 7. compute derived metrics from streams
       | 8. store new run + metrics in DB
       | 9. completeness gate: if profile/goal incomplete, DM
       |    "finish setup", stop
       | 10. fetch last 4 weeks of run history for context
       | 11. build adaptive Claude prompt → POST to Claude API
       |     (claude-sonnet-4-6, structured JSON via tool use)
       | 12. parse response → post-run review (text) + next session (JSON)
       | 13. mirror next session to users.next_planned_session_json
       | 14. send TWO Telegram messages: (1) post-run review, (2) next session
       v
[Telegram Bot] → review + next session land in user's pocket
```

### Key Design Decisions

- **No webapp** — Telegram is the entire UI. Zero frontend complexity.
- **`athlete_id` is the link** — Strava always sends this in webhook payloads, so the backend always knows whose run triggered the event.
- **`telegram_chat_id` is the delivery address** — stored per user, used to send coaching messages.
- **Private by design** — invite-code gated. The bot ignores anyone not in the DB except `/start <code>`.
- **Stateful per user** — each user has their own profile, goals, run history, and coaching context stored separately.
- **Reactive only** — no scheduled jobs, no proactive messages. Simplifies the server.
- **No cold start** — profile baselines (5K/10K times) anchor coaching from the very first ingested run.

---

## 7. Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Language | **Python 3.12** | Latest stable, supported on Render, modern type hints and error messages. |
| Package manager | **uv** | Astral's Rust-based tool. 10–100× faster than pip/poetry, single tool for venv + dependencies, standard `pyproject.toml`. |
| Linter / formatter | **ruff** (lint + format) | Single fast tool replacing black + flake8 + isort + pyupgrade. Configured in `pyproject.toml`. No mypy at this scale. |
| Web framework | FastAPI | Handles Strava webhooks and OAuth redirect cleanly |
| Telegram bot | python-telegram-bot v21+ | Well-documented, async support, `ConversationHandler` for `/profile` and `/goal` flows. Long-polling so the bot needs no public URL of its own. |
| Database | PostgreSQL on **Supabase** | Free tier (500MB), managed Postgres, no DB admin work. SQLite skipped — go straight to Postgres so there's no migration later. |
| ORM | SQLAlchemy 2.x + Alembic | SQLAlchemy for queries, Alembic for migrations from day one |
| AI | Claude API — **`claude-sonnet-4-6`** | Best reasoning-to-cost ratio for multi-factor coaching analysis. ~$0.06 per coaching call estimated. |
| Strava auth | OAuth 2.0 via FastAPI | Industry-standard auth flow |
| Hosting | **Render** (web service) | Free tier, deploy from GitHub, public HTTPS URL for the Strava webhook + OAuth callback. Free instances cold-start after 15 min idle (~30s wake-up); acceptable for personal scale. |

---

## 8. Build Phases

Build incrementally. Each phase is independently testable. Never move to the next phase until the current one works end-to-end.

| # | Phase | Deliverable |
|---|---|---|
| 1 | Strava API | OAuth setup, fetch your own activity list + streams in Python |
| 2 | Claude Integration | Send a sample run + mock profile/goals to Claude, get structured coaching output in terminal |
| 3 | Telegram Bot (basic) | Bot responds to `/start`, `/profile`, `/goal`; sends a test message |
| 4 | Database | Supabase Postgres + SQLAlchemy models + Alembic migrations for users, runs, invite_codes; seed a test user |
| 5 | FastAPI Server | `/webhook` endpoint + `/auth/strava` OAuth callback route |
| 6 | End-to-end (local) | Full pipeline locally. Upload a run → two Telegram messages. Use ngrok for the Strava webhook. |
| 7 | Deploy to Render | Live 24/7 web service connected to Supabase. Register Strava webhook against the Render public URL. |
| 8 | Friend Onboarding | Test full `/invite` → `/start <code>` → OAuth → first run → Telegram flow with a friend |
| 9 | Bot Commands | Add `/history`, `/plan`, `/status`, `/injury`. Claude uses 4-week run history for richer context. |

---

## 9. Estimated Costs

| Service | Cost | Notes |
|---|---|---|
| Strava API | Free | Free for personal/non-commercial use |
| Telegram Bot | Free | Always free at this scale |
| Claude API (`claude-sonnet-4-6`) | ~$0.06 per coaching call | 5 users × 5 runs/week ≈ ~$6/month |
| Render (web service) | Free tier | Personal scale fits comfortably; cold-starts after 15 min idle |
| Supabase (Postgres) | Free tier | 500 MB DB, daily backups included |
| **Total** | **~$5–8/month** | Almost entirely Claude API calls |

---

## 10. First Steps

1. Go to `strava.com/settings/api`, create an application, get Client ID and Client Secret
2. Write a Python script to fetch your last 5 activities + streams using the access token
3. Send one activity (with a mock profile + goal) to Claude API — see what the raw structured coaching output looks like
4. Build a simple FastAPI server with a `/webhook` route + `/health` endpoint
5. Test locally with ngrok pointing at your Strava webhook URL
6. Deploy to Render + Supabase
7. Run `scripts/register_webhook.py` once with prod env vars to register the Strava webhook against your Render public URL
8. Send `/start` to the bot from your admin Telegram account, complete OAuth, run first activity

---

## 11. Operational Decisions

Locked-in choices for hosting, secrets, environment, and runtime behaviour. These are the answers to "what else needs to be ascertained before building".

### 11.1 Hosting

| Concern | Choice |
|---|---|
| Web/server hosting | **Render** (free web service tier). Single deployment from GitHub. |
| Database | **Supabase** Postgres (free tier, 500 MB). App connects via `DATABASE_URL`. |
| Public URL | Render provides one (e.g. `claudecoach.onrender.com`). Used for the Strava webhook callback and the Strava OAuth redirect. |
| Telegram bot | Long-polling, runs in the same Render service as FastAPI (background asyncio task). No separate Telegram webhook URL needed. |
| Cold starts | Render free tier sleeps after 15 min idle, ~30 s to wake. Acceptable: Strava retries webhooks, and the first webhook of the day takes a one-time wake-up hit. |
| Supabase pause | Supabase free tier pauses projects after 1 week of inactivity. For an active user base of ≥1 runner this won't trigger; otherwise a tiny weekly cron-ping keeps it alive. |
| Backups | Supabase free tier includes daily backups. No additional backup setup required. |

**No dev/prod split**. Single Strava app, single Render deployment, single Supabase project. Personal scale doesn't justify the duplication.

### 11.2 Secrets & Environment Variables

All secrets live in Render's environment variable UI. A local `.env` file mirrors them for development. **No secrets are committed to git.**

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection string |
| `STRAVA_CLIENT_ID` | Strava OAuth app client ID |
| `STRAVA_CLIENT_SECRET` | Strava OAuth app client secret |
| `STRAVA_WEBHOOK_VERIFY_TOKEN` | Random string used to verify Strava webhook subscription challenges and reject spoofed callbacks |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `ANTHROPIC_API_KEY` | Claude API key |
| `CLAUDE_MODEL` | Defaults to `claude-sonnet-4-6`. Overridable so you can swap models without a deploy. |
| `CLAUDE_MAX_OUTPUT_TOKENS` | Defaults to `3500`. Caps Claude's response length. |
| `BOOTSTRAP_ADMIN_TELEGRAM_USER_ID` | The project owner's Telegram user ID. On first `/start` from this ID, the user is auto-flagged `is_admin = true` and skips the invite-code requirement. |
| `APP_BASE_URL` | Render public URL (e.g. `https://claudecoach.onrender.com`). Used to build the Strava OAuth redirect URI. |
| `LOG_LEVEL` | Defaults to `INFO` |

### 11.3 Strava OAuth Token Storage

**Plain text in the DB.** Personal-scale, single-tenant Supabase, no PCI/PHI data — encryption at rest is overkill. Revisit if the user base grows beyond a small invited circle.

### 11.4 Claude Model

**`claude-sonnet-4-6`** (current Sonnet 4.6).

- **Why Sonnet over Opus**: Coaching reasoning is non-trivial but not Opus-level. Opus is ~5× more expensive per call for marginal quality gain on this task.
- **Why Sonnet over Haiku**: Multi-factor periodisation (weekly volume budget × race-goal phase × HR drift × injury notes × structured workout design) is too much for Haiku to reliably get right.
- **Output budget**: ~3500 tokens max. Empirically a structured coaching response (post-run review + structured next session as JSON) lands around 1500–2500 tokens.
- **Estimated cost**: ~$0.06 per coaching call → ~$6/month for 5 users × 5 runs/week.

Locked behind `CLAUDE_MODEL` env var so it's swappable without a code change.

### 11.5 Time Zones

Each user has a `timezone` column (IANA name, e.g. `Asia/Singapore`). Used to compute "today" for the Claude prompt and to schedule the next session on the correct local day. Captured during onboarding (defaults to the user's profile timezone from Strava if available, otherwise prompted).

### 11.6 Prompt Versioning

Every coaching call records a `prompt_version` (TEXT) on the `runs` row — a short string like `v1`, `v2.coaching-tightening`, etc. When the prompt template changes, bump the version. Lets you correlate response quality with prompt iterations later, and reprocess old runs against a new prompt without losing the original.

### 11.7 Multiple Runs Per Day

Edge case, treated naively: each run upload triggers a full coaching cycle independently. The second run's "next session" recommendation will likely push to the day after tomorrow. No special deduplication.

### 11.8 Webhook Idempotency

Strava occasionally retries webhooks. The `runs.strava_activity_id` UNIQUE constraint (schema §4A) catches duplicates — the ingest pipeline catches the integrity error and returns HTTP 200 so Strava stops retrying. No duplicate Claude calls or Telegram messages.

### 11.9 Logging

Python's stdlib `logging` to stdout. Render captures stdout automatically. Each request is tagged with `user_id`, `activity_id`, and an `event` field (e.g. `webhook_received`, `claude_called`, `telegram_sent`, `error`). No Sentry or external aggregator needed at personal scale.

### 11.10 Cost Cap

No hard cap initially. Manual check via the Anthropic billing dashboard. If a user starts spamming `/plan`, add a per-user daily Claude-call limit later.

### 11.11 Webhook Handler Pattern

The Strava webhook handler is **split** into a synchronous part (must return HTTP 200 within 2s) and an asynchronous background task (does the heavy work). See [§3.4](spec.md) backend pipeline.

- **Synchronous**: validate `aspect_type == 'create'`, look up user, dispatch `BackgroundTask`, return 200.
- **Asynchronous**: Strava fetches → metric computation → DB write → completeness gate → Claude call → 2× Telegram messages.
- **Mechanism**: FastAPI's built-in `BackgroundTasks` (no Celery/Redis). In-process, fire-and-forget.
- **Trade-off**: if Render restarts mid-task, that one webhook is lost (Strava won't retry because it received a 200). Acceptable at personal scale — Render free-tier restarts are rare and a missed coaching message is recoverable via `/plan`.

### 11.12 Healthcheck Endpoint

`GET /health` returns `{"status": "ok"}` instantly. **Liveness only** — no DB ping, no external API check. If the DB is unreachable we want the service to keep running so `/status` can surface a clear error, not restart-loop.

### 11.13 Initial Strava Webhook Registration

After the first deploy, the Strava webhook subscription must be registered against the production URL exactly once. This is done via `scripts/register_webhook.py` — a standalone script you run locally with prod env vars set:

```bash
DATABASE_URL=... STRAVA_CLIENT_ID=... STRAVA_CLIENT_SECRET=... \
APP_BASE_URL=https://claudecoach.onrender.com STRAVA_WEBHOOK_VERIFY_TOKEN=... \
python scripts/register_webhook.py
```

The script:
1. Lists existing subscriptions for the Strava app
2. If one exists with a different URL, deletes it
3. POSTs a new subscription pointing at `${APP_BASE_URL}/webhook` with the verify token
4. Strava calls back with `hub.challenge`; the FastAPI server echoes it; Strava confirms

Re-runnable any time the public URL changes.

### 11.14 Disk Space Planning

Each `runs` row carries stream JSON archives (50–300 KB depending on run length and `stream_resolution`). Rough math at 5 users × 5 runs/week:

- ~25 runs/week → ~5 MB/week → ~250 MB/year of stream archives
- Plus derived metrics, Claude responses, etc. → call it ~400 MB/year
- Supabase free tier is 500 MB → ~12–18 months of runway before hitting the cap

**Strategy**: noted, no action yet. When the database approaches 70% capacity, add a cleanup job that NULLs the `stream_*_json` columns for runs older than 12 months. The derived metrics in P1/P2/P3 columns are kept forever — only the raw archive is dropped. Recovering archives later (if ever needed) requires re-fetching from Strava.

### 11.15 Conversational Flow Pattern

`/profile` and `/goal` use python-telegram-bot's `ConversationHandler` to walk users through field-by-field input with free-text replies. See [§3.2](spec.md) and [§3.3](spec.md).

- **State**: held in-memory by `ConversationHandler` (per-user, per-conversation). Lost on server restart — the user just runs `/profile` again. Acceptable.
- **Cancel**: `/cancel` exits any active conversation. No partial state saved.
- **Validation**: invalid input within a step re-prompts. After 3 failed attempts, the conversation is cancelled with a "try again later" message.
- **Free text outside conversations**: still ignored. The `commands only` rule from [§5](spec.md) holds — free text only matters inside an active conversation.

### 11.16 Project Layout

Layered by concern. Standard FastAPI shape — easy to navigate when you know what kind of file you're looking for.

```
claudecoach/
├── app/
│   ├── main.py              # FastAPI entrypoint, mounts routes, starts bot
│   ├── config.py            # Pydantic Settings — loads env vars
│   ├── db.py                # SQLAlchemy engine + session factory
│   ├── models/              # SQLAlchemy ORM models
│   │   ├── user.py
│   │   ├── run.py
│   │   ├── invite_code.py
│   │   └── oauth_state.py
│   ├── schemas/             # Pydantic models for Claude I/O + API requests
│   │   ├── claude_response.py
│   │   └── strava_webhook.py
│   ├── routes/              # FastAPI route handlers
│   │   ├── webhook.py       # POST /webhook, GET /webhook (subscription challenge)
│   │   ├── auth.py          # GET /auth/strava/callback
│   │   └── health.py        # GET /health
│   ├── services/            # Business logic
│   │   ├── strava.py        # Strava API client + token refresh
│   │   ├── claude.py        # Claude prompt builder + API call
│   │   ├── telegram.py      # Telegram send helpers
│   │   ├── coaching.py      # The full ingest pipeline orchestrator
│   │   ├── metrics.py       # Stream → derived metrics
│   │   ├── pace.py          # format_pace_min / parse_pace_str helpers
│   │   └── hr_zones.py      # Compute Z1–Z5 from max_hr
│   ├── bot/                 # python-telegram-bot handlers
│   │   ├── commands.py      # /start, /profile, /goal, /plan, etc.
│   │   ├── conversations.py # ConversationHandler definitions
│   │   └── runner.py        # Long-polling loop, runs as asyncio task
│   └── prompts/
│       └── v1.py            # Prompt template for prompt_version='v1'
├── alembic/                 # Migrations (initialised with `alembic init`)
├── scripts/
│   └── register_webhook.py  # One-shot Strava webhook registration
├── tests/
│   ├── test_metrics.py      # Pure-function tests for stream math
│   ├── test_pace.py         # format_pace / parse_pace round-trips
│   └── fixtures/            # Saved Strava activity JSONs for replay
├── .env.example
├── pyproject.toml
└── README.md
```

### 11.17 HR Zone Defaults

Simple percentage-of-max-HR model. Uses only `max_hr` from the profile (no resting HR, no LTHR).

| Zone | Range | Purpose |
|---|---|---|
| Z1 | 0% – 60% max_hr | Recovery, very easy |
| Z2 | 60% – 70% max_hr | Aerobic base |
| Z3 | 70% – 80% max_hr | Moderate aerobic |
| Z4 | 80% – 90% max_hr | Lactate threshold |
| Z5 | 90% – 100% max_hr | VO2max / anaerobic |

Implemented in `app/services/hr_zones.py`. The zone boundaries are stored on the `users` table (`hr_zone1_max` … `hr_zone4_max`, see schema.md §7C) so they can be customised per user later without recomputing. On profile creation, all four are auto-populated from `max_hr` × the percentages above.

If `max_hr` is not provided, default to `220 − age`. If neither is provided, the user hasn't completed their profile and coaching is gated per [§3.4 step 8](spec.md).

### 11.18 Pace Format Helpers

**Internal representation**: `float minutes per kilometre`. e.g. `4.5` = 4 min 30 sec/km.

**Display representation**: `m:ss/km` string. e.g. `4:30/km`.

**Helpers** (in `app/services/pace.py`):

```python
def format_pace_min(pace_min_km: float) -> str:
    """4.5 -> '4:30/km'"""

def parse_pace_str(pace_str: str) -> float:
    """'4:30/km' or '4:30' -> 4.5. Raises ValueError on bad input."""

def secs_to_pace_min(total_secs: float, distance_m: float) -> float:
    """Compute pace from raw time + distance. Returns float minutes/km."""
```

**Trade-off acknowledged**: float minutes loses sub-second precision over long runs (~1–2 sec across a marathon), and integer second arithmetic would be technically cleaner. Float minutes was chosen for ergonomics — paces in code read closer to how runners think about them (`4.5` ≈ "four-and-a-half-minute pace" ≈ `4:30/km`).

All `runs` table pace columns use FLOAT type with the `_min` suffix (e.g. `avg_pace_min_km`, `pace_first_half_min`). See schema.md §4B and §4C for the column definitions.

### 11.19 Telegram Markdown Flavour

**MarkdownV2** — the strict, modern parser. All bot messages set `parse_mode='MarkdownV2'` when calling Telegram's `sendMessage`.

**Trade-off**: every dynamic string going into a message must escape `_*[]()~>#+-=|{}.!`. A missed escape character causes the **entire message to fail to render** with a `Bad Request: can't parse entities` error from Telegram.

**Mitigation** — single helper in `app/services/telegram.py`:

```python
_MD_V2_SPECIAL = r'_*[]()~`>#+-=|{}.!'

def escape_md_v2(text: str) -> str:
    """Escape every MarkdownV2 special char with a backslash.
    Use on ALL dynamic content before interpolating into a message template."""
    return ''.join('\\' + c if c in _MD_V2_SPECIAL else c for c in text)

async def send_message(chat_id: int, text: str) -> None:
    """Send a MarkdownV2 message. Caller is responsible for escaping
    dynamic content with escape_md_v2 before passing it in."""
```

**Convention**: all message-rendering helpers (e.g. `render_status(user)`, `render_history_entry(run)`) build the final string with escaped dynamic content interpolated into hand-authored templates. Constant strings in the templates are author-controlled — escaped once at write time. Tests cover round-trips on common edge-case characters (`8.0km`, `(target 45:00)`, `4:30/km`, dates with `-`).

### 11.20 BotFather Command Menu

The list registered with BotFather via `/setcommands`. Shown to users when they type `/` in the chat. Should be set **once per environment** during initial setup.

```
start - Start onboarding (with invite code) or reconnect Strava
help - Show every command
profile - View or edit your runner profile
goal - View or edit your training goals
plan - Get next session recommendation right now
history - Show recent runs and coaching summaries
injury - Set or clear current injury / niggle note
status - Connection state, goals, week progress, next session
cancel - Exit any active conversation
```

`/invite` is **deliberately omitted** — admin-only commands shouldn't appear in the menu where non-admins would see them. Admins type `/invite` manually.
