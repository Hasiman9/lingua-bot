import logging
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import Application

import main


class LoggingTests(unittest.TestCase):
    def test_exception_traceback_redacts_token_and_request_url(self):
        token = "123456:" + "a" * 30
        formatter = main.RedactingFormatter("%(levelname)s %(message)s")
        formatter.token = token
        try:
            raise RuntimeError(f"Request failed: https://api.telegram.org/bot{token}/getUpdates")
        except RuntimeError:
            record = logging.LogRecord("test", logging.ERROR, __file__, 1,
                                       "Startup failed", (), sys.exc_info())
        output = formatter.format(record)
        self.assertIn("Traceback (most recent call last)", output)
        self.assertIn("RuntimeError", output)
        self.assertIn("<BOT_TOKEN_REDACTED>", output)
        self.assertNotIn(token, output)

    def test_configuration_failure_logs_traceback_and_exits_unsuccessfully(self):
        with (patch.object(main.config, "load_settings", side_effect=ValueError("invalid configuration")),
              self.assertLogs(main.logger, level="ERROR") as logs,
              self.assertRaises(SystemExit) as exit_status):
            main.main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))


class LifecycleLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_lifecycle_success_checkpoints(self):
        app = main.LoggedApplication.builder().application_class(main.LoggedApplication).token(
            "123456:offline-test-token"
        ).build()
        with (patch.object(Application, "initialize", new_callable=AsyncMock),
              patch.object(Application, "start", new_callable=AsyncMock),
              patch.object(Application, "stop", new_callable=AsyncMock),
              patch.object(Application, "shutdown", new_callable=AsyncMock),
              self.assertLogs(main.logger, level="INFO") as logs):
            await app.initialize()
            await app.start()
            await app.stop()
            await app.shutdown()
        output = "\n".join(logs.output)
        self.assertIn("initialized successfully", output)
        self.assertIn("started successfully", output)
        self.assertIn("Application stopped", output)
        self.assertIn("resources shut down", output)

    async def test_application_failure_logs_and_propagates(self):
        app = Application.builder().application_class(main.LoggedApplication).token(
            "123456:offline-test-token"
        ).build()
        for method in ("initialize", "start", "stop", "shutdown"):
            with (self.subTest(method=method),
                  patch.object(Application, method, new_callable=AsyncMock,
                               side_effect=RuntimeError("lifecycle failure")),
                  self.assertLogs(main.logger, level="ERROR") as logs,
                  self.assertRaises(RuntimeError)):
                await getattr(app, method)()
            self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))

    async def test_polling_error_handler_includes_original_traceback(self):
        try:
            raise RuntimeError("polling failed")
        except RuntimeError as error:
            with self.assertLogs(main.logger, level="ERROR") as logs:
                await main.on_error(None, SimpleNamespace(error=error))
        self.assertIn("Traceback (most recent call last)", "\n".join(logs.output))
