"""Read-only Telegram diagnostics: getMe and six bot getChatMember checks only."""
import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

import config
from safe_logging import RedactingFormatter
from subscriptions import api_error_details, check_membership

logger = logging.getLogger(__name__)


async def diagnose(bot, channels):
    config.validate_channels(channels)
    failed = 0
    for channel in channels:
        result = await check_membership(bot, channel, bot.id, "bot")
        failed += result.category != "ok"
    logger.info("Channel diagnostic complete: checked=%d administrator_ok=%d failed=%d",
                len(channels), len(channels) - failed, failed)
    return 1 if failed else 0


async def run(token):
    try:
        # Bot context initializes with getMe; it does not poll or touch the webhook.
        async with Bot(token=token) as bot:
            logger.info("Authenticated bot_id=%s username=@%s", bot.id, bot.username)
            return await diagnose(bot, config.CHANNELS)
    except TelegramError as exc:
        category, reason = api_error_details(exc)
        logger.error("Diagnostic failed before completion category=%s exception=%s reason=%s",
                     category, type(exc).__name__, reason)
        return 2


def main():
    load_dotenv(config.BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    logging.basicConfig(level=logging.INFO)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    formatter.token = token
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Transport messages can contain URLs; diagnostics emit only allowlisted API details.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    if not token:
        logger.error("Set BOT_TOKEN in the project .env or process environment")
        return 2
    try:
        config.validate_channels(config.CHANNELS)
    except ValueError:
        logger.error("Invalid channel configuration: require six distinct @usernames or negative numeric IDs and join links")
        return 2
    return asyncio.run(run(token))


if __name__ == "__main__":
    raise SystemExit(main())
