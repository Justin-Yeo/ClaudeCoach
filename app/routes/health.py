"""GET /health — Render's liveness check.

Liveness only — returns 200 unconditionally. We deliberately do NOT check the
DB or external APIs here so a transient Supabase blip doesn't cause Render to
restart-loop the whole service. See [spec.md §11.12](spec.md).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
