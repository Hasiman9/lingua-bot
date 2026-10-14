import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from telegram.error import Forbidden, NetworkError, RetryAfter

from config import Channel, load_settings, parse_campaign, validate_channels
from database import Database
from materials import send_materials, validate_materials
from scheduler import DeliveryService
from subscriptions import is_member, keyboard, verify


CHANNELS = tuple(Channel(f"@channel{i}", f"Channel {i}", f"https://t.me/channel{i}") for i in range(6))
# Used only with mocked Telegram sends; no requests are made to this URL.
MATERIALS_URL = "https://t.me/linguamaterials"
MATERIALS_MESSAGE = "Your educational materials are ready."


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "bot.sqlite3"
        self.db = Database(self.path)
        self.campaign = parse_campaign("2026-10-22", "16:00", "Asia/Krasnoyarsk")
        await self.db.open(self.campaign.timestamp())
        self.now = self.campaign.timestamp() - 3600
        self.statuses = ["member"] * 6
        self.admin = True
        self.failed_channel = None
        self.bot = SimpleNamespace(id=999, get_chat_member=AsyncMock(side_effect=self.membership),
                                   send_message=AsyncMock(), send_document=AsyncMock(), send_video=AsyncMock())
        self.service = self.make_service()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def make_service(self, url=MATERIALS_URL, message=MATERIALS_MESSAGE):
        return DeliveryService(self.db, self.bot, CHANNELS, url,
                               campaign_at=self.campaign, message=message, clock=lambda: self.now)

    def assert_no_materials_message(self):
        for call in self.bot.send_message.call_args_list:
            markup = call.kwargs.get("reply_markup")
            if markup:
                self.assertFalse(any(button.text == "🎁 Get Materials"
                                     for row in markup.inline_keyboard for button in row))

    async def membership(self, chat_id, user_id):
        index = int(chat_id[-1])
        if index == self.failed_channel:
            raise NetworkError("temporary failure")
        if user_id == self.bot.id:
            return SimpleNamespace(status="administrator" if self.admin else "member")
        return SimpleNamespace(status=self.statuses[index], is_member=True)

    async def schedule_due(self):
        await self.service.confirm(42)
        self.now = self.campaign.timestamp()
        self.bot.send_message.reset_mock()
        self.bot.get_chat_member.reset_mock()

    async def test_all_six_valid(self):
        self.statuses = ["member", "administrator", "creator", "owner", "restricted", "member"]
        result = await verify(self.bot, 42, CHANNELS)
        self.assertTrue(result.passed)
        user_calls = [call for call in self.bot.get_chat_member.call_args_list if call.kwargs["user_id"] == 42]
        self.assertEqual(len(user_calls), 6)

    async def test_only_one_valid(self):
        self.statuses = ["member"] + ["left"] * 5
        await self.service.confirm(42)
        self.assertIsNone(await self.db.get(42))
        markup = self.bot.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 6)

    async def test_five_valid_one_missing(self):
        self.statuses[-1] = "kicked"
        result = await verify(self.bot, 42, CHANNELS)
        self.assertEqual(result.missing, [CHANNELS[-1]])
        await self.service.confirm(42)
        self.assertIsNone(await self.db.get(42))

    async def test_unsubscribes_before_delivery_and_preserves_due_date(self):
        await self.schedule_due()
        original = await self.db.get(42)
        self.statuses[-1] = "left"
        await self.service.tick()
        self.assert_no_materials_message()
        row = await self.db.get(42)
        self.assertEqual(row["status"], "awaiting_subscription")
        self.assertEqual(row["progress"], 0)
        self.now += 900
        self.statuses[-1] = "member"
        await self.service.confirm(42)
        self.assertEqual((await self.db.get(42))["scheduled_at"], original["scheduled_at"])
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_users_verified_at_different_times_share_campaign_date(self):
        await self.service.confirm(42)
        self.now += 1800
        await self.service.confirm(43)
        for user_id in (42, 43):
            row = await self.db.get(user_id)
            self.assertEqual(row["scheduled_at"], self.campaign.timestamp())
            self.assertEqual(row["verification_state"], "initial_passed")
        self.bot.send_message.reset_mock()
        self.now = self.campaign.timestamp() - 1
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()
        self.now += 1
        await self.service.tick()
        self.assertEqual(self.bot.send_message.await_count, 2)
        for user_id in (42, 43):
            self.assertEqual((await self.db.get(user_id))["status"], "delivered")

    async def test_late_verification_recovers_on_next_scan(self):
        self.now = self.campaign.timestamp() + 86400
        await self.service.confirm(42)
        self.assertEqual((await self.db.get(42))["scheduled_at"], self.campaign.timestamp())
        self.bot.get_chat_member.reset_mock()
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")
        self.assertEqual(self.bot.get_chat_member.await_count, 12)

    async def test_duplicate_confirmation_buttons(self):
        await asyncio.gather(*(self.service.confirm(42) for _ in range(5)))
        first = await self.db.get(42)
        self.now += 100
        await self.service.confirm(42)
        row = await self.db.get(42)
        self.assertEqual(row["scheduled_at"], first["scheduled_at"])
        self.assertEqual(row["first_verified_at"], first["first_verified_at"])

    async def test_restart_before_campaign_and_overdue_recovery(self):
        await self.service.confirm(42)
        self.bot.send_message.reset_mock()
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now += 7200
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_crashed_processing_claim_recovers(self):
        await self.schedule_due()
        self.assertIsNotNone(await self.db.claim_due(self.now))
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service()
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_initial_api_failure_never_schedules(self):
        self.failed_channel = 2
        await self.service.confirm(42)
        self.assertIsNone(await self.db.get(42))
        self.assertEqual(self.bot.get_chat_member.await_count, 11)

    async def test_temporary_api_failure_retries(self):
        await self.schedule_due()
        self.failed_channel = 3
        await self.service.tick()
        row = await self.db.get(42)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["verification_state"], "verification_error")
        self.assertEqual(row["retry_attempts"], 1)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()
        self.failed_channel = None
        self.now = row["next_attempt_at"]
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_bot_missing_admin_rights(self):
        self.admin = False
        result = await verify(self.bot, 42, CHANNELS)
        self.assertEqual(len(result.errors), 6)
        self.assertEqual(result.missing, [])
        await self.service.confirm(42)
        self.assertIsNone(await self.db.get(42))

    async def test_admin_rights_removed_before_delivery(self):
        await self.schedule_due()
        self.admin = False
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.db.get(42))["status"], "pending")

    async def test_user_blocks_bot(self):
        await self.schedule_due()
        self.bot.send_message.side_effect = Forbidden("blocked")
        await self.service.tick()
        row = await self.db.get(42)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["progress"], 0)
        self.bot.send_document.assert_not_awaited()

    async def test_campaign_delivery_and_repeated_attempts(self):
        await self.schedule_due()
        await asyncio.gather(self.service.tick(), self.service.tick())
        await self.service.tick()
        row = await self.db.get(42)
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["second_verified_at"], self.now)
        self.assertEqual(row["delivered_at"], self.now)
        self.bot.send_message.assert_awaited_once()
        kwargs = self.bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["text"], MATERIALS_MESSAGE)
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🎁 Get Materials")
        self.assertEqual(button.url, MATERIALS_URL)
        self.bot.send_document.assert_not_awaited()
        self.bot.send_video.assert_not_awaited()
        self.assertEqual(self.bot.get_chat_member.await_count, 12)

    async def test_send_network_failure_retries_after_restart(self):
        await self.schedule_due()
        self.bot.send_message.side_effect = NetworkError("temporary")
        await self.service.tick()
        row = await self.db.get(42)
        self.assertEqual(row["progress"], 0)
        self.assertEqual(row["status"], "pending")
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service()
        self.bot.send_message.side_effect = None
        self.bot.send_message.reset_mock()
        self.bot.get_chat_member.reset_mock()
        self.now = row["next_attempt_at"]
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.get_chat_member.await_count, 12)
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_membership_rechecked_before_retry(self):
        await self.schedule_due()
        self.bot.send_message.side_effect = NetworkError("temporary")
        await self.service.tick()
        self.now = (await self.db.get(42))["next_attempt_at"]
        self.bot.send_message.side_effect = None
        self.bot.send_message.reset_mock()
        self.statuses[-1] = "left"
        await self.service.tick()
        self.assert_no_materials_message()
        self.assertEqual((await self.db.get(42))["status"], "awaiting_subscription")

    async def test_rate_limit_respected(self):
        await self.schedule_due()
        self.bot.send_message.side_effect = RetryAfter(120)
        await self.service.tick()
        row = await self.db.get(42)
        self.assertEqual(row["next_attempt_at"], self.now + 120)
        self.bot.send_message.reset_mock()
        self.now += 119
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()

    async def test_legacy_schedule_migration_preserves_progress_and_completed_users(self):
        old_due = self.campaign.timestamp() - 86400
        for user_id in (42, 43, 44):
            await self.db.schedule(user_id, self.now - 86400, old_due)
        await self.db.update(42, progress=1, status="processing", manifest="legacy-file-manifest")
        await self.db.update(43, progress=2, status="delivered", delivered_at=old_due)
        await self.db.update(44, status="awaiting_subscription")
        first = (await self.db.get(42))["first_verified_at"]
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service()
        row = await self.db.get(42)
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["first_verified_at"], first)
        self.assertEqual(row["scheduled_at"], self.campaign.timestamp())
        self.assertEqual((await self.db.get(44))["status"], "awaiting_subscription")
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now = self.campaign.timestamp()
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.db.get(43))["scheduled_at"], old_due)
        self.assertEqual((await self.db.get(43))["status"], "delivered")

    async def test_retry_backoff_survives_restart(self):
        await self.schedule_due()
        self.failed_channel = 3
        await self.service.tick()
        before = await self.db.get(42)
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service()
        self.assertEqual((await self.db.get(42))["next_attempt_at"], before["next_attempt_at"])
        self.failed_channel = None
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.now = before["next_attempt_at"]
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["status"], "delivered")

    async def test_empty_url_stays_pending_until_configured(self):
        self.service = self.make_service(url="")
        await self.schedule_due()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()
        self.assertEqual((await self.db.get(42))["status"], "pending")
        self.service = self.make_service()
        self.now = (await self.db.get(42))["next_attempt_at"]
        await self.service.tick()
        self.bot.send_message.assert_awaited_once()

    async def test_completed_message_not_resent_after_restart_or_config_change(self):
        await self.schedule_due()
        await self.service.tick()
        await self.db.close()
        self.db = Database(self.path)
        await self.db.open(self.campaign.timestamp())
        self.service = self.make_service(url=MATERIALS_URL + "/1", message="Updated message")
        self.bot.send_message.reset_mock()
        await self.service.tick()
        self.bot.send_message.assert_not_awaited()
        await self.service.confirm(42)
        self.assert_no_materials_message()
        await self.service.tick()
        self.assertEqual((await self.db.get(42))["progress"], 1)

    async def test_sender_rejects_blank_or_placeholder_urls(self):
        for url in ("", "https://example.com/materials", "https://host.invalid/materials"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await send_materials(self.bot, 42, url, MATERIALS_MESSAGE)
        self.bot.send_message.assert_not_awaited()

    def test_statuses_and_keyboard(self):
        for status in ("left", "kicked", "banned", "unknown"):
            self.assertFalse(is_member(SimpleNamespace(status=status)))
        self.assertFalse(is_member(SimpleNamespace(status="restricted", is_member=False)))
        self.assertEqual(len(keyboard(CHANNELS).inline_keyboard), 7)
        self.assertEqual(keyboard(CHANNELS).inline_keyboard[-1][0].callback_data, "verify")

    def test_configuration_validation(self):
        validate_channels(CHANNELS)
        with self.assertRaises(ValueError):
            validate_channels(CHANNELS[:5])
        for url in ("not-a-url", "ftp://host.com/file", "https://example.org", "https://host.com/REPLACE_ME"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_materials(url, MATERIALS_MESSAGE)
        for message in ("", " ", "x" * 4097):
            with self.assertRaises(ValueError):
                validate_materials(MATERIALS_URL, message)


class CampaignConfigurationTests(unittest.TestCase):
    def test_krasnoyarsk_campaign_converts_to_utc_and_berlin(self):
        campaign = parse_campaign("2026-10-22", "16:00", "Asia/Krasnoyarsk")
        self.assertEqual(campaign.utcoffset().total_seconds(), 7 * 3600)
        self.assertEqual(campaign.astimezone(timezone.utc), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
        berlin = campaign.astimezone(ZoneInfo("Europe/Berlin"))
        self.assertEqual(berlin.strftime("%Y-%m-%d %H:%M %Z"), "2026-10-22 11:00 CEST")

    def test_summer_uses_daylight_saving_time(self):
        campaign = parse_campaign("2026-07-27", "10:00", "Europe/Berlin")
        self.assertEqual(campaign.astimezone(timezone.utc).hour, 8)

    def test_invalid_or_ambiguous_campaign_settings_rejected(self):
        for date, time, zone in (
            ("2026-02-30", "10:00", "Europe/Berlin"),
            ("2026-10-27", "25:00", "Europe/Berlin"),
            ("2026-10-27", "10:00", "Invalid/Zone"),
            ("2026-03-29", "02:30", "Europe/Berlin"),
            ("2026-10-25", "02:30", "Europe/Berlin"),
        ):
            with self.subTest(date=date, time=time, zone=zone), self.assertRaises(ValueError):
                parse_campaign(date, time, zone)

    @patch("config.load_dotenv")
    @patch("config.CHANNELS", CHANNELS)
    def test_environment_campaign_settings(self, dotenv):
        with patch.dict("os.environ", {"BOT_TOKEN": "test-only", "CAMPAIGN_DATE": "2026-10-22",
                                      "CAMPAIGN_TIME": "16:00", "CAMPAIGN_TIMEZONE": "Asia/Krasnoyarsk",
                                      "MATERIALS_URL": MATERIALS_URL}, clear=True):
            settings = load_settings()
        self.assertEqual(settings.campaign_at.hour, 16)
        self.assertEqual(settings.campaign_at.minute, 0)
        self.assertEqual(settings.campaign_at.astimezone(timezone.utc), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))
        self.assertEqual(settings.materials_url, MATERIALS_URL)

    @patch("config.load_dotenv")
    @patch("config.CHANNELS", CHANNELS)
    def test_default_campaign_settings(self, dotenv):
        with patch.dict("os.environ", {"BOT_TOKEN": "test-only"}, clear=True):
            settings = load_settings()
        self.assertEqual(str(settings.campaign_at.tzinfo), "Asia/Krasnoyarsk")
        self.assertEqual(settings.campaign_at.astimezone(timezone.utc), datetime(2026, 10, 22, 9, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
