"""GET /auth/strava/callback — Strava OAuth redirect handler.

Phase 5: scaffolded but minimal — your admin user is already seeded so you
don't need to go through OAuth to get phase 5 working end-to-end.

Phase 8 fills this in fully: validate `oauth_states.state`, exchange `code`
for tokens, atomically create the `users` row, mark the invite code consumed,
delete the `oauth_states` row. See [spec.md §3.1](spec.md) and
[schema.md §9](schema.md).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/auth/strava/callback", response_class=HTMLResponse)
async def strava_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> str:
    """Phase 5 placeholder. Phase 8 implements the full OAuth flow."""
    if error:
        log.warning("auth.strava.error error=%s", error)
        return _html("Authorization denied", f"Strava returned: {error}")

    if not code or not state:
        return _html("Missing parameters", "Both `code` and `state` are required.")

    log.info("auth.strava.callback (phase 8 will handle this) state=%s", state[:8])
    return _html(
        "OAuth callback received",
        "Phase 8 will complete the onboarding here. Your admin user is already "
        "seeded directly in the DB for phases 5–7, so you can ignore this for now.",
    )


def _html(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:520px;margin:80px auto;"
        "padding:0 24px;color:#222;line-height:1.5}</style>"
        "</head><body>"
        f"<h1>{title}</h1><p>{body}</p>"
        "<p>You can close this tab and return to Telegram.</p>"
        "</body></html>"
    )
