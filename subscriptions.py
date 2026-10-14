"""Membership failures and Telegram request failures are distinct outcomes."""
from dataclasses import dataclass, field
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import config

logger = logging.getLogger(__name__)


@dataclass
class Verification:
    missing: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def passed(self):
        return not self.missing and not self.errors


def is_member(member):
    return member.status in {"member", "administrator", "creator", "owner"} or (
        member.status == "restricted" and member.is_member is True
    )


async def verify(bot, user_id, channels):
    if len(channels) != 6:
        raise ValueError("Verification requires all six channels")
    result = Verification()
    for channel in channels:
        try:
            # Check our own rights as well: getChatMember is only guaranteed for admins.
            own = await bot.get_chat_member(chat_id=channel.chat_id, user_id=bot.id)
            member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
            if own.status not in {"administrator", "creator", "owner"}:
                result.errors.append(channel)
                logger.warning("Bot lacks administrator rights in channel %s", channel.name)
            elif not is_member(member):
                result.missing.append(channel)
        except TelegramError as exc:
            result.errors.append(channel)
            logger.warning("Membership API error in %s (%s)", channel.name, type(exc).__name__)
    return result


def keyboard(channels, retry=False):
    rows = [[InlineKeyboardButton(c.name, url=c.url)] for c in channels]
    rows.append([InlineKeyboardButton(
        config.RETRY_LABEL if retry else config.CONFIRM_LABEL, callback_data="verify"
    )])
    return InlineKeyboardMarkup(rows)


async def show_failure(bot, user_id, result):
    text = config.MISSING_TEXT if result.missing else config.API_ERROR_TEXT
    if result.missing and result.errors:
        text += "\n\n" + config.API_ERROR_TEXT
    await bot.send_message(chat_id=user_id, text=text,
                           reply_markup=keyboard(result.missing, retry=True))
