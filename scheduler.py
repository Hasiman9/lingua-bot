import asyncio
import logging
import time

from telegram.error import Forbidden, RetryAfter, TelegramError

import config
from materials import send_materials, validate_materials
from subscriptions import show_failure, verify

logger = logging.getLogger(__name__)


class DeliveryService:
    def __init__(self, db, bot, channels, materials_url, campaign_at,
                 message, clock=time.time):
        validate_materials(materials_url, message)
        self.db, self.bot, self.channels = db, bot, channels
        self.materials_url, self.message = materials_url, message
        if campaign_at.tzinfo is None or campaign_at.utcoffset() is None:
            raise ValueError("Campaign datetime must be timezone-aware")
        self.campaign_at, self.clock = campaign_at, clock
        # Serialize confirmation and delivery, including their API calls.
        self.lock = asyncio.Lock()

    async def confirm(self, user_id):
        async with self.lock:
            result = await verify(self.bot, user_id, self.channels)
            if not result.passed:
                await show_failure(self.bot, user_id, result)
                return
            row = await self.db.schedule(user_id, self.clock(), self.campaign_at.timestamp())
            if row["status"] == "delivered":
                text = "Your materials have already been delivered."
            else:
                text = (f"Вижу все подписки, спасибо! 💞\n\n"

"⏰ Ссылку на материалы Бот вышлет 22.10.2026 в 16:00 ⏰\n\n"

"❗️ ВАЖНО ❗️\n "
"Не отменяйте подписки! Иначе, Бот НЕ вышлет материалы!")
            await self.bot.send_message(chat_id=user_id, text=text)

    async def tick(self, context=None):
        async with self.lock:
            if self.clock() < self.campaign_at.timestamp():
                return
            # Limit each batch so confirmations are not indefinitely starved.
            for _ in range(25):
                row = await self.db.claim_due(self.clock())
                if row is None:
                    break
                try:
                    await self.deliver(row)
                except Forbidden:
                    await self.db.update(row["user_id"], status="blocked")
                    logger.info("Delivery blocked for user %s", row["user_id"])
                except (TelegramError, OSError) as exc:
                    await self.retry(row, exc)
                except Exception as exc:
                    # Log type only: exception messages can contain request URLs/tokens.
                    logger.error("Delivery error for %s (%s)", row["user_id"], type(exc).__name__)
                    await self.retry(row, exc)

    async def retry(self, row, error=None, state=None):
        attempts = row["retry_attempts"] + 1
        delay = min(config.RETRY_MAX_SECONDS, config.RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 10))
        if isinstance(error, RetryAfter):
            wait = error.retry_after
            delay = max(delay, wait.total_seconds() if hasattr(wait, "total_seconds") else wait)
        values = dict(status="pending", retry_attempts=attempts, next_attempt_at=self.clock() + delay)
        if state:
            values["verification_state"] = state
        await self.db.update(row["user_id"], **values)
        logger.warning("Delivery retry for user %s in %s seconds", row["user_id"], delay)

    async def deliver(self, row):
        user_id = row["user_id"]
        if not self.materials_url:
            # Keep the original due date, but don't busy-loop or send fake content.
            await self.retry(row)
            return
        # Recheck ALL channels immediately before the single message, including retries.
        result = await verify(self.bot, user_id, self.channels)
        if result.errors:
            await self.retry(row, state="verification_error")
            return
        if result.missing:
            await self.db.update(user_id, status="awaiting_subscription",
                                 verification_state="subscription_missing")
            await show_failure(self.bot, user_id, result)
            return
        await self.db.update(user_id, second_verified_at=self.clock(),
                             verification_state="second_passed")
        await send_materials(self.bot, user_id, self.materials_url, self.message)
        # Commit acknowledgment and completion together. Legacy file progress is not a link receipt.
        await self.db.update(user_id, status="delivered", delivered_at=self.clock(), progress=1)
        logger.info("Materials delivered to user %s", user_id)
