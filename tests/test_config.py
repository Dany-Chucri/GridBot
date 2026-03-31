"""Tests for configuration loading and defaults."""

from gridbot.config import AssetConfig, BotConfig, default_eth_config


class TestDefaults:
    def test_btc_defaults(self):
        cfg = AssetConfig()
        assert cfg.symbol == "BTC-PERP"
        assert cfg.leverage == 2.0
        assert cfg.levels_per_side == 25
        assert cfg.breakout_atr_distance == 4.5

    def test_eth_overrides(self):
        cfg = default_eth_config()
        assert cfg.symbol == "ETH-PERP"
        assert cfg.capital_allocation == 0.40
        assert cfg.breakout_atr_distance == 4.0
        assert cfg.expansion_range_atr == 3.5
        assert cfg.max_flatten_slippage_bps == 75.0

    def test_bot_config_has_both_assets(self):
        cfg = BotConfig()
        symbols = [a.symbol for a in cfg.assets]
        assert "BTC-PERP" in symbols
        assert "ETH-PERP" in symbols

    def test_testnet_endpoints(self):
        cfg = BotConfig(testnet=True)
        assert "testnet" in cfg.ws_url
        assert "testnet" in cfg.rest_info_url

    def test_mainnet_endpoints(self):
        cfg = BotConfig(testnet=False)
        assert "testnet" not in cfg.ws_url
