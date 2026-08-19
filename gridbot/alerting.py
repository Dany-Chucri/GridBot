"""Alert delivery channels (design doc section 10.3).

Supervisor's alert hook (`gridbot.supervisor.AlertCallback`) is transport-agnostic
— it just calls `Callable[[severity, message], Awaitable[None]]`. This module
supplies the actual senders (Telegram, Discord) and wires them into a single
callback from `AlertingConfig` plus secrets read from the environment.

Secrets (bot token, webhook URL) are never read from the YAML config — only
from environment variables, matching how `GRIDBOT_PRIVATE_KEY` is handled
(see deploy/gridbot.env.example).
"""

from __future__ import annotations

import logging
import os

import aiohttp

from gridbot.config import AlertingConfig
from gridbot.supervisor import AlertCallback

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN_ENV = "GRIDBOT_TELEGRAM_BOT_TOKEN"
DISCORD_WEBHOOK_URL_ENV = "GRIDBOT_DISCORD_WEBHOOK_URL"

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10.0)


async def _send_telegram(bot_token: str, chat_id: str, severity: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": f"[{severity}] GridBot: {message}"}
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Telegram alert failed (%s): %s", resp.status, body)


async def _send_discord(webhook_url: str, severity: str, message: str) -> None:
    payload = {"content": f"**[{severity}]** GridBot: {message}"}
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
        async with session.post(webhook_url, json=payload) as resp:
            if resp.status not in (200, 204):
                body = await resp.text()
                logger.error("Discord alert failed (%s): %s", resp.status, body)


def build_alert_callback(config: AlertingConfig) -> AlertCallback | None:
    """Build a combined alert callback from configured channels.

    Returns None if no channel is both enabled in config and has its secret
    present in the environment — callers should treat that as "alerting not
    configured" and log a warning, not silently proceed.
    """
    senders: list[AlertCallback] = []
    min_severity = _SEVERITY_ORDER.get(config.min_severity.upper(), 0)

    if config.telegram_enabled:
        bot_token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV, "")
        if bot_token and config.telegram_chat_id:
            async def _telegram(severity: str, message: str, _token=bot_token, _chat=config.telegram_chat_id) -> None:
                await _send_telegram(_token, _chat, severity, message)
            senders.append(_telegram)
        else:
            logger.warning(
                "Telegram alerting enabled but %s or alerting.telegram.chat_id is missing",
                TELEGRAM_BOT_TOKEN_ENV,
            )

    if config.discord_enabled:
        webhook_url = os.environ.get(DISCORD_WEBHOOK_URL_ENV, "")
        if webhook_url:
            async def _discord(severity: str, message: str, _url=webhook_url) -> None:
                await _send_discord(_url, severity, message)
            senders.append(_discord)
        else:
            logger.warning(
                "Discord alerting enabled but %s is missing", DISCORD_WEBHOOK_URL_ENV
            )

    if not senders:
        return None

    async def _combined(severity: str, message: str) -> None:
        if _SEVERITY_ORDER.get(severity.upper(), 0) < min_severity:
            logger.debug(
                "Alert suppressed by min_severity=%s: [%s] %s",
                config.min_severity, severity, message,
            )
            return
        for send in senders:
            try:
                await send(severity, message)
            except Exception:
                logger.exception("Alert channel failed")

    return _combined
