"""Offline verification and explicit campaign workflow tests."""
import asyncio
import io
from contextlib import closing
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
        self.statuses = ['member'] * 5
        self.admin = True
        self.failed_channel = None
        self.channels = tuple(config.Channel(f'@channel{i}', f'Channel {i}', f'https://t.me/channel{i}')
                              for i in range(5))
        self.bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=self.membership),
                                   send_message=AsyncMock())
        self.service = DeliveryService(self.db, self.bot, self.channels, '',
                                       datetime.fromtimestamp(100, timezone.utc), '', clock=lambda: self.now,
                                       sleep=AsyncMock())

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

    async def test_immediate_activation_and_exact_single_message(self):
        await self.register()
        self.now += 8000  # Delay between deployment/registration and activation.
        due = self.activate()
        self.assertEqual(due, self.now)
        self.now = due - 1
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now = due
        await asyncio.gather(self.service.tick(), self.service.tick())
        self.bot.send_message.assert_awaited_once_with(
            chat_id=42, text='🎁✨Спасибо за доверие, вот ваши материалы-подарочки:\n\n'
            'https://drive.google.com/drive/folders/1lvzrprek8YgQt42iv7BWS9eSveJUFm-W?usp=sharing',
            parse_mode=None, disable_web_page_preview=True)
        self.assertEqual(self.bot.get_chat_member.await_count, 10)
        self.assertEqual(self.recipient()['status'], 'delivered')
        self.assertEqual(self.store.preview()['campaigns'][0]['status'], 'completed')

    async def test_restart_preserves_explicit_future_date_and_overdue_work(self):
        await self.register()
        due = self.activate(at=self.now + 3600)
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
        self.assertEqual(self.bot.get_chat_member.await_count, 10)
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
        self.assertEqual(self.bot.get_chat_member.await_count, 10)
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
            campaign.main(['activate', '--immediate', '--replace-active', '--confirm'])
            saved = self.store.preview()['campaigns'][0]
            self.assertEqual(saved['scheduled_at'], saved['activated_at'])
            with self.assertRaises(SystemExit):
                campaign.main(['activate', '--confirm'])
            self.assertEqual(saved, self.store.preview()['campaigns'][0])
        self.bot.send_message.assert_not_awaited()

    async def test_all_membership_statuses_and_initial_failure(self):
        self.statuses = ['member', 'administrator', 'creator', 'owner', 'restricted']
        self.assertTrue((await verify(self.bot, 42, self.channels)).passed)
        self.failed_channel = 2
        await self.register()
        self.assertIsNone(await self.db.get(42))
        for status in ('left', 'kicked', 'unknown'):
            self.assertFalse(is_member(SimpleNamespace(status=status)))
        self.assertFalse(is_member(SimpleNamespace(status='restricted', is_member=False)))
        self.assertEqual(len(keyboard(self.channels).inline_keyboard), 6)

    async def test_59_legacy_users_require_explicit_replacement_then_current_five_checks(self):
        # Simulate the actual pre-upgrade schema in a temporary DB, not production.
        for uid in range(1, 60):
            await self.db.schedule(uid, 10, 20)
        original_users = [await self.db.get(uid) for uid in range(1, 60)]
        with closing(self.store.connect()) as db, db:
            db.execute('DROP TABLE campaigns')
            db.execute('CREATE TABLE campaigns (id TEXT PRIMARY KEY, activated_at REAL NOT NULL, '
                       'scheduled_at REAL NOT NULL, status TEXT NOT NULL, completed_at REAL, message TEXT NOT NULL)')
            db.execute("INSERT INTO campaigns VALUES ('old', 10, 20, 'active', NULL, ?)", (MESSAGE,))
            db.execute("INSERT INTO campaigns VALUES ('finished', 1, 2, 'completed', 3, ?)", (MESSAGE,))
            db.executemany("INSERT INTO campaign_recipients (campaign_id,user_id,status,next_attempt_at) "
                           "VALUES ('old', ?, 'pending', 20)", [(uid,) for uid in range(1, 60)])
        self.assertEqual(self.store.preview()['eligible'], 59)
        self.store.initialize()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        preview = self.store.preview()
        self.assertTrue(next(c for c in preview['campaigns'] if c['id'] == 'old')['requires_explicit_replacement'])
        with self.assertRaises(ValueError):
            self.activate()
        due = self.store.activate('one', clock=lambda: self.now, replace_active=True)
        self.assertEqual(due, self.now)
        self.assertEqual(self.store.preview()['deliveries'], [
            {'campaign_id': 'old', 'status': 'pending', 'count': 59},
            {'campaign_id': 'one', 'status': 'pending', 'count': 59},
        ])
        self.service.channels = config.CHANNELS
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status='administrator')
        for _ in range(3):
            await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 59)
        self.assertEqual(len({c.kwargs['chat_id'] for c in self.bot.send_message.call_args_list}), 59)
        self.assertEqual(self.bot.get_chat_member.await_count, 59 * 10)
        self.assertEqual({c.kwargs['chat_id'] for c in self.bot.get_chat_member.call_args_list},
                         {c.chat_id for c in config.CHANNELS})
        self.assertEqual(self.service.sleep.await_count, 59)
        self.assertTrue(all(c.args == (1.0,) for c in self.service.sleep.call_args_list))
        self.assertEqual(original_users, [await self.db.get(uid) for uid in range(1, 60)])
        self.assertEqual(self.store.preview()['previously_delivered'], 59)
        self.assertEqual(self.store.preview()['eligible'], 0)
        with closing(self.store.connect(readonly=True)) as db:
            self.assertEqual(tuple(db.execute("SELECT status,scheduled_at FROM campaigns WHERE id='old'").fetchone()),
                             ('superseded', 20))
            self.assertEqual(tuple(db.execute("SELECT status,completed_at FROM campaigns WHERE id='finished'").fetchone()),
                             ('completed', 3))
            self.assertEqual(db.execute("SELECT count(*) FROM campaign_recipients WHERE campaign_id='old' AND status='pending'").fetchone()[0], 59)

    async def test_replacement_is_atomic_and_refuses_inflight_sends(self):
        await self.due()
        before = self.store.preview()
        with self.assertRaises(ValueError):
            self.store.activate('two', at=1, replace_active=True, clock=lambda: self.now)
        self.assertEqual(before, self.store.preview())
        row = self.store.claim(self.now)
        for status in ('processing', 'sending'):
            self.store.update(row, status=status)
            with self.assertRaises(ValueError):
                self.store.activate('two', replace_active=True, clock=lambda: self.now)
        self.store.initialize()  # Restart: interrupted sending is uncertain, never resent.
        self.store.activate('two', replace_active=True, clock=lambda: self.now)
        self.assertIsNone(self.recipient(name='two'))
        self.assertEqual(self.recipient()['status'], 'uncertain')

    async def test_preview_counts_delivered_users_without_double_counting(self):
        await self.due()
        await self.service.tick()
        await self.db.update(42, status='delivered', delivered_at=self.now)
        await self.db.schedule(43, 10, 20)
        await self.db.update(43, status='delivered', delivered_at=30)
        result = self.store.preview()
        self.assertEqual(result['previously_delivered'], 2)
        self.assertEqual(result['eligible'], 0)

    async def test_global_send_rate_limit_survives_restart_and_holds_other_users(self):
        await self.register(42)
        await self.register(43)
        self.activate()
        self.bot.send_message.side_effect = RetryAfter(120)
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()
        self.store.initialize()
        self.bot.send_message.side_effect = None
        self.bot.send_message.reset_mock()
        self.bot.get_chat_member.reset_mock()
        self.now += 119
        await self.service.tick()
        self.bot.get_chat_member.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.now += 1
        await self.service.tick()
        self.assertEqual(self.bot.get_chat_member.await_count, 20)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_membership_rate_limit_withholds_and_stops_further_requests(self):
        await self.due()
        self.bot.get_chat_member.side_effect = RetryAfter(120)
        await self.service.tick()
        self.bot.get_chat_member.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.recipient()['status'], 'pending')
        self.assertEqual(self.recipient()['next_attempt_at'], self.now + 120)


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

    def test_five_distinct_channels_required(self):
        config.validate_channels(config.CHANNELS)
        with self.assertRaises(ValueError):
            config.validate_channels(config.CHANNELS[:4])
        self.assertEqual([c.chat_id for c in config.CHANNELS], [
            '@linguamaterials', '@nymomenglish', '@sofikenglish', '@miss_di_english', '@shek_english'])
