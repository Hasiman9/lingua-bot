"""Edit channels, campaign wording, and real materials here."""
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from materials import validate_materials

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Channel:
    chat_id: int | str
    name: str
    url: str


CHANNELS = (
    Channel("@linguamaterials", "LINGUA MATERIALS | for English teachers", "https://t.me/+IBvMcpTlTmhmYmZi"),
    Channel("@nymomenglish", "Not Your Mom’s English", "https://t.me/+N-pMyx7PSac5NDYy"),
    Channel("@sofikenglish", "Sofik.english", "https://t.me/+ADz8Fn388e1jNjlk"),
    Channel("@miss_di_english", "Di English", "https://t.me/+hasak6SslgI2NDAy"),
    Channel("@abcgames_eng", "English Games by @abcgames_eng", "https://t.me/+gtE3hF8QEithYzAy"),
    Channel("@shek_english", "Шекспирыч 🇬🇧🇨🇳language school", "https://t.me/+-uqXj5TiTeQ4OWJi"),
)
# Private channel example: Channel(-1001234567890, "Private channel", "https://t.me/+REAL_INVITE")
# chat_id must resolve to the actual required channel, independently of the join URL.
# An invite link does not prove that an @username is correct or accessible to this bot.
# Check access/admin status with: python diagnose_channels.py (no database or delivery access).

WELCOME = (
    "Hi! Спасибо, что присоединились к нашей осенней рассылке 🤎🍂\n\n"
    "Чтобы получить наши материалы, подпишись на организаторов ⬇️\n\n"
    "После подписки нажми кнопку «ГОТОВО»."
)
CONFIRM_LABEL = "ГОТОВО"
RETRY_LABEL = "ГОТОВО"
MISSING_TEXT = "Похоже, ты забыл(а) на кого-то подписаться 🙁"

"НЕ ЗАБУДЬ НАЖАТЬ КНОПКУ: ГОТОВО"
API_ERROR_TEXT = "Telegram could not verify every channel. Please try again later."
POLL_SECONDS = 30
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600

# Editable plain text. The sender adds a button using MATERIALS_URL loaded from .env.
MATERIALS_MESSAGE = (
    "🎁 Your educational materials are ready!\n\n"
    "Thank you for subscribing to our channels! ❤️\n\n"
    "Tap the button below to get your materials."
)


@dataclass(frozen=True)
class Settings:
    token: str
    campaign_at: datetime
    database_path: Path
    materials_url: str
    materials_message: str


def parse_campaign(date_text, time_text, timezone_name):
    """Resolve local wall time explicitly, rejecting DST gaps and ambiguous times."""
    try:
        local = datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M")
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Use CAMPAIGN_DATE=YYYY-MM-DD, CAMPAIGN_TIME=HH:MM and an IANA timezone") from exc
    candidates = {}
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates[utc] = aware
    if len(candidates) != 1:
        raise ValueError("Campaign local time is ambiguous or nonexistent due to daylight saving time")
    return next(iter(candidates.values()))


def campaign_label(campaign_at):
    return f"{campaign_at:%Y-%m-%d %H:%M %Z} ({campaign_at.tzinfo})"


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set BOT_TOKEN in .env to your existing bot token")
    campaign_at = parse_campaign(
        os.getenv("CAMPAIGN_DATE", "2026-10-22"),
        os.getenv("CAMPAIGN_TIME", "16:00"),
        os.getenv("CAMPAIGN_TIMEZONE", "Asia/Krasnoyarsk"),
    )
    path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3"))
    if not path.is_absolute():
        path = BASE_DIR / path
    validate_channels(CHANNELS)
    materials_url = os.getenv("MATERIALS_URL", "").strip()
    validate_materials(materials_url, MATERIALS_MESSAGE)
    return Settings(
        token=token,
        campaign_at=campaign_at,
        database_path=path,
        materials_url=materials_url,
        materials_message=MATERIALS_MESSAGE,
    )


def validate_channels(channels):
    if len(channels) != 6 or len({c.chat_id for c in channels}) != 6:
        raise ValueError("Configure exactly six distinct channels")
    for channel in channels:
        if "REPLACE" in str(channel.chat_id) or "REPLACE" in channel.url:
            raise ValueError("Replace Channels 2–6 in config.py before starting")
        if not channel.name or not channel.url.startswith("https://t.me/"):
            raise ValueError("Each channel requires a name and Telegram join URL")
        if not (isinstance(channel.chat_id, int) and channel.chat_id < 0) and not (
            isinstance(channel.chat_id, str) and channel.chat_id.startswith("@")
        ):
            raise ValueError("Channel chat_id must be a negative integer or @username")
