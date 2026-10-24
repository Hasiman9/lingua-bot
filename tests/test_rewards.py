"""Temporary databases and mocked Telegram only: no live rewards or campaigns."""
import asyncio
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError, RetryAfter

import config
import reward
from campaigns import CampaignStore, MESSAGE
from database import Database
from rewards import DURATION_SECONDS, RewardStore
from scheduler import DeliveryService


class RewardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.db = Database(self.path)
        await self.db.open(100)
        self.campaigns = CampaignStore(self.path)
        self.campaigns.initialize()
        self.rewards = RewardStore(self.path)
        self.now = 1000
        self.bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status='administrator')), send_message=AsyncMock())
        self.service = DeliveryService(self.db, self.bot, config.CHANNELS, '',
                                       datetime.fromtimestamp(100, timezone.utc), '',
                                       clock=lambda: self.now, sleep=AsyncMock())

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def activate(self):
        return self.rewards.activate(clock=lambda: self.now)

    def sends(self):
        return [c for c in self.bot.send_message.call_args_list if c.kwargs.get('text') == MESSAGE]

    def row(self, uid=42):
        with closing(self.rewards.connect(readonly=True)) as db:
            row = db.execute('SELECT * FROM reward_deliveries WHERE user_id=?', (uid,)).fetchone()
            return dict(row) if row else None

    def campaign_history(self):
        with closing(self.campaigns.connect(readonly=True)) as db:
            return ([tuple(r) for r in db.execute('SELECT * FROM campaigns')],
                    [tuple(r) for r in db.execute('SELECT * FROM campaign_recipients')])

    async def test_inactive_feature_only_verifies_and_registers(self):
        with self.assertLogs('scheduler', level='INFO') as logs:
            await self.service.confirm(42)
        self.assertIsNotNone(await self.db.get(42))
        self.assertIsNone(self.row())
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.rewards.preview()['status'], 'inactive')
        self.assertIn('reason=window_inactive', '\n'.join(logs.output))
        self.bot.send_message.assert_awaited_once_with(chat_id=42, text='Вижу все подписки, спасибо! 💞')

    async def test_first_verification_sends_exact_message_without_another_click(self):
        self.activate()
        with patch.object(self.service, 'tick', side_effect=AssertionError('Must not wait for scheduler')):
            await self.service.confirm(42)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.sends()[0].kwargs, dict(chat_id=42, text=MESSAGE,
                         parse_mode=None, disable_web_page_preview=True))
        self.assertEqual(self.row()['status'], 'delivered')
        self.assertEqual(self.bot.get_chat_member.await_count, 10)
        self.assertEqual(self.campaign_history(), ([], []))

    async def test_delivered_only_after_telegram_accepts_the_inline_send(self):
        self.activate()
        started, accepted = asyncio.Event(), asyncio.Event()
        async def send(**kwargs):
            self.assertEqual(kwargs['text'], MESSAGE)
            started.set()
            await accepted.wait()
        self.bot.send_message.side_effect = send
        with self.assertLogs('scheduler', level='INFO') as logs:
            task = asyncio.create_task(self.service.confirm(42))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                self.assertEqual(self.row()['status'], 'sending')
                self.assertIsNone(self.row()['delivered_at'])
                self.assertNotIn('delivery acknowledged', '\n'.join(logs.output))
            finally:
                accepted.set()
                await task
        self.assertEqual(self.row()['status'], 'delivered')
        self.assertIn('delivery acknowledged', '\n'.join(logs.output))

    async def test_inline_network_failure_is_uncertain_not_success_and_not_resent(self):
        self.activate()
        self.bot.send_message.side_effect = NetworkError('response lost')
        with self.assertLogs('scheduler', level='INFO') as logs:
            await self.service.confirm(42)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.row()['status'], 'uncertain')
        self.assertIsNone(self.row()['delivered_at'])
        self.assertNotIn('delivery acknowledged', '\n'.join(logs.output))
        self.bot.send_message.side_effect = None
        await self.service.confirm(42)
        await self.service.tick()
        self.assertEqual(len(self.sends()), 1)

    async def test_inline_rate_limit_failure_is_pending_not_delivered(self):
        self.activate()
        self.bot.send_message.side_effect = RetryAfter(60)
        await self.service.confirm(42)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.row()['status'], 'pending')
        self.assertIsNone(self.row()['delivered_at'])
        self.bot.send_message.side_effect = None
        with self.assertLogs('scheduler', level='INFO') as logs:
            await self.service.confirm(42)
        self.assertIn('reason=telegram_cooldown', '\n'.join(logs.output))
        self.assertEqual(len(self.sends()), 1)
        self.now += 60
        await self.service.tick()
        self.assertEqual(self.row()['status'], 'delivered')

    async def test_duplicate_callback_logs_already_rewarded_without_resending(self):
        self.activate()
        await self.service.confirm(42)
        with self.assertLogs('scheduler', level='INFO') as logs:
            await self.service.confirm(42)
        self.assertIn('reason=reward_delivered', '\n'.join(logs.output))
        self.assertEqual(len(self.sends()), 1)

    async def test_user_verified_before_activation_does_not_silently_become_new(self):
        await self.service.confirm(42)
        self.activate()
        with self.assertLogs('scheduler', level='INFO') as logs:
            await self.service.confirm(42)
        self.assertIn('reason=not_new_during_window', '\n'.join(logs.output))
        self.assertEqual(self.sends(), [])

    async def test_existing_users_never_enroll_even_with_new_confirmation(self):
        await self.db.schedule(42, 1, 100)
        before = await self.db.get(42)
        self.activate()
        await self.service.confirm(42)
        await self.service.tick()
        self.assertEqual(self.sends(), [])
        self.assertIsNone(self.row())
        self.assertEqual(before, await self.db.get(42))

    async def test_exact_deadline_stops_rewards_but_verification_still_succeeds(self):
        expiry = self.activate()
        self.assertEqual(expiry - self.now, 72 * 3600)
        self.now = expiry - 1
        await self.service.confirm(42)
        self.assertEqual(len(self.sends()), 1)
        for uid, now in ((43, expiry), (44, expiry + 1)):
            self.now = now
            with self.assertLogs('scheduler', level='INFO') as logs:
                await self.service.confirm(uid)
            self.assertIn('reason=window_expired', '\n'.join(logs.output))
            self.assertIsNotNone(await self.db.get(uid))
            self.assertIsNone(self.row(uid))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.rewards.preview(clock=lambda: self.now)['status'], 'expired')

    async def test_deadline_crossed_during_final_checks_withholds_send(self):
        expiry = self.activate()
        self.rewards.register_verified(42, self.now, 100)
        row = self.rewards.claim(self.now)
        self.now = expiry - 1
        async def membership(**kwargs):
            self.now = expiry
            return SimpleNamespace(status='administrator')
        self.bot.get_chat_member.side_effect = membership
        await self.service.deliver(row)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.row()['status'], 'expired')

    async def test_restart_retains_window_and_recovers_pending_without_click(self):
        expiry = self.activate()
        self.rewards.register_verified(42, self.now, 100)
        self.rewards.claim(self.now)  # interrupted pre-send work
        await self.db.close()
        await self.db.open(99999)
        self.campaigns.initialize()
        self.now += 3600
        self.assertEqual(self.rewards.preview(clock=lambda: self.now)['expires_at'], expiry)
        await self.service.tick()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.row()['status'], 'delivered')

    async def test_deadline_crossed_after_reservation_still_prevents_api_send(self):
        expiry = self.activate()
        self.rewards.register_verified(42, self.now, 100)
        row = self.rewards.claim(self.now)
        reserve = self.service.rewards.begin_send
        def reserve_then_expire(*args, **kwargs):
            result = reserve(*args, **kwargs)
            self.now = expiry
            return result
        with patch.object(self.service.rewards, 'begin_send', side_effect=reserve_then_expire):
            await self.service.deliver(row)
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.row()['status'], 'expired')

    async def test_duplicate_callbacks_and_concurrent_enrollment_are_once_only(self):
        self.activate()
        await asyncio.gather(*(self.service.confirm(42) for _ in range(5)), self.service.tick())
        self.assertEqual(len(self.sends()), 1)
        inserted = await asyncio.gather(*(asyncio.to_thread(
            self.rewards.register_verified, 43, self.now, 100) for _ in range(5)))
        self.assertEqual(inserted.count(True), 1)
        claims = await asyncio.gather(*(asyncio.to_thread(self.rewards.claim, self.now, 43) for _ in range(5)))
        self.assertEqual(sum(row is not None for row in claims), 1)

    async def test_reward_activation_and_delivery_preserve_current_campaign_snapshot(self):
        await self.db.schedule(1, 1, 100)
        self.campaigns.activate('materials-immediate-v1', clock=lambda: self.now)
        history = self.campaign_history()
        subscriber = await self.db.get(1)
        self.activate()
        await self.service.confirm(42)
        self.assertEqual(history, self.campaign_history())
        self.assertEqual(subscriber, await self.db.get(1))
        self.assertEqual(len(self.sends()), 1)
        await self.service.tick()
        self.assertEqual({c.kwargs['chat_id'] for c in self.sends()}, {1, 42})

    async def test_campaign_receipts_block_reward_and_reward_receipts_block_future_campaigns(self):
        self.activate()
        # A receipt can outlive a subscriber row; the receipt itself must protect deduplication.
        with closing(self.campaigns.connect()) as db, db:
            db.execute("INSERT INTO campaign_recipients (campaign_id,user_id,status,next_attempt_at,delivered_at) "
                       "VALUES ('materials-immediate-v1',42,'delivered',0,1)")
        await self.service.confirm(42)
        self.assertEqual(self.sends(), [])
        self.assertIsNone(self.row())
        await self.service.confirm(43)
        self.campaigns.activate('future', clock=lambda: self.now)
        self.assertEqual(self.campaigns.preview()['eligible'], 0)
        self.assertEqual(self.campaigns.preview()['previously_delivered'], 2)
        await self.service.tick()
        self.assertEqual(len(self.sends()), 1)

    async def test_campaign_and_reward_concurrent_sends_use_atomic_shared_guard(self):
        self.activate()
        self.rewards.register_verified(42, self.now, 100)
        self.campaigns.activate('future', clock=lambda: self.now)
        campaign_row = self.campaigns.claim(self.now)
        reward_row = self.rewards.claim(self.now)
        await asyncio.gather(self.service.deliver(campaign_row), self.service.deliver(reward_row))
        self.assertEqual(len(self.sends()), 1)
        await self.service.tick()
        self.assertEqual(len(self.sends()), 1)

    async def test_uncertain_send_is_never_retried_after_restart(self):
        self.activate()
        self.rewards.register_verified(42, self.now, 100)
        row = self.rewards.claim(self.now)
        self.rewards.begin_send(row, self.now)  # crash around send
        self.campaigns.initialize()
        self.assertEqual(self.row()['status'], 'uncertain')
        await self.service.confirm(42)
        self.campaigns.activate('future', clock=lambda: self.now)
        await self.service.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.campaigns.preview()['eligible'], 0)

    async def test_rate_limit_retry_expires_without_sending_after_deadline(self):
        expiry = self.activate()
        self.rewards.register_verified(42, self.now, 100)
        self.now = expiry - 10
        self.bot.send_message.side_effect = RetryAfter(60)
        await self.service.tick()
        self.assertEqual(self.row()['status'], 'pending')
        self.bot.send_message.reset_mock()
        self.bot.send_message.side_effect = None
        self.now = expiry + 60
        self.campaigns.initialize()
        await self.service.tick()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.row()['status'], 'expired')

    async def test_verification_failure_withholds_reward_and_recovers_automatically(self):
        self.activate()
        self.rewards.register_verified(42, self.now, 100)
        self.bot.get_chat_member.side_effect = NetworkError('offline')
        await self.service.tick()
        self.assertEqual(self.sends(), [])
        self.now = self.row()['next_attempt_at']
        self.bot.get_chat_member.side_effect = None
        await self.service.tick()
        self.assertEqual(len(self.sends()), 1)

    async def test_activation_cannot_reset_active_or_expired_window(self):
        expiry = self.activate()
        before = self.rewards.preview(clock=lambda: self.now)
        with self.assertRaises(ValueError):
            self.activate()
        self.assertEqual(before, self.rewards.preview(clock=lambda: self.now))
        self.now = expiry
        with self.assertRaises(ValueError):
            self.activate()
        self.assertEqual(self.rewards.preview(clock=lambda: self.now)['expires_at'], expiry)

    async def test_cli_preview_status_and_explicit_activation_are_offline(self):
        with (patch('reward.load_dotenv'), patch.dict('os.environ', {'DATABASE_PATH': str(self.path)}),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO())):
            reward.main(['preview'])
            reward.main(['status'])
            self.assertIn('inactive', output.getvalue())
            with self.assertRaises(SystemExit):
                reward.main(['activate'])
            self.assertEqual(self.rewards.preview()['status'], 'inactive')
            reward.main(['activate', '--confirm'])
            result = self.rewards.preview()
            self.assertEqual(result['expires_at'] - result['activated_at'], DURATION_SECONDS)
            before = self.rewards.preview()
            reward.main(['preview'])
            reward.main(['status'])
            self.assertEqual(before['expires_at'], self.rewards.preview()['expires_at'])
        self.bot.send_message.assert_not_awaited()
