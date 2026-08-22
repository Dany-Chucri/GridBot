"""Shared types, enums, and dataclasses used across all modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Regime(Enum):
    """Market regime states (design doc section 8.1)."""
    RANGE = auto()
    TREND = auto()
    HIGH_VOL = auto()
    UNKNOWN = auto()


class BotState(Enum):
    """Top-level bot operating states."""
    STARTING = auto()      # Pre-flight checks and reconciliation
    RUNNING = auto()       # Normal grid operation
    COOLDOWN = auto()      # Post-breakout/vol-kill cooldown
    FLATTENING = auto()    # Emergency flatten in progress (section 6.7)
    MAINTENANCE = auto()   # Exchange maintenance detected
    DEAD = auto()          # Kill switch fired, requires manual restart
    SHUTTING_DOWN = auto() # Graceful shutdown in progress


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class GridLayer(Enum):
    """Grid layer identifiers (design doc section 5.2)."""
    CORE = auto()
    EXPANSION = auto()


class TimeInForce(Enum):
    GTC = "gtc"       # Good-Til-Cancel, resting grid orders
    IOC = "ioc"       # Immediate-Or-Cancel, emergency flatten only
    ALO = "alo"       # Post-Only / Add Liquidity Only


class InventoryZone(Enum):
    """Inventory cap zones (design doc section 6.2)."""
    NORMAL = auto()    # abs(pos) < soft_cap
    SOFT_CAP = auto()  # soft_cap <= abs(pos) < hard_cap
    HARD_CAP = auto()  # abs(pos) >= hard_cap


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GridLevel:
    """A single grid level in the desired order set."""
    price: float
    side: OrderSide
    size: float
    layer: GridLayer
    reduce_only: bool = False


@dataclass(frozen=True)
class DesiredOrder:
    """An order the GridEngine wants on the exchange."""
    client_order_id: str
    symbol: str
    price: float
    size: float
    side: OrderSide
    time_in_force: TimeInForce = TimeInForce.ALO
    reduce_only: bool = False


@dataclass
class OpenOrder:
    """An order currently resting on the exchange."""
    order_id: int
    client_order_id: str
    symbol: str
    price: float
    size: float
    remaining: float
    side: OrderSide
    reduce_only: bool = False


@dataclass
class Fill:
    """A single fill event."""
    fill_id: str
    order_id: int
    client_order_id: str
    symbol: str
    price: float
    size: float
    side: OrderSide
    fee: float
    timestamp_ms: int
    is_maker: bool
    is_partial: bool = False


@dataclass
class Position:
    """Current position for a single asset."""
    symbol: str
    size: float              # Signed: positive = long, negative = short
    avg_entry_price: float
    unrealized_pnl: float
    liquidation_price: float | None = None


@dataclass
class PendingFlip:
    """A flip order that must persist across re-anchoring (section 7.6)."""
    price: float
    side: OrderSide
    size: float
    originating_fill_id: str


@dataclass
class GridConfig:
    """Snapshot of grid parameters used for deterministic order IDs."""
    symbol: str
    anchor: float
    range_atr: float
    step_bps: float
    epoch: int


@dataclass
class VolMetrics:
    """Volatility and derived metrics from MarketData."""
    realized_vol: float        # Annualized realized volatility
    atr: float                 # ATR proxy from minute candles
    spread_bps: float          # Current bid-ask spread in basis points
    rolling_return_1m: float   # 1-minute rolling return
    rolling_return_5m: float   # 5-minute rolling return
    baseline_vol: float = 0.0  # Trailing 7d median vol (reference for "normal")


@dataclass
class AssetState:
    """Aggregated state for a single asset, passed between modules."""
    symbol: str
    mid_price: float = 0.0
    mark_price: float = 0.0
    regime: Regime = Regime.UNKNOWN
    bot_state: BotState = BotState.STARTING
    position: Position | None = None
    vol_metrics: VolMetrics | None = None
    grid_config: GridConfig | None = None
    open_orders: list[OpenOrder] = field(default_factory=list)
    pending_flips: list[PendingFlip] = field(default_factory=list)
    cooldown_until_ms: int | None = None
    account_equity: float = 0.0
    funding_rate: float = 0.0
    moving_avg: float = 0.0
    last_breakout_ms: int | None = None
    stagger_placed_count: int = 0
    drift_start_ms: int | None = None
    anchor_epoch: int = 0
    force_reduce_only: bool = False  # Portfolio delta cap breach (section 9.2)
