"""Strava OAuth dance — one-off helper to get your first access + refresh tokens.

Run this once to authorise the ClaudeCoach Strava app against your own Strava
account. Phase 1 only — phase 4 replaces it with the proper /auth/strava/callback
endpoint driven by the Telegram /start flow.

Usage:
    uv run python scripts/strava_oauth_dance.py

Prerequisites:
    - STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET set in .env
    - Your Strava app's "Authorization Callback Domain" is `localhost`
      (set at https://www.strava.com/settings/api)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
REDIRECT_URI = "http://localhost/exchange_token"
SCOPES = "read,activity:read_all"


def extract_code(raw: str) -> str | None:
    """Accept either a full redirect URL or a bare code string."""
    raw = raw.strip()
    if "code=" in raw:
        parsed = urlparse(raw)
        return parse_qs(parsed.query).get("code", [None])[0]
    return raw or None


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        sys.exit(1)

    # Step 1: build the authorisation URL
    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "approval_prompt": "force",
    }
    auth_url = f"{STRAVA_AUTH_URL}?{urlencode(auth_params)}"

    print("=" * 72)
    print("STEP 1: Open this URL in your browser and click Authorize:")
    print()
    print(auth_url)
    print()
    print("After clicking Authorize, your browser will try to load")
    print(f"  {REDIRECT_URI}?state=&code=XXX&scope=read,activity:read_all")
    print("and fail with 'connection refused' — that's expected.")
    print("=" * 72)
    print()
    print("STEP 2: Copy the FULL redirected URL from your browser's address bar")
    print("(or just the `code` value) and paste it here:")
    print()

    raw = input("> ")
    code = extract_code(raw)
    if not code:
        print("ERROR: could not extract a `code` from your input")
        sys.exit(1)

    print()
    print(f"Got code: {code[:10]}... (exchanging for tokens)")
    print()

    # Step 3: exchange code for tokens
    try:
        response = httpx.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"ERROR: network error calling Strava: {exc}")
        sys.exit(1)

    if response.status_code != 200:
        print(f"ERROR: Strava returned {response.status_code}")
        print(response.text)
        sys.exit(1)

    data = response.json()
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_at = data["expires_at"]
    athlete = data.get("athlete", {})

    print("=" * 72)
    print("SUCCESS!")
    print()
    print(
        f"Athlete: {athlete.get('firstname', '?')} {athlete.get('lastname', '?')}"
        f"  (id={athlete.get('id')})"
    )
    print()
    print("Add these lines to your .env file:")
    print()
    print(f"DEV_STRAVA_ACCESS_TOKEN={access_token}")
    print(f"DEV_STRAVA_REFRESH_TOKEN={refresh_token}")
    print(f"DEV_STRAVA_TOKEN_EXPIRES_AT={expires_at}")
    print(f"DEV_STRAVA_ATHLETE_ID={athlete.get('id')}")
    print()
    print("These are dev-only vars for phase 1 scripts. Phase 4 moves per-user")
    print("tokens into the database via the proper /auth/strava/callback flow.")
    print("=" * 72)


if __name__ == "__main__":
    main()
