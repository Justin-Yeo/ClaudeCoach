"""Register the Strava webhook subscription.

Run once per environment after deployment, or whenever `APP_BASE_URL` changes
(which it does every time you restart ngrok during local dev).

Strava allows ONE `push_subscription` per app. This script:
  1. Lists existing subscriptions
  2. If one already points at the correct URL → no-op, exit
  3. Otherwise deletes any existing subscriptions
  4. Creates a new subscription pointing at `${APP_BASE_URL}/webhook`

Step 4 makes Strava immediately GET the callback URL with `hub.challenge`
and `hub.verify_token` query params — your server's `/webhook` GET handler
must be reachable and respond correctly. Make sure uvicorn is running
BEFORE running this script.

Usage:
    uv run python scripts/register_webhook.py
    uv run python scripts/register_webhook.py --delete-only
    uv run python scripts/register_webhook.py --list

Prerequisites in .env:
    STRAVA_CLIENT_ID
    STRAVA_CLIENT_SECRET
    STRAVA_WEBHOOK_VERIFY_TOKEN
    APP_BASE_URL  (must be HTTPS — Strava rejects http:// callback URLs)
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.config import get_settings

STRAVA_SUBSCRIPTIONS_URL = "https://www.strava.com/api/v3/push_subscriptions"


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    settings = get_settings()

    if not settings.STRAVA_WEBHOOK_VERIFY_TOKEN:
        print("ERROR: STRAVA_WEBHOOK_VERIFY_TOKEN is empty in .env.")
        print()
        print("Generate one with:")
        print('  uv run python -c "import secrets; print(secrets.token_urlsafe(24))"')
        print()
        print("Then save it as STRAVA_WEBHOOK_VERIFY_TOKEN in your .env file.")
        sys.exit(1)

    if not settings.APP_BASE_URL.startswith("https://"):
        print(f"ERROR: APP_BASE_URL must be HTTPS, got: {settings.APP_BASE_URL}")
        print("Strava rejects http:// callback URLs.")
        sys.exit(1)

    callback_url = f"{settings.APP_BASE_URL.rstrip('/')}/webhook"
    list_only = "--list" in sys.argv
    delete_only = "--delete-only" in sys.argv

    auth = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "client_secret": settings.STRAVA_CLIENT_SECRET,
    }

    print(f"Target callback URL: {callback_url}")
    print()

    with httpx.Client(timeout=30.0) as client:
        # Step 1: list existing
        response = client.get(STRAVA_SUBSCRIPTIONS_URL, params=auth)
        if response.status_code != 200:
            _bail("listing subscriptions", response)
        existing = response.json()

        print(f"Existing subscriptions: {len(existing)}")
        for sub in existing:
            print(f"  id={sub['id']}  url={sub.get('callback_url')}")
        print()

        if list_only:
            return

        # Step 2: short-circuit if already correct
        if not delete_only and any(sub.get("callback_url") == callback_url for sub in existing):
            print(f"✓ Subscription already points to {callback_url}. Nothing to do.")
            return

        # Step 3: delete any others
        for sub in existing:
            print(f"Deleting subscription {sub['id']}...")
            del_response = client.delete(
                f"{STRAVA_SUBSCRIPTIONS_URL}/{sub['id']}",
                params=auth,
            )
            if del_response.status_code not in (200, 204):
                _bail("deleting subscription", del_response)
            print("  deleted.")

        if delete_only:
            print()
            print("✓ All subscriptions deleted (--delete-only).")
            return

        # Step 4: create new — this triggers Strava's GET to /webhook
        print()
        print(f"Creating new subscription → {callback_url}")
        print("(Strava will now GET your webhook URL to verify it's reachable.)")
        create_response = client.post(
            STRAVA_SUBSCRIPTIONS_URL,
            data={
                **auth,
                "callback_url": callback_url,
                "verify_token": settings.STRAVA_WEBHOOK_VERIFY_TOKEN,
            },
        )
        if create_response.status_code != 201:
            _bail("creating subscription", create_response)
        new_sub = create_response.json()

        print()
        print(f"✓ Subscription created: id={new_sub['id']}")
        print()
        print("Your server will now receive a webhook event each time you (or any")
        print("user with a row in the `users` table) uploads a Run to Strava.")


def _bail(action: str, response: httpx.Response) -> None:
    print(f"ERROR {action}: HTTP {response.status_code}")
    print(response.text)
    sys.exit(1)


if __name__ == "__main__":
    main()
