from typing import Final
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import random
import logging

# ---------------- Logging Setup ----------------
logging.basicConfig(level=logging.INFO)  # Eigene Logs sichtbar
logging.getLogger("httpx").setLevel(logging.WARNING)  # Unterdrückt httpx INFO Logs
logger = logging.getLogger(__name__)

# ---------------- Constants ----------------
TOKEN: Final = ''
BOT_USERNAME: Final = '@linguamaterials_bot'
FORWARD_TO_USER_ID: Final = ''

RESPONSES = [
    'Thank you!',
    'Got it! We’ll share it soon 📢',
    'Thanks for your story 🙌',
    'Appreciate it! Your input matters ❤️'
]

# ---------------- Command Handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f'User ({update.message.chat.id}) used /start')
    await update.message.reply_text(
        'LINGUA MATERIALS BOT \n\n'
        'This is your space to:\n\n'
        '❓ Ask professional questions \n'
        '📕 Share interesting, funny, or challenging experiences from your teaching practice \n'
        '💬 Start discussions on relevant topics in education \n\n'
        'All stories, questions and ideas will be posted on Telegram channel '
        '<a href="https://t.me/linguamaterials">LINGUA MATERIALS</a> \n\n'
        'Write your message down👇',
        parse_mode="HTML"
    )

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f'User ({update.message.chat.id}) used /emc')
    await update.message.reply_text(
        'Available EMC’s + links on the channel now:\n\n'
        '• <a href="https://t.me/linguamaterials/11">Kid’s Box New Generation</a>\n'
        '• <a href="https://t.me/linguamaterials/31">Kid’s Box Updated Second Edition</a>\n'
        '• <a href="https://t.me/linguamaterials/21">Eyes Open</a>\n'
        '• <a href="https://t.me/linguamaterials/38">English File 4th Edition</a>\n'
        '• <a href="https://t.me/linguamaterials/70">Gateway to the World</a>\n'
        '• <a href="https://t.me/linguamaterials/120">SpeakOut 2nd Edition</a>\n'
        '• <a href="https://t.me/linguamaterials/143">Wider World 2nd Edition</a>\n'
        '• <a href="https://t.me/linguamaterials/150">Go Getter</a>\n'
        '• <a href="https://t.me/linguamaterials/185">Family and Friends</a>\n\n'
        'If you didn’t find the EMC you were looking for, write its name to the bot after the “start” command. We will try to search for it and post it soon.',
        parse_mode="HTML"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f'User ({update.message.chat.id}) used /help')
    await update.message.reply_text(
        "Available commands:\n"
        "/start - Learn how to use this bot\n"
        "/emc - Find links to educational materials\n"
        "/help - Show this help message\n\n"
        "Or just type your question/story directly 👇"
    )

# ---------------- Response & Forward ----------------
def handle_response(text: str) -> str:
    processed: str = text.lower()
    if len(processed) < 1000:
        return random.choice(RESPONSES)
    else:
        return "Thank you for sharing your detailed story with us! 🙏"

async def forward_message_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        await context.bot.copy_message(
            chat_id=FORWARD_TO_USER_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.effective_message.id
        )
        text = (getattr(update.effective_message, "text", None)
                or getattr(update.effective_message, "caption", None)
                or "<no text/caption>")
        await context.bot.send_message(
            chat_id=FORWARD_TO_USER_ID,
            text=f"[from {user_id}] {text}"
        )
        logger.info(f"Forwarded message from {user_id}: {text}")
    except Exception as e:
        logger.error(f"Forward failed from {user_id}: {e}")

# ---------------- Message Handlers ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_type: str = update.message.chat.type
    text: str = update.message.text
    logger.info(f'User ({update.message.chat.id}) sent message: "{text}"')

    if message_type == 'group':
        if BOT_USERNAME in text:
            new_text: str = text.replace(BOT_USERNAME, '').strip()
            response: str = handle_response(new_text)
        else:
            return
    else:
        response: str = handle_response(text)

    await forward_message_to_user(update, context)
    await update.message.reply_text(response, parse_mode="HTML")

async def handle_non_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_type = update.effective_chat.type

    # Detect media type
    media_type = None
    file_name = None

    if msg.photo:
        media_type = "photo"
        file_name = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        file_name = msg.video.file_id
    elif msg.document:
        media_type = "document"
        file_name = msg.document.file_name or msg.document.file_id
    elif msg.audio:
        media_type = "audio"
        file_name = msg.audio.file_id
    elif msg.voice:
        media_type = "voice"
        file_name = msg.voice.file_id
    elif msg.sticker:
        media_type = "sticker"
        file_name = msg.sticker.file_id
    else:
        media_type = "unknown"
        file_name = "<no file_id>"

    logger.info(
        f"User ({msg.chat.id}) sent {media_type}: {file_name}, caption: {msg.caption}"
    )

    if chat_type == 'group' and BOT_USERNAME not in (msg.caption or ""):
        return

    await forward_message_to_user(update, context)
    await msg.reply_text(
        "Thanks! We received your media. Your submission will remain anonymous. 👀"
    )

# ---------------- Admin Reply ----------------
async def reply_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        logger.info(f"Sent private reply to user {user_id}: {text}")
    except Exception as e:
        logger.error(f"Failed to send private reply to {user_id}: {e}")

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != FORWARD_TO_USER_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return

    try:
        user_id = int(context.args[0])
        text = " ".join(context.args[1:])
        await reply_to_user(context, user_id, text)
        await update.message.reply_text(f"✅ Replied to {user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user_id format.")

# ---------------- Unknown Command ----------------
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f'User ({update.message.chat.id}) used unknown command: "{update.message.text}"')
    await update.message.reply_text("Sorry, I don’t recognize that command. Try /help 🙂")

# ---------------- Error Handler ----------------
async def error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error: {context.error}")

# ---------------- Main ----------------
if __name__ == '__main__':
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler('start', start_command))
    app.add_handler(CommandHandler('emc', find_command))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('reply', reply_command))

    # Text Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Non-text Messages (media)
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, handle_non_text_message))

    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Error handler
    app.add_error_handler(error)

    logger.info("Bot is starting polling...")
    app.run_polling()

