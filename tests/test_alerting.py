from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gridbot.alerting import (
    DISCORD_WEBHOOK_URL_ENV,
    TELEGRAM_BOT_TOKEN_ENV,
    build_alert_callback,
)
from gridbot.config import AlertingConfig


def _mock_session(status: int = 200):
    """Build a MagicMock standing in for aiohttp.ClientSession() as an async CM."""
    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value="body")
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


class TestBuildAlertCallback:
    def test_no_channels_enabled_returns_none(self):
        assert build_alert_callback(AlertingConfig()) is None

    def test_telegram_enabled_without_token_returns_none(self, monkeypatch):
        monkeypatch.delenv(TELEGRAM_BOT_TOKEN_ENV, raising=False)
        config = AlertingConfig(telegram_enabled=True, telegram_chat_id="123")
        assert build_alert_callback(config) is None

    def test_discord_enabled_without_webhook_returns_none(self, monkeypatch):
        monkeypatch.delenv(DISCORD_WEBHOOK_URL_ENV, raising=False)
        config = AlertingConfig(discord_enabled=True)
        assert build_alert_callback(config) is None


class TestTelegramDelivery:
    @pytest.mark.asyncio
    async def test_sends_message_with_token_and_chat_id(self, monkeypatch):
        monkeypatch.setenv(TELEGRAM_BOT_TOKEN_ENV, "abc123")
        config = AlertingConfig(telegram_enabled=True, telegram_chat_id="999")
        session_cm, session = _mock_session()

        with patch("gridbot.alerting.aiohttp.ClientSession", return_value=session_cm):
            cb = build_alert_callback(config)
            assert cb is not None
            await cb("CRITICAL", "kill switch fired")

        url, kwargs = session.post.call_args
        assert "abc123" in url[0]
        assert kwargs["json"]["chat_id"] == "999"
        assert "kill switch fired" in kwargs["json"]["text"]

    @pytest.mark.asyncio
    async def test_below_min_severity_is_not_sent(self, monkeypatch):
        monkeypatch.setenv(TELEGRAM_BOT_TOKEN_ENV, "abc123")
        config = AlertingConfig(
            telegram_enabled=True, telegram_chat_id="999", min_severity="CRITICAL"
        )
        session_cm, session = _mock_session()

        with patch("gridbot.alerting.aiohttp.ClientSession", return_value=session_cm):
            cb = build_alert_callback(config)
            await cb("WARNING", "reconcile discrepancy")

        session.post.assert_not_called()


class TestDiscordDelivery:
    @pytest.mark.asyncio
    async def test_sends_message_to_webhook(self, monkeypatch):
        monkeypatch.setenv(DISCORD_WEBHOOK_URL_ENV, "https://discord.example/webhook")
        config = AlertingConfig(discord_enabled=True)
        session_cm, session = _mock_session()

        with patch("gridbot.alerting.aiohttp.ClientSession", return_value=session_cm):
            cb = build_alert_callback(config)
            assert cb is not None
            await cb("WARNING", "pnl divergence")

        args, kwargs = session.post.call_args
        assert args[0] == "https://discord.example/webhook"
        assert "pnl divergence" in kwargs["json"]["content"]


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_channel_failure_does_not_raise(self, monkeypatch):
        monkeypatch.setenv(DISCORD_WEBHOOK_URL_ENV, "https://discord.example/webhook")
        config = AlertingConfig(discord_enabled=True)

        with patch("gridbot.alerting.aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            cb = build_alert_callback(config)
            await cb("CRITICAL", "should not raise")
