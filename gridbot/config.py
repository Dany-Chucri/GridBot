"""Configuration loading and parameter definitions.

Default values from design doc sections 11.1-11.3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Endpoint configuration
# ---------------------------------------------------------------------------

MAINNET_BASE = "https://api.hyperliquid.xyz"
MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
MAINNET_REST_INFO = "https://api.hyperliquid.xyz/info"
MAINNET_REST_EXCHANGE = "https://api.hyperliquid.xyz/exchange"

TESTNET_BASE = "https://api.hyperliquid-testnet.xyz"
TESTNET_WS = "wss://api.hyperliquid-testnet.xyz/ws"
TESTNET_REST_INFO = "https://api.hyperliquid-testnet.xyz/info"
TESTNET_REST_EXCHANGE = "https://api.hyperliquid-testnet.xyz/exchange"


# ---------------------------------------------------------------------------
# Per-asset configuration
# ---------------------------------------------------------------------------

@dataclass
class AssetConfig:
    """Parameters for a single asset's grid (design doc sections 11.1-11.2)."""

    symbol: str = "BTC-PERP"

    # Core Grid
    leverage: float = 2.0
    levels_per_side: int = 25
    grid_step_bps_min: float = 15.0
    grid_step_bps_max: float = 25.0
    capital_allocation: float = 0.70

    # Expansion Grid
    expansion_levels_per_side: int = 15
    expansion_step_mult: float = 1.5
    expansion_range_atr: float = 4.0
    expansion_allocation: float = 0.30

    # Anchoring
    anchor_shift_threshold_atr: float = 1.5
    anchor_delay_minutes: float = 30.0

    # Inventory
    soft_cap_pct: float = 0.50
    # Derived by RiskManager.preflight_check from this asset's share of the
    # shared risk budget (section 9.1), total_risk_budget_pct * asset_weight
    # * capital_allocation * leverage, converted to units via mid_price at
    # startup. Not a settable config value; any value here is overwritten.
    max_abs_position: float = 0.0

    # Breakout
    breakout_atr_distance: float = 4.5
    cooldown_minutes: float = 30.0

    # Volatility circuit breakers
    vol_pause_percentile: float = 0.80   # Top 20th percentile
    vol_kill_percentile: float = 0.95    # Top 5th percentile
    vol_recovery_minutes: float = 10.0

    # Funding
    funding_moderate_threshold: float = 0.30   # 30% annualized
    funding_extreme_threshold: float = 1.00    # 100% annualized

    # Drawdown
    max_daily_drawdown_pct: float = 0.03
    max_weekly_drawdown_pct: float = 0.07

    # Order execution
    post_only_max_retries: int = 3
    tick_size: float = 1.0  # USD, per-asset tick size for ALO price nudging
    sz_decimals: int = 5  # Hyperliquid szDecimals; order sizes snap to this

    # Dynamic slippage (section 5.7)
    base_slippage_bps: float = 1.5
    vol_slippage_scale: float = 2.0
    min_slippage_bps: float = 1.0
    max_slippage_bps: float = 10.0
    depth_impact_scale: float = 1.0

    # Emergency flatten (section 6.7)
    max_flatten_slippage_bps: float = 50.0
    flatten_time_budget_seconds: float = 10.0
    flatten_tranche_pct: float = 0.80
    flatten_retry_pause_ms: float = 300.0

    # Server-side backstop (section 6.8)
    backstop_buffer_atr: float = 1.0


# ETH-PERP overrides (section 11.2)
def default_eth_config() -> AssetConfig:
    return AssetConfig(
        symbol="ETH-PERP",
        capital_allocation=0.40,
        grid_step_bps_min=18.0,
        grid_step_bps_max=30.0,
        expansion_range_atr=3.5,
        breakout_atr_distance=4.0,
        max_flatten_slippage_bps=75.0,
        tick_size=0.01,
        sz_decimals=4,
    )


# ---------------------------------------------------------------------------
# Portfolio-level configuration (section 11.3)
# ---------------------------------------------------------------------------

@dataclass
class PortfolioConfig:
    total_risk_budget_pct: float = 0.10
    btc_weight: float = 0.60
    eth_weight: float = 0.40
    portfolio_delta_mult: float = 0.75
    pnl_crosscheck_interval_seconds: float = 60.0
    pnl_divergence_threshold_usd: float = 50.0


# ---------------------------------------------------------------------------
# Operational configuration
# ---------------------------------------------------------------------------

