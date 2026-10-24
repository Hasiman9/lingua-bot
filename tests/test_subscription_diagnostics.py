"""All Telegram calls are mocked; no real token, subscriber database or campaign is used."""
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Bot, User
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest

import config
import diagnose_channels
from safe_logging import RedactingFormatter
from subscriptions import api_error_details, check_membership, verify


class ErrorClassificationTests(unittest.TestCase):
    def test_distinct_failure_categories(self):
        cases = [
            (TimedOut(), 'timeout'),
            (InvalidToken('invalid'), 'authentication_error'),
            (RetryAfter(30), 'rate_limited'),
            (BadRequest('Chat_admin_required'), 'insufficient_admin'),
            (BadRequest('Not enough rights'), 'insufficient_admin'),
            (BadRequest('Member list is inaccessible'), 'insufficient_admin'),
            (BadRequest('Chat not found'), 'channel_id_or_access'),
            (BadRequest('Username_not_occupied'), 'channel_id_or_access'),
            (Forbidden('bot was kicked'), 'access_denied'),
            (BadRequest('User not found'), 'invalid_user'),
            (BadRequest('other'), 'api_bad_request'),
            (NetworkError('connection reset'), 'network_error'),
            (TelegramError('other'), 'api_error'),
        ]
        for error, category in cases:
            with self.subTest(error=type(error).__name__, category=category):
                self.assertEqual(api_error_details(error)[0], category)

    def test_shared_formatter_redacts_token_in_traceback_text(self):
        token = '123456:' + 'x' * 35
        formatter = RedactingFormatter('%(message)s')
        formatter.token = token
        record = logging.LogRecord('test', logging.ERROR, __file__, 1,
                                   'URL https://api.telegram.org/bot%s/getChatMember', (token,), None)
        self.assertNotIn(token, formatter.format(record))


class SubscriptionDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(id=999, get_chat_member=AsyncMock())

    async def test_all_ten_calls_log_id_role_and_status(self):
        self.bot.get_chat_member.return_value = SimpleNamespace(status='administrator')
        with self.assertLogs('subscriptions', level='INFO') as logs:
            result = await verify(self.bot, 42, config.CHANNELS)
        self.assertTrue(result.passed)
        self.assertEqual(self.bot.get_chat_member.await_count, 10)
        for channel in config.CHANNELS:
            for role in ('bot', 'subscriber'):
                self.assertTrue(any(f'channel_id={channel.chat_id} role={role} status=administrator' in line
                                    for line in logs.output))

    async def test_mixed_missing_permissions_timeout_and_invalid_id_continue_all_channels(self):
        async def response(chat_id, user_id):
            index = [c.chat_id for c in config.CHANNELS].index(chat_id)
            if index == 2:
                raise TimedOut()
            if index == 3:
                raise BadRequest('Chat not found')
            if index == 4 and user_id == 42:
                raise NetworkError('temporary transport failure')
            if user_id == 999:
                return SimpleNamespace(status='member' if index == 1 else 'administrator')
            return SimpleNamespace(status='left' if index == 0 else 'member')
        self.bot.get_chat_member.side_effect = response
        with self.assertLogs('subscriptions', level='INFO') as logs:
            result = await verify(self.bot, 42, config.CHANNELS)
        self.assertEqual(result.missing, [config.CHANNELS[0]])
        self.assertEqual(result.errors, list(config.CHANNELS[1:5]))
        self.assertFalse(result.passed)
        text = '\n'.join(logs.output)
        for category in ('missing_subscription', 'insufficient_admin', 'timeout', 'channel_id_or_access', 'network_error'):
            self.assertIn('category=' + category, text)
        for channel in config.CHANNELS:
            self.assertIn('channel_id=' + channel.chat_id, text)
        self.assertIn('subscriber_check=skipped reason=bot_access_failed', text)
        self.assertIn('exception=TimedOut', text)
        self.assertIn('exception=BadRequest', text)

    async def test_api_exception_never_logs_raw_secrets_even_without_formatter(self):
        token = '123456:' + 'z' * 35
        secret = 'private-proxy-password'
        invite = 'https://t.me/+private-invite'
        self.bot.get_chat_member.side_effect = NetworkError(
            f'https://api.telegram.org/bot{token}/getChatMember password={secret} {invite}')
        with self.assertLogs('subscriptions', level='INFO') as logs:
            result = await check_membership(self.bot, config.CHANNELS[0], 999, 'bot')
        text = '\n'.join(logs.output)
        for value in (token, secret, invite, 'https://api.telegram.org'):
            self.assertNotIn(value, text)
        self.assertEqual(result.category, 'network_error')
        self.assertIn('exception=NetworkError', text)

    async def test_restricted_membership_is_logged_and_enforced(self):
        for active in (True, False):
            self.bot.get_chat_member.return_value = SimpleNamespace(status='restricted', is_member=active)
            with self.assertLogs('subscriptions', level='INFO') as logs:
                result = await check_membership(self.bot, config.CHANNELS[0], 42, 'subscriber')
            self.assertEqual(result.category, 'ok' if active else 'missing_subscription')
            self.assertIn(f'is_member={active}', '\n'.join(logs.output))

    async def test_read_only_diagnostic_calls_all_five_even_after_failure(self):
        self.bot.get_chat_member.side_effect = [
            BadRequest('Chat not found'), SimpleNamespace(status='member'),
            *[SimpleNamespace(status='administrator') for _ in range(3)],
        ]
        with self.assertLogs(level='INFO') as logs:
            code = await diagnose_channels.diagnose(self.bot, config.CHANNELS)
        self.assertEqual(code, 1)
        self.assertEqual(self.bot.get_chat_member.await_count, 5)
        self.assertIn('administrator_ok=3 failed=2', '\n'.join(logs.output))
        for call, channel in zip(self.bot.get_chat_member.call_args_list, config.CHANNELS):
            self.assertEqual(call.kwargs, dict(chat_id=channel.chat_id, user_id=999))

    async def test_real_bot_lifecycle_uses_read_only_api_and_no_database(self):
        async def identify(bot, *args, **kwargs):
            # The real get_me caches its response; preserve that behavior in the mock.
            bot._bot_user = User(id=999, first_name='Test', is_bot=True, username='test_bot')
            return bot._bot_user

        with (patch.object(Bot, 'get_me', autospec=True, side_effect=identify) as me,
              patch.object(Bot, 'get_chat_member', new_callable=AsyncMock,
                           return_value=SimpleNamespace(status='administrator')) as membership,
              patch.object(HTTPXRequest, 'do_request', side_effect=AssertionError('Unexpected network call')),
              patch('database.Database.open', side_effect=AssertionError('Database must not open')),
              patch('campaigns.CampaignStore.activate', side_effect=AssertionError('Must not activate')),
              self.assertLogs(level='INFO') as logs):
            code = await diagnose_channels.run('123456:offline-test-token')
        self.assertEqual(code, 0)
        me.assert_awaited_once()
        self.assertEqual(membership.await_count, 5)
        self.assertIn('administrator_ok=5 failed=0', '\n'.join(logs.output))

    async def test_diagnostic_auth_failure_is_safe(self):
        secret = 'hidden-auth-details'
        with (patch.object(Bot, 'get_me', new_callable=AsyncMock, side_effect=InvalidToken(secret)),
              self.assertLogs(level='ERROR') as logs):
            code = await diagnose_channels.run('123456:offline-test-token')
        self.assertEqual(code, 2)
        self.assertNotIn(secret, '\n'.join(logs.output))
        self.assertIn('category=authentication_error', '\n'.join(logs.output))
