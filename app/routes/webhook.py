"""Strava webhook routes.

`GET /webhook` — subscription handshake. Strava POSTs a `hub.challenge` query
parameter and we echo it back if `hub.verify_token` matches our env var.

`POST /webhook` — Strava sends a JSON event whenever a user creates / updates /
deletes an activity. We only handle `aspect_type == 'create'` for activities
of type `Run`. The handler returns 200 IMMEDIATELY (within Strava's 2-second
SLA) and dispatches the heavy work to `coaching.ingest_run` via
`fastapi.BackgroundTasks`.

See [spec.md §3.4 backend pipeline](spec.md) for the full flow.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.coaching import ingest_run

log = logging.getLogger(__name__)
router = APIRouter()


class StravaWebhookEvent(BaseModel):
    """Subset of Strava's webhook event payload that we care about."""

    aspect_type: str  # 'create' | 'update' | 'delete'
    object_type: str  # 'activity' | 'athlete'
    object_id: int  # the activity_id (or athlete_id for deauth events)
    owner_id: int = Field(alias="owner_id")  # the athlete_id who owns the object
    subscription_id: int
    event_time: int


# ---------------------------------------------------------------- subscription challenge


@router.get("/webhook")
async def webhook_subscription_challenge(
    hub_mode: str = Query("subscribe", alias="hub.mode"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
) -> dict[str, str]:
    """One-shot exchange used when registering the webhook subscription.

    Strava's POST to `/api/v3/push_subscriptions` immediately calls back here
    with the three `hub.*` params. We echo `hub.challenge` only if the verify
    token matches what we configured.
    """
    settings = get_settings()
    if hub_verify_token != settings.STRAVA_WEBHOOK_VERIFY_TOKEN:
        log.warning("webhook.bad_verify_token received=%s", hub_verify_token[:8])
        raise HTTPException(status_code=403, detail="invalid verify token")

    log.info("webhook.subscription_handshake mode=%s", hub_mode)
    return {"hub.challenge": hub_challenge}


# ---------------------------------------------------------------- event receiver


@router.post("/webhook")
async def webhook_event(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Receive an event, validate, dispatch the coaching pipeline as a background task.

    Returns 200 IMMEDIATELY so Strava doesn't retry. All processing happens in
    the background task — including failures, which are logged but not reported
    back to Strava.
    """
    try:
        body = await request.json()
        event = StravaWebhookEvent.model_validate(body)
    except Exception as exc:
        # Malformed payload — log it but still return 200 so Strava stops retrying.
        log.warning("webhook.malformed body=%s err=%s", body if "body" in dir() else "?", exc)
        return {"ok": "true", "ignored": "malformed"}

    # We only process new activity creations; ignore updates, deletes, athlete events
    if event.object_type != "activity" or event.aspect_type != "create":
        log.info(
            "webhook.skip aspect_type=%s object_type=%s id=%s",
            event.aspect_type,
            event.object_type,
            event.object_id,
        )
        return {"ok": "true", "ignored": event.aspect_type}

    # Dispatch the heavy work, return 200
    bot = request.app.state.bot
    background_tasks.add_task(ingest_run, event.owner_id, event.object_id, bot)
    log.info(
        "webhook.dispatched athlete_id=%s activity_id=%s",
        event.owner_id,
        event.object_id,
    )
    return {"ok": "true"}
