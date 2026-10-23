"""Offline verification and explicit campaign workflow tests."""
import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, NetworkError, RetryAfter

import campaign
import config
from campaigns import CampaignStore, MESSAGE
from database import Database
from scheduler import DeliveryService
from subscriptions import is_member, keyboard, verify


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.db = Database(self.path)
        await self.db.open(100)
        self.store = CampaignStore(self.path)
        self.store.initialize()
        self.now = 1000
        self.statuses = ['member'] * 6
        self.admin = True
        self.failed_channel = None
        self.channels = tuple(config.Channel(f'@channel{i}', f'Channel {i}', f'https://t.me/channel{i}')
                              for i in range(6))
        self.bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=self.membership),
                                   send_message=AsyncMock())
        self.service = DeliveryService(self.db, self.bot, self.channels, '',
                                       datetime.fromtimestamp(100, timezone.utc), '', clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def membership(self, chat_id, user_id):
        index = int(chat_id[-1])
        if index == self.failed_channel:
            raise NetworkError('offline test')
        if user_id == 999:
            return SimpleNamespace(status='administrator' if self.admin else 'member')
        return SimpleNamespace(status=self.statuses[index], is_member=True)

    async def register(self, user_id=42):
        await self.service.confirm(user_id)
        self.bot.send_message.reset_mock()
        self.bot.get_chat_member.reset_mock()

    def activate(self, name='one', at=None):
        return self.store.activate(name, at=at, clock=lambda: self.now)

    async def due(self):
        await self.register()
        self.now = self.activate()

    def recipient(self, user_id=42, name='one'):
        db = self.store.connect(readonly=True)
        try:
            row = db.execute('SELECT * FROM campaign_recipients WHERE campaign_id=? AND user_id=?',
                             (name, user_id)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    async def test_overdue_legacy_campaign_stays_inactive(self):
        await self.register()
        before = await self.db.get(42)
        self.now += 86400
        await self.service.tick()
        self.store.initialize()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.preview()['campaigns'], [])
        self.assertEqual(before, await self.db.get(42))

    async def test_activation_plus_five_minutes_and_exact_single_message(self):
        await self.register()
        self.now += 8000  # Delay between deployment/registration and activation.
        due = self.activate()
        self.assertEqual(due, self.now + 300)
        self.now = due - 1
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now = due
        await asyncio.gather(self.service.tick(), self.service.tick())
        self.bot.send_message.assert_awaited_once_with(
            chat_id=42, text='🎁✨Спасибо за доверие, вот ваши материалы-подарочки:\n\n'
            'https://drive.google.com/drive/folders/1lvzrprek8YgQt42iv7BWS9eSveJUFm-W?usp=sharing',
            parse_mode=None, disable_web_page_preview=True)
        self.assertEqual(self.bot.get_chat_member.await_count, 12)
        self.assertEqual(self.recipient()['status'], 'delivered')
        self.assertEqual(self.store.preview()['campaigns'][0]['status'], 'completed')

    async def test_restart_preserves_countdown_and_activated_overdue_work(self):
        await self.register()
        due = self.activate()
        await self.db.close()
        await self.db.open(999999)
        self.store.initialize()
        self.assertEqual(self.store.preview()['campaigns'][0]['scheduled_at'], due)
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now = due + 86400
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()

    async def test_snapshot_excludes_late_and_failed_verification(self):
        await self.register(42)
        self.statuses[-1] = 'left'
        await self.register(43)
        due = self.activate()
        self.statuses[-1] = 'member'
        await self.register(44)
        self.now = due
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_message.call_args.kwargs['chat_id'], 42)
        self.assertIsNone(self.recipient(43))
        self.assertIsNone(self.recipient(44))

    async def test_missing_membership_skipped_and_campaign_finishes(self):
        await self.due()
        self.statuses[-1] = 'left'
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.bot.get_chat_member.await_count, 12)
        self.assertEqual(self.recipient()['status'], 'subscription_missing')
        self.statuses[-1] = 'member'
        await self.register()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.store.preview()['campaigns'][0]['status'], 'completed')

    async def test_api_errors_retry_with_fresh_checks_and_persistent_backoff(self):
        await self.due()
        self.failed_channel = 3
        await self.service.tick()
        row = self.recipient()
        self.assertEqual(row['retry_attempts'], 1)
        self.assertEqual(row['status'], 'pending')
        self.store.initialize()
        self.assertEqual(row, self.recipient())
        self.failed_channel = None
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.bot.get_chat_member.reset_mock()
        self.now = row['next_attempt_at']
        await self.service.tick()
        self.assertEqual(self.bot.get_chat_member.await_count, 12)
        self.bot.send_message.assert_awaited_once()

    async def test_no_admin_rights_prevents_delivery(self):
        await self.due()
        self.admin = False
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.recipient()['status'], 'pending')

    async def test_ambiguous_send_never_retried_even_in_future_campaign(self):
        await self.due()
        self.bot.send_message.side_effect = NetworkError('response lost')
        await self.service.tick()
        self.assertEqual(self.recipient()['status'], 'uncertain')
        self.store.initialize()
        self.now = self.activate('future')
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()
        self.assertIsNone(self.recipient(name='future'))

    async def test_crash_before_send_is_held(self):
        await self.due()
        row = self.store.claim(self.now)
        self.store.update(row, status='sending')
        self.store.initialize()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.recipient()['status'], 'uncertain')

    async def test_crash_during_verification_can_recover(self):
        await self.due()
        self.store.claim(self.now)
        self.store.initialize()
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()

    async def test_explicit_rate_limit_rejection_retries_and_rechecks(self):
        await self.due()
        self.bot.send_message.side_effect = RetryAfter(120)
        await self.service.tick()
        self.assertEqual(self.recipient()['next_attempt_at'], self.now + 120)
        self.bot.send_message.reset_mock()
        self.bot.send_message.side_effect = None
        self.now += 119
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now += 1
        self.statuses[-1] = 'left'
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()

    async def test_blocked_recipient_is_terminal(self):
        await self.due()
        self.bot.send_message.side_effect = Forbidden('blocked')
        await self.service.tick()
        self.assertEqual(self.recipient()['status'], 'blocked')
        self.assertEqual(self.store.preview()['campaigns'][0]['status'], 'completed')

    async def test_legacy_history_preserved_and_completed_excluded(self):
        for uid in (42, 43, 44, 45):
            await self.db.schedule(uid, 10, 100)
        await self.db.update(43, status='delivered', delivered_at=200, progress=2, manifest='old')
        await self.db.update(44, status='processing', progress=1)
        await self.db.update(45, status='awaiting_subscription')
        before = [await self.db.get(uid) for uid in (42, 43, 44, 45)]
        await self.db.close()
        await self.db.open(9000)
        self.store.initialize()
        self.now = self.activate()
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertIsNone(self.recipient(43))
        self.assertIsNone(self.recipient(44))
        self.assertEqual(before, [await self.db.get(uid) for uid in (42, 43, 44, 45)])

    async def test_reactivation_refused_and_new_future_date_allowed(self):
        await self.due()
        with self.assertRaises(ValueError):
            self.activate()
        with self.assertRaises(ValueError):
            self.activate('two')
        await self.service.tick()
        original = self.store.preview()['campaigns'][0]
        with self.assertRaises(ValueError):
            self.activate()
        with self.assertRaises(ValueError):
            self.activate('two', at=self.now)
        self.activate('two', at=self.now + 86400)
        self.assertEqual(original, self.store.preview()['campaigns'][0])
        self.assertIsNone(self.recipient(name='two'))

    async def test_duplicate_confirmations_preserve_history(self):
        await self.register()
        before = await self.db.get(42)
        self.now += 500
        await asyncio.gather(*(self.service.confirm(42) for _ in range(4)))
        self.assertEqual(before, await self.db.get(42))

    async def test_preview_is_read_only_before_and_after_activation(self):
        await self.register()
        self.assertEqual(self.store.preview()['eligible'], 1)
        self.assertEqual(self.store.preview()['campaigns'], [])
        self.activate()
        before = self.recipient()
        result = self.store.preview()
        self.assertEqual(result['deliveries'], [{'campaign_id': 'one', 'status': 'pending', 'count': 1}])
        self.assertEqual(before, self.recipient())
        self.bot.send_message.assert_not_awaited()

    async def test_cli_preview_and_explicit_activation(self):
        await self.register()
        with (patch('campaign.load_dotenv'), patch.dict('os.environ', {'DATABASE_PATH': str(self.path)}),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO())):
            campaign.main(['preview'])
            self.assertIn('Not activated', output.getvalue())
            with self.assertRaises(SystemExit):
                campaign.main(['activate'])
            self.assertEqual(self.store.preview()['campaigns'], [])
            campaign.main(['activate', '--confirm'])
            saved = self.store.preview()['campaigns'][0]
            self.assertEqual(saved['scheduled_at'] - saved['activated_at'], 300)
            with self.assertRaises(SystemExit):
                campaign.main(['activate', '--confirm'])
            self.assertEqual(saved, self.store.preview()['campaigns'][0])
        self.bot.send_message.assert_not_awaited()

    async def test_all_membership_statuses_and_initial_failure(self):
        self.statuses = ['member', 'administrator', 'creator', 'owner', 'restricted', 'member']
        self.assertTrue((await verify(self.bot, 42, self.channels)).passed)
        self.failed_channel = 2
        await self.register()
        self.assertIsNone(await self.db.get(42))
        for status in ('left', 'kicked', 'unknown'):
            self.assertFalse(is_member(SimpleNamespace(status=status)))
        self.assertFalse(is_member(SimpleNamespace(status='restricted', is_member=False)))
        self.assertEqual(len(keyboard(self.channels).inline_keyboard), 7)


class ConfigurationTests(unittest.TestCase):
    def test_utc_and_timezone_validation(self):
        value = config.parse_campaign('2026-10-22', '16:00', 'Asia/Krasnoyarsk')
        self.assertEqual(value.astimezone(timezone.utc), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
        for date, time, zone in [('2026-02-30', '10:00', 'UTC'),
                                 ('2026-03-29', '02:30', 'Europe/Berlin'),
                                 ('2026-10-25', '02:30', 'Europe/Berlin')]:
            with self.assertRaises(ValueError):
                config.parse_campaign(date, time, zone)
        self.assertEqual(campaign.utc_time('2026-10-22T09:00:00Z'), value.timestamp())
        import argparse
        for text in ('2026-12-01T10:00:00', 'bad', '2026-12-01T10:00:00+07:00'):
            with self.assertRaises(argparse.ArgumentTypeError):
                campaign.utc_time(text)

    def test_six_distinct_channels_required(self):
        config.validate_channels(config.CHANNELS)
        with self.assertRaises(ValueError):
            config.validate_channels(config.CHANNELS[:5])
