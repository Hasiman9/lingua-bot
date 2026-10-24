"""Shared token redaction for the bot and read-only diagnostics."""
import logging
import re


class RedactingFormatter(logging.Formatter):
    token = ""

    def format(self, record):
        text = super().format(record)
        if self.token:
            text = text.replace(self.token, "<BOT_TOKEN_REDACTED>")
        return re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", "<BOT_TOKEN_REDACTED>", text)
