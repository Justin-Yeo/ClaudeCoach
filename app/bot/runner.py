"""Standalone Telegram bot runner.

Runs the bot in long-polling mode for local development.

In phase 5 this is replaced by an asyncio task started from `app/main.py`
inside the FastAPI process so the bot and the webhook server share one
deployment.

Usage:
    uv run python -m app.bot.runner
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram.ext import Application

from app.bot.commands import register_handlers


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    log = logging.getLogger("app.bot.runner")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        raise SystemExit(1)

    log.info("starting bot in long-polling mode (Ctrl+C to stop)")
    app = Application.builder().token(token).build()
    register_handlers(app)
    app.run_polling()


if __name__ == "__main__":
    main()
