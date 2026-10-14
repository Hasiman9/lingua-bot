"""Polling entry point. Only /start and the single verification callback are handled."""
import logging

from filelock import FileLock, Timeout
from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

import config
from database import Database
from materials import validate_materials
from scheduler import DeliveryService
from subscriptions import keyboard

logger = logging.getLogger(__name__)


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
    settings = application.bot_data["settings"]
    db = Database(settings.database_path)
    await db.open(settings.campaign_at.timestamp())
    application.bot_data["database"] = db
    service = DeliveryService(db, application.bot, config.CHANNELS, settings.materials_url,
                              settings.campaign_at, message=settings.materials_message)
    application.bot_data["service"] = service
    await application.bot.set_my_commands([BotCommand("start", "Get free educational materials")])
    application.job_queue.run_repeating(service.tick, interval=config.POLL_SECONDS, first=1,
                                        job_kwargs={"max_instances": 1, "coalesce": True})
    logger.info("Persistent delivery scheduler ready")
    if not settings.materials_url:
        logger.info("Materials delivery disabled: MATERIALS_URL is empty; subscription verification is available")


async def on_shutdown(application):
    if "database" in application.bot_data:
        await application.bot_data["database"].close()


async def on_error(update, context):
    logger.error("Telegram handler failed (%s)", type(context.error).__name__)


def build_application(settings):
    """Construct handlers and lifecycle hooks without connecting or starting polling."""
    validate_materials(settings.materials_url, settings.materials_message)
    application = (Application.builder().token(settings.token)
                   .post_init(on_startup).post_shutdown(on_shutdown).build())
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_command, filters=filters.ChatType.PRIVATE))
    application.add_handler(CallbackQueryHandler(confirm_callback, pattern="^verify$"))
    application.add_error_handler(on_error)
    return application


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Third-party request logging can expose token-bearing Telegram URLs.
    for name in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = config.load_settings()
    except (ValueError, OSError) as exc:
        logger.error("Configuration invalid (%s). Check .env, channels, and materials.", type(exc).__name__)
        raise SystemExit(1) from None
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # OS-backed lock is released on crashes. Run one polling process on one host.
        with FileLock(str(settings.database_path) + ".lock", timeout=0):
            application = build_application(settings)
            application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    except Timeout:
        logger.error("Another bot process already owns this database")
    except Exception as exc:
        logger.error("Bot stopped (%s)", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
