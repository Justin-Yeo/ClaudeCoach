"""Async Strava API client with automatic token refresh.

Phase 1 usage — pass tokens directly from .env:

    async with StravaClient(
        client_id=os.environ["STRAVA_CLIENT_ID"],
        client_secret=os.environ["STRAVA_CLIENT_SECRET"],
        access_token=os.environ["DEV_STRAVA_ACCESS_TOKEN"],
        refresh_token=os.environ["DEV_STRAVA_REFRESH_TOKEN"],
    ) as strava:
        activity = await strava.get_activity(12345678)
        streams = await strava.get_activity_streams(12345678)

Phase 4 usage — construct from a `User` ORM row and pass an `on_refresh`
callback that writes the new tokens back to the DB.

On 401, the client refreshes the access token once and retries the request.
If the refresh itself returns 401 (revoked app or expired refresh token),
`StravaAuthError` is raised and the caller should mark the user as disconnected
and DM them a reconnect link (see spec.md §3.4 and §11 admin bootstrap).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"

# The streams we always request — see METRICS.md §1 and schema.md §4.
DEFAULT_STREAM_KEYS: list[str] = [
    "time",
    "distance",
    "heartrate",
    "velocity_smooth",
    "altitude",
    "cadence",
    "grade_smooth",
]


class StravaError(Exception):
    """Base class for Strava API errors."""


class StravaAPIError(StravaError):
    """Non-auth failure — 4xx (not 401), 5xx, or network error."""


class StravaAuthError(StravaError):
    """Authentication failure — refresh token expired or app revoked.

    Caller should mark the user as disconnected and prompt re-auth.
    """


class StravaClient:
    """Async Strava API client with automatic access-token refresh.

    Parameters
    ----------
    client_id, client_secret
        Strava OAuth app credentials from https://www.strava.com/settings/api.
    access_token, refresh_token
        Per-user OAuth tokens obtained via the OAuth dance.
    on_refresh
        Optional callback invoked with the full refresh response dict whenever
        the access token is rotated. Typical phase-4 implementation writes the
        new `access_token`, `refresh_token`, and `expires_at` back into the
        `users` table. Phase-1 callers can pass `None` or `print`.
    timeout
        HTTP timeout in seconds. Defaults to 30.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        on_refresh: Callable[[dict], None] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.on_refresh = on_refresh
        self._http = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> StravaClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ API

    async def get_activity(self, activity_id: int) -> dict:
        """Fetch a single activity's summary stats from `GET /activities/{id}`."""
        return await self._authed_get(f"{STRAVA_API_BASE}/activities/{activity_id}")

    async def get_activity_streams(
        self,
        activity_id: int,
        keys: list[str] | None = None,
    ) -> dict:
        """Fetch raw stream arrays for an activity.

        Returns a dict keyed by stream type, e.g.:
            {
                "time":      {"data": [0, 1, 2, ...], ...},
                "distance":  {"data": [0.0, 1.2, 2.5, ...], ...},
                "heartrate": {"data": [120, 121, 122, ...], ...},
                ...
            }

        Not every stream is always present — `heartrate` and `cadence` may be
        absent depending on the recording device. The metrics layer checks for
        presence before computing derived values (see METRICS.md §1).
        """
        keys_str = ",".join(keys or DEFAULT_STREAM_KEYS)
        return await self._authed_get(
            f"{STRAVA_API_BASE}/activities/{activity_id}/streams",
            params={"keys": keys_str, "key_by_type": "true"},
        )

    # ----------------------------------------------------------------- internals

    async def _authed_get(self, url: str, params: dict | None = None) -> dict:
        """GET with auth; on 401 refresh once and retry."""
        headers = {"Authorization": f"Bearer {self.access_token}"}

        try:
            response = await self._http.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise StravaAPIError(f"Network error calling {url}: {exc}") from exc

        if response.status_code == 401:
            # Token expired or revoked — try refresh once, then retry
            await self._refresh_tokens()
            headers["Authorization"] = f"Bearer {self.access_token}"
            try:
                response = await self._http.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise StravaAPIError(f"Network error calling {url}: {exc}") from exc

            if response.status_code == 401:
                raise StravaAuthError(
                    "Still 401 after refresh — refresh token is invalid or revoked."
                )

        if response.status_code != 200:
            raise StravaAPIError(
                f"GET {url} returned {response.status_code}: {response.text[:400]}"
            )

        return response.json()

    async def _refresh_tokens(self) -> None:
        """Exchange the refresh token for a new access token.

        On success, updates `self.access_token` + `self.refresh_token` in place
        and invokes `self.on_refresh` with the full response dict so the caller
        can persist the new tokens.

        On failure raises `StravaAuthError`.
        """
        try:
            response = await self._http.post(
                STRAVA_TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
        except httpx.HTTPError as exc:
            raise StravaAuthError(f"Network error during token refresh: {exc}") from exc

        if response.status_code != 200:
            raise StravaAuthError(
                f"Token refresh failed ({response.status_code}): {response.text[:400]}"
            )

        data = response.json()
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]

        if self.on_refresh is not None:
            self.on_refresh(data)
