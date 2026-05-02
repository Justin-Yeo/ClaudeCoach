"""FastAPI entrypoint.

Single deployment: FastAPI serves `/health`, `/webhook`, and
`/auth/strava/callback`, while the Telegram bot's long-polling updater runs
as an asyncio task in the same process. See [spec.md §11.1](spec.md).

Run locally:
    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Run in production (Render):
    alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from telegram.ext import Application

# Load .env into os.environ before anything else imports settings. Pydantic
# Settings populates its own object from .env but does NOT export to os.environ,
# so SDKs that read os.environ directly (e.g. anthropic) wouldn't see the values
# without this. Belt-and-suspenders alongside the explicit api_key wiring in
# `app/services/claude.py`.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.bot.commands import register_handlers  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.routes import auth, health, webhook  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the Telegram bot's long-polling updater on app startup, stop it on shutdown.

    The bot runs in the same asyncio event loop as FastAPI's request handlers,
    sharing one Python process. The bot's `Bot` instance is exposed via
    `app.state.bot` so the webhook handler can dispatch outgoing messages.
    """
    settings = get_settings()

    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        level=settings.LOG_LEVEL,
    )
    log = logging.getLogger("app.main")

    bot_app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    register_handlers(bot_app)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    app.state.bot_app = bot_app
    app.state.bot = bot_app.bot

    log.info("ClaudeCoach started — bot polling, FastAPI serving %s", settings.APP_BASE_URL)

    try:
        yield
    finally:
        log.info("ClaudeCoach shutting down")
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()


app = FastAPI(
    title="ClaudeCoach",
    description="AI-powered running coach. Webhook backend + Telegram bot.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(auth.router)
