"""Tests for configuration loading and defaults."""

import yaml

from gridbot.config import AssetConfig, BotConfig, default_eth_config, load_config


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


class TestLoadConfigAssets:
    """The YAML `assets:` list must be authoritative (regression: a
    single-asset config used to silently leave the other asset running with
    full default parameters, since BotConfig defaults to both)."""

    def _write(self, tmp_path, raw: dict):
        path = tmp_path / "gridbot.yaml"
        path.write_text(yaml.dump(raw))
        return path

    def test_single_asset_excludes_the_other(self, tmp_path):
        path = self._write(tmp_path, {
            "assets": [{"symbol": "BTC-PERP", "leverage": 2.0}],
        })
        cfg = load_config(path)
        symbols = [a.symbol for a in cfg.assets]
        assert symbols == ["BTC-PERP"]

    def test_listed_asset_keeps_its_specific_defaults(self, tmp_path):
        # ETH's own defaults (e.g. tighter breakout distance, higher flatten
        # slippage) must survive being listed, not fall back to bare
        # AssetConfig() generic defaults.
        path = self._write(tmp_path, {
            "assets": [{"symbol": "ETH-PERP", "leverage": 3.0}],
        })
        cfg = load_config(path)
        assert len(cfg.assets) == 1
        eth = cfg.assets[0]
        assert eth.leverage == 3.0  # overridden
        assert eth.max_flatten_slippage_bps == 75.0  # ETH default, preserved

    def test_both_assets_listed(self, tmp_path):
        path = self._write(tmp_path, {
            "assets": [
                {"symbol": "BTC-PERP"},
                {"symbol": "ETH-PERP"},
            ],
        })
        cfg = load_config(path)
        symbols = [a.symbol for a in cfg.assets]
        assert symbols == ["BTC-PERP", "ETH-PERP"]

    def test_no_assets_key_keeps_defaults(self, tmp_path):
        path = self._write(tmp_path, {"testnet": True})
        cfg = load_config(path)
        symbols = [a.symbol for a in cfg.assets]
        assert "BTC-PERP" in symbols
        assert "ETH-PERP" in symbols
