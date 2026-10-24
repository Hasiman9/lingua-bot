"""Membership failures and Telegram request failures are distinct outcomes."""
from dataclasses import dataclass, field
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut

import config

logger = logging.getLogger(__name__)
ADMIN_STATUSES = {"administrator", "creator", "owner"}


def api_error_details(error):
    """Return allowlisted details, never arbitrary exception text/URLs/credentials.

    Telegram's 'chat not found' cannot distinguish a bad ID from missing access.
    """
    text = str(error).lower()
    if isinstance(error, TimedOut):
        return "timeout", "Telegram request timed out"
    if isinstance(error, InvalidToken):
        return "authentication_error", "Telegram rejected the bot credentials"
    if isinstance(error, RetryAfter):
        return "rate_limited", "Telegram requested a retry later"
    if any(value in text for value in ("chat_admin_required", "not enough rights", "administrator rights", "member list is inaccessible")):
        return "insufficient_admin", "Telegram requires administrator access to membership information"
    if any(value in text for value in ("chat not found", "chat_id_invalid", "peer_id_invalid", "username_not_occupied", "username not found")):
        return "channel_id_or_access", "Channel ID/username is incorrect or the channel is inaccessible to this bot"
    if isinstance(error, Forbidden):
        return "access_denied", "Telegram denied access; check whether the bot was removed or banned"
    if isinstance(error, BadRequest):
        if any(value in text for value in ("user not found", "user_id_invalid", "participant_id_invalid")):
            return "invalid_user", "Telegram could not resolve the requested user"
        return "api_bad_request", "Telegram rejected the membership request"
    if isinstance(error, NetworkError):
        return "network_error", "Telegram transport failed; check network/proxy connectivity"
    return "api_error", "Telegram API request failed"


@dataclass
class MembershipCheck:
    status: str | None
    category: str
    reason: str
    member: object = None
    exception_type: str | None = None


async def check_membership(bot, channel, user_id, role):
    """One logged, read-only getChatMember call, shared by verification and diagnostics."""
    started = time.monotonic()
    logger.info("get_chat_member channel_id=%s role=%s phase=request", channel.chat_id, role)
    try:
        member = await bot.get_chat_member(chat_id=channel.chat_id, user_id=user_id)
    except TelegramError as exc:
        category, reason = api_error_details(exc)
        logger.warning(
            "get_chat_member channel_id=%s role=%s status=unavailable category=%s exception=%s reason=%s elapsed_ms=%d",
            channel.chat_id, role, category, type(exc).__name__, reason,
            int((time.monotonic() - started) * 1000),
        )
        return MembershipCheck(None, category, reason, exception_type=type(exc).__name__)
    if role == "bot":
        category = "ok" if member.status in ADMIN_STATUSES else "insufficient_admin"
        reason = "Bot is an administrator" if category == "ok" else "Bot is not an administrator in this channel"
    else:
        category = "ok" if is_member(member) else "missing_subscription"
        reason = "User is subscribed" if category == "ok" else "User has no active subscription"
    log = logger.info if category == "ok" else logger.warning
    log("get_chat_member channel_id=%s role=%s status=%s is_member=%s category=%s reason=%s elapsed_ms=%d",
        channel.chat_id, role, member.status, getattr(member, "is_member", None), category, reason,
        int((time.monotonic() - started) * 1000))
    return MembershipCheck(member.status, category, reason, member=member)


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
        own = await check_membership(bot, channel, bot.id, "bot")
        if own.member is None:
            result.errors.append(channel)
            logger.info("verification channel_id=%s subscriber_check=skipped reason=bot_access_failed", channel.chat_id)
            continue
        member = await check_membership(bot, channel, user_id, "subscriber")
        # A non-admin bot's user lookup is not trustworthy, even if it returned a status.
        if own.category != "ok" or member.member is None:
            result.errors.append(channel)
        elif member.category == "missing_subscription":
            result.missing.append(channel)
    logger.info("verification summary checked_channels=%d missing=%d errors=%d passed=%s",
                len(channels), len(result.missing), len(result.errors), result.passed)
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
