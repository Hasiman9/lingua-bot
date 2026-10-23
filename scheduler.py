import asyncio
import logging
import time

from telegram.error import Forbidden, RetryAfter

import config
from campaigns import CampaignStore
from subscriptions import show_failure, verify

logger = logging.getLogger(__name__)


class DeliveryService:
    def __init__(self, db, bot, channels, materials_url, campaign_at,
                 message, clock=time.time):
        self.db, self.bot, self.channels = db, bot, channels
        self.campaign_at, self.clock = campaign_at, clock
        self.store = CampaignStore(db.path)
        self.lock = asyncio.Lock()

    async def confirm(self, user_id):
        async with self.lock:
            result = await verify(self.bot, user_id, self.channels)
            if not result.passed:
                await show_failure(self.bot, user_id, result)
                return
            await self.db.schedule(user_id, self.clock(), self.campaign_at.timestamp())
            await self.bot.send_message(chat_id=user_id, text=(
                'Вижу все подписки, спасибо! 💞\n\n'
                'Не отменяйте подписки! Перед рассылкой бот проверит их снова.'))

    async def tick(self, context=None):
        async with self.lock:
            for _ in range(25):
                row = await asyncio.to_thread(self.store.claim, self.clock())
                if row is None:
                    break
                await self.deliver(row)

    async def update(self, row, **values):
        await asyncio.to_thread(self.store.update, row, **values)

    async def retry(self, row, error=None):
        attempts = row['retry_attempts'] + 1
        delay = min(config.RETRY_MAX_SECONDS, config.RETRY_BASE_SECONDS * 2 ** min(attempts - 1, 10))
        if isinstance(error, RetryAfter):
            wait = error.retry_after
            delay = max(delay, wait.total_seconds() if hasattr(wait, 'total_seconds') else wait)
        await self.update(row, status='pending', retry_attempts=attempts,
                          next_attempt_at=self.clock() + delay)

    async def deliver(self, row):
        try:
            result = await verify(self.bot, row['user_id'], self.channels)
        except Exception:
            await self.retry(row)
            return
        if result.errors:
            await self.retry(row)
            return
        if result.missing:
            await self.update(row, status='subscription_missing')
            return
        # Persist before the API call. A crash here is held, never automatically resent.
        await self.update(row, status='sending', second_verified_at=self.clock())
        try:
            await self.bot.send_message(chat_id=row['user_id'], text=row['message'],
                                        parse_mode=None, disable_web_page_preview=True)
        except RetryAfter as exc:
            await self.retry(row, exc)  # Explicit rejection: Telegram did not send.
        except Forbidden:
            await self.update(row, status='blocked')
        except Exception as exc:
            await self.update(row, status='uncertain')
            logger.error('Send held for review for user %s (%s)', row['user_id'], type(exc).__name__)
        else:
            await self.update(row, status='delivered', delivered_at=self.clock())
