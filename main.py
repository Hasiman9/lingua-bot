"""Polling entry point. Only /start and the single verification callback are handled."""
import logging
import os
import sys

from filelock import FileLock, Timeout
from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

import config
from database import Database
from campaigns import CampaignStore
from materials import validate_materials
from scheduler import DeliveryService
from subscriptions import keyboard
from safe_logging import RedactingFormatter

logger = logging.getLogger(__name__)


class LoggedApplication(Application):
    """Observe PTB's standard run_polling lifecycle without replacing it."""

    async def initialize(self):
        logger.info("Initializing Telegram Application (Bot API authentication/getMe)")
        try:
            await super().initialize()
        except Exception:
            logger.exception("Telegram Application initialization failed")
            raise
        logger.info("Telegram Application initialized successfully")

    async def start(self):
        logger.info("Telegram polling startup completed; updater running=%s", self.updater.running)
        logger.info("Starting Telegram Application update processing and delivery scheduler")
        try:
            await super().start()
        except Exception:
            logger.exception("Telegram Application start failed")
            raise
        logger.info("Telegram Application started successfully; long polling is running")

    async def stop(self):
        logger.info("Stopping Telegram Application and delivery scheduler")
        try:
            await super().stop()
        except Exception:
            logger.exception("Telegram Application stop failed")
            raise
        logger.info("Telegram Application stopped")

    async def shutdown(self):
        logger.info("Shutting down Telegram Application resources")
        try:
            await super().shutdown()
        except Exception:
            logger.exception("Telegram Application shutdown failed")
            raise
        logger.info("Telegram Application resources shut down")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = context.application.bot_data["settings"]
    await update.effective_message.reply_text(
        config.WELCOME.format(campaign_time=config.campaign_label(settings.campaign_at)),
        reply_markup=keyboard(config.CHANNELS),
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    await context.application.bot_data["service"].confirm(update.effective_user.id)


async def on_startup(application):
    logger.info("Running post-initialization setup; opening persistent SQLite storage")
    settings = application.bot_data["settings"]
    db = Database(settings.database_path)
    await db.open(settings.campaign_at.timestamp())
    CampaignStore(settings.database_path).initialize()
    application.bot_data["database"] = db
    service = DeliveryService(db, application.bot, config.CHANNELS, settings.materials_url,
                              settings.campaign_at, message=settings.materials_message)
    application.bot_data["service"] = service
    logger.info("Registering /start command with Telegram")
    await application.bot.set_my_commands([BotCommand("start", "Start")])
    logger.info("Registering delivery scheduler job (interval=%s seconds)", config.POLL_SECONDS)
    application.job_queue.run_repeating(service.tick, interval=config.POLL_SECONDS, first=1,
                                        job_kwargs={"max_instances": 1, "coalesce": True})
    logger.info("Persistent delivery scheduler registered; starts with Telegram Application")
    logger.info("Materials require an explicitly activated campaign or temporary reward window")
    logger.info("Post-initialization complete; starting Telegram polling (webhook removal/getUpdates bootstrap)")


async def on_shutdown(application):
    logger.info("Running shutdown hook; closing SQLite storage")
    try:
        if "database" in application.bot_data:
            await application.bot_data["database"].close()
    except Exception:
        logger.exception("SQLite shutdown failed")
        raise
    logger.info("Shutdown hook completed")


async def on_error(update, context):
    error = context.error
    logger.error("Telegram update, polling, or scheduler error",
                 exc_info=(type(error), error, error.__traceback__))


def build_application(settings):
    """Construct handlers and lifecycle hooks without connecting or starting polling."""
    validate_materials(settings.materials_url, settings.materials_message)
    logger.info("Creating Telegram Application and handlers")
    application = (Application.builder().application_class(LoggedApplication).token(settings.token)
                   .post_init(on_startup).post_shutdown(on_shutdown).build())
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(confirm_callback, pattern="^verify$"))
    application.add_error_handler(on_error)
    logger.info("Telegram Application created; network startup has not begun yet")
    return application


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Keep transport request chatter quiet, but expose warnings/errors and PTB lifecycle logs.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.INFO)
    try:
        logger.info("Bot process starting: pid=%s python=%s cwd=%s", os.getpid(), sys.executable, os.getcwd())
        logger.info("Loading configuration from %s (process environment takes precedence)", config.BASE_DIR / ".env")
        settings = config.load_settings()
        formatter.token = settings.token
        logger.info("Configuration loaded: campaign=%s database=%s materials_enabled=%s",
                    config.campaign_label(settings.campaign_at), settings.database_path, bool(settings.materials_url))
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Acquiring exclusive database process lock")
        # OS-backed lock is released on crashes. Run one polling process on one host.
        with FileLock(str(settings.database_path) + ".lock", timeout=0):
            application = build_application(settings)
            logger.info("Entering run_polling: initialize -> post_init -> polling -> Application.start")
            application.run_polling(allowed_updates=["message", "callback_query"],
                                    drop_pending_updates=True, bootstrap_retries=0)
            logger.info("run_polling returned; bot process is exiting")
    except Timeout:
        logger.exception("Another bot process already owns this database; stop the competing instance")
        raise SystemExit(1) from None
    except Exception:
        logger.exception("Bot startup, polling, or shutdown failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
