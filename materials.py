"""One campaign message with a button to the real materials collection."""
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def validate_materials(url, message):
    if not url:
        return  # Subscription-only testing is allowed even before message text is configured.
    if not message.strip() or len(message.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("MATERIALS_MESSAGE must contain 1–4096 Telegram text characters")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    placeholders = {"example.com", "example.org", "example.net"}
    if (parsed.scheme not in {"http", "https"} or not host or "." not in host
            or parsed.username or parsed.password or any(c.isspace() for c in url)
            or host in placeholders or any(host.endswith("." + h) for h in placeholders)
            or host.endswith((".invalid", ".example", ".test", ".localhost"))
            or any(marker in url.lower() for marker in ("replace_me", "placeholder", "your_materials_url", "<", ">"))):
        raise ValueError("MATERIALS_URL must be a real HTTP(S) materials link, not a placeholder")


async def send_materials(bot, user_id, url, message):
    validate_materials(url, message)
    if not url:
        raise ValueError("MATERIALS_URL is not configured")
    await bot.send_message(
        chat_id=user_id,
        text=message,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Get Materials", url=url)]
        ]),
    )