@dataclass
class OperationalConfig:
    cycle_interval_seconds: float = 1.0
    reconcile_interval_seconds: float = 5.0
    max_consecutive_errors: int = 10
    max_time_desynced_seconds: float = 30.0
    ws_reconnect_stale_seconds: float = 10.0
    stagger_initial_levels: int = 5


# ---------------------------------------------------------------------------
# Alerting configuration (design doc section 10.3)
# ---------------------------------------------------------------------------

@dataclass
class AlertingConfig:
    """Non-secret alert-channel settings. Secrets (bot token, webhook URL) are
    read from the environment only, see gridbot/alerting.py and
    deploy/gridbot.env.example.
    """

    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    discord_enabled: bool = False
    min_severity: str = "WARNING"


# ---------------------------------------------------------------------------
# Top-level bot configuration
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    testnet: bool = False
    wallet_address: str = ""
    assets: list[AssetConfig] = field(default_factory=lambda: [AssetConfig(), default_eth_config()])
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    operational: OperationalConfig = field(default_factory=OperationalConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)

    @property
    def base_url(self) -> str:
        return TESTNET_BASE if self.testnet else MAINNET_BASE

    @property
    def ws_url(self) -> str:
        return TESTNET_WS if self.testnet else MAINNET_WS

    @property
    def rest_info_url(self) -> str:
        return TESTNET_REST_INFO if self.testnet else MAINNET_REST_INFO

    @property
    def rest_exchange_url(self) -> str:
        return TESTNET_REST_EXCHANGE if self.testnet else MAINNET_REST_EXCHANGE


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path, testnet: bool = False) -> BotConfig:
    """Load configuration from a YAML file, falling back to defaults."""
    config = BotConfig(testnet=testnet)

    if path.exists():
        logger.info("Loading config from %s", path)
        with open(path) as f:
            raw = yaml.safe_load(f)
        if raw:
            _apply_overrides(config, raw)
    else:
        logger.warning("Config file %s not found, using defaults", path)

    return config


def _apply_overrides(config: BotConfig, raw: dict) -> None:
    """Apply YAML overrides onto the default BotConfig.

    Keeps it simple: top-level keys map to config sections,
    nested keys map to dataclass fields.
    """
    if "testnet" in raw:
        config.testnet = bool(raw["testnet"])

    if "wallet_address" in raw:
        config.wallet_address = str(raw["wallet_address"])

    if "portfolio" in raw:
        for k, v in raw["portfolio"].items():
            if hasattr(config.portfolio, k):
                setattr(config.portfolio, k, v)

    if "operational" in raw:
        for k, v in raw["operational"].items():
            if hasattr(config.operational, k):
                setattr(config.operational, k, v)

    if "alerting" in raw:
        alerting_raw = raw["alerting"]
        if "telegram" in alerting_raw:
            telegram_raw = alerting_raw["telegram"]
            if "enabled" in telegram_raw:
                config.alerting.telegram_enabled = bool(telegram_raw["enabled"])
            if "chat_id" in telegram_raw:
                config.alerting.telegram_chat_id = str(telegram_raw["chat_id"])
        if "discord" in alerting_raw:
            discord_raw = alerting_raw["discord"]
            if "enabled" in discord_raw:
                config.alerting.discord_enabled = bool(discord_raw["enabled"])
        if "min_severity" in alerting_raw:
            config.alerting.min_severity = str(alerting_raw["min_severity"])

    if "assets" in raw:
        # The YAML `assets:` list is authoritative for which assets run -
        # replace config.assets entirely rather than merging onto BotConfig's
        # default two-asset list. Otherwise an asset omitted from the config
        # (e.g. a single-asset testnet soak) silently survives with full
        # default parameters instead of being excluded.
        existing_by_symbol = {a.symbol: a for a in config.assets}
        new_assets: list[AssetConfig] = []
        for asset_raw in raw["assets"]:
            symbol = asset_raw.get("symbol", "BTC-PERP")
            # Reuse the matching default (preserves asset-specific defaults
            # like ETH's tighter breakout distance) if one exists, else start
            # from a bare AssetConfig for the given symbol.
            target = existing_by_symbol.get(symbol) or AssetConfig(symbol=symbol)
            for k, v in asset_raw.items():
                if hasattr(target, k):
                    setattr(target, k, v)
            new_assets.append(target)
        config.assets = new_assets
