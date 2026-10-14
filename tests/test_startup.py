"""Offline startup smoke tests: real dotenv, Settings, Application, hooks and SQLite."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

from telegram.ext import Application, ExtBot

import config
import main


class StartupSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.dict("os.environ", {}, clear=True))
        self.enterContext(patch.object(config, "BASE_DIR", self.root))

    def write_env(self, url=""):
        (self.root / ".env").write_text(
            "BOT_TOKEN=123456:offline-test-token\n"
            "CAMPAIGN_DATE=2026-10-22\n"
            "CAMPAIGN_TIME=16:00\n"
            "CAMPAIGN_TIMEZONE=Asia/Krasnoyarsk\n"
            "DATABASE_PATH=smoke.sqlite3\n"
            f"MATERIALS_URL={url}\n",
            encoding="utf-8",
        )

    async def smoke_lifecycle(self, url, message):
        self.write_env(url)
        with patch.object(config, "MATERIALS_MESSAGE", message):
            settings = config.load_settings()
        # Configuration is snapshotted after dotenv loading, not captured on module import.
        self.assertEqual(settings.materials_url, url)
        self.assertEqual(settings.materials_message, message)
        self.assertEqual(settings.database_path, self.root / "smoke.sqlite3")
        self.assertEqual(settings.campaign_at.astimezone(timezone.utc),
                         datetime(2026, 10, 22, 9, tzinfo=timezone.utc))

        with (patch.object(ExtBot, "initialize", new_callable=AsyncMock),
              patch.object(ExtBot, "shutdown", new_callable=AsyncMock),
              patch.object(ExtBot, "id", new_callable=PropertyMock, return_value=999),
              patch.object(ExtBot, "set_my_commands", new_callable=AsyncMock) as commands,
              patch.object(ExtBot, "get_chat_member", new_callable=AsyncMock,
                           return_value=SimpleNamespace(status="administrator")) as membership,
              patch.object(ExtBot, "send_message", new_callable=AsyncMock) as send):
            app = main.build_application(settings)
            try:
                await app.initialize()
                await app.post_init(app)
                self.assertFalse(app.running)
                self.assertEqual(len(app.job_queue.jobs()), 1)
                commands.assert_awaited_once()
                self.assertEqual([c.command for c in commands.call_args.args[0]], ["start"])

                reply = AsyncMock()
                await main.start_command(
                    SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply)),
                    SimpleNamespace(application=app),
                )
                self.assertEqual(len(reply.call_args.kwargs["reply_markup"].inline_keyboard), 7)
                self.assertIn("2026-10-22 16:00", reply.call_args.args[0])
                service = app.bot_data["service"]
                self.assertEqual(service.message, message)
                service.clock = lambda: settings.campaign_at.timestamp() - 60
                await service.confirm(42)
                self.assertEqual(membership.await_count, 12)
                send.reset_mock()
                membership.reset_mock()
                await service.tick()
                send.assert_not_awaited()
                service.clock = lambda: settings.campaign_at.timestamp()
                await service.tick()
                row = await app.bot_data["database"].get(42)
                if url:
                    self.assertEqual(membership.await_count, 12)
                    send.assert_awaited_once()
                    self.assertEqual(send.call_args.kwargs["text"], message)
                    self.assertEqual(send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url, url)
                    self.assertEqual(row["status"], "delivered")
                else:
                    send.assert_not_awaited()
                    self.assertEqual(row["status"], "pending")
                    self.assertIsNone(row["delivered_at"])
            finally:
                await app.shutdown()
                await app.post_shutdown(app)

    async def test_startup_with_dotenv_link_and_message(self):
        await self.smoke_lifecycle("https://t.me/linguamaterials", "Configured after module import")

    async def test_subscription_only_startup_with_no_link_or_message(self):
        await self.smoke_lifecycle("", "")

    def test_placeholder_link_rejected_during_settings_loading(self):
        self.write_env("https://example.com/materials")
        with self.assertRaises(ValueError):
            config.load_settings()

    def test_enabled_delivery_requires_message(self):
        self.write_env("https://t.me/linguamaterials")
        with patch.object(config, "MATERIALS_MESSAGE", ""), self.assertRaises(ValueError):
            config.load_settings()

    def test_environment_overrides_dotenv_without_import_time_capture(self):
        self.write_env("https://t.me/linguamaterials")
        with patch.dict("os.environ", {"MATERIALS_URL": ""}):
            self.assertEqual(config.load_settings().materials_url, "")


class EntrypointSmokeTests(unittest.TestCase):
    def test_main_builds_application_without_starting_polling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("BOT_TOKEN=123456:offline-test-token\nMATERIALS_URL=\n", encoding="utf-8")
            with (patch.dict("os.environ", {}, clear=True),
                  patch.object(config, "BASE_DIR", root),
                  patch.object(Application, "run_polling", autospec=True) as polling):
                main.main()
                polling.assert_called_once()
                app = polling.call_args.args[0]
                self.assertFalse(app.running)
                self.assertEqual(app.bot_data["settings"].materials_message, config.MATERIALS_MESSAGE)
                self.assertEqual(app.bot_data["settings"].materials_url, "")


if __name__ == "__main__":
    unittest.main()
