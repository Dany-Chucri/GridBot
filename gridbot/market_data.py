"""MarketData — real-time market view via WS + REST fallback.

Responsibilities (design doc section 3.2):
- Subscribe to WS mid/mark price and trade streams
- Subscribe to WS orderUpdates for real-time fill notifications
- Compute rolling returns, realized vol, ATR proxy, bid-ask spread
- Expose latest prices and vol metrics to other modules

Dual-path state updates (section 4.3):
- WS orderUpdates is the PRIMARY state driver
- REST reconciliation is the BACKUP consistency check
- Both paths update the same state; must be serialized to prevent races
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from gridbot.config import AssetConfig, BotConfig
from gridbot.types import Fill, OpenOrder, Position, VolMetrics

logger = logging.getLogger(__name__)


class MarketData:
    """Maintains the real-time market view for all assets."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()

        # Per-asset latest prices
        self._mid_prices: dict[str, float] = {}
        self._mark_prices: dict[str, float] = {}
        self._best_bid: dict[str, float] = {}
        self._best_ask: dict[str, float] = {}

        # Per-asset vol computation buffers
        self._return_buffers: dict[str, deque[float]] = {}
        self._minute_candles: dict[str, deque[dict]] = {}

        # Connection state
        self._ws_connected = False
        self._last_ws_message_ms: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish WS connections and subscribe to channels."""
        raise NotImplementedError

    async def disconnect(self) -> None:
        """Cleanly close WS connections."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # WS message handlers
    # ------------------------------------------------------------------

    async def _handle_price_update(self, symbol: str, bid: float, ask: float) -> None:
        """Process a new best bid/ask update."""
        raise NotImplementedError

    async def _handle_trade(self, symbol: str, price: float, size: float, timestamp_ms: int) -> None:
        """Process a trade message for vol/return calculation."""
        raise NotImplementedError

    async def _handle_order_update(self, raw: dict) -> Fill | None:
        """Process a WS orderUpdate — the PRIMARY fill detection path.

        Returns a Fill if a full fill occurred, None otherwise.
        Updates local position tracking on any fill/partial fill.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # REST backup path (section 4.3)
    # ------------------------------------------------------------------

    async def fetch_open_orders(self, symbol: str) -> list[OpenOrder]:
        """REST fetch of current open orders — backup consistency check."""
        raise NotImplementedError

    async def fetch_position(self, symbol: str) -> Position | None:
        """REST fetch of current position — backup consistency check."""
        raise NotImplementedError

    async def fetch_exchange_pnl(self, symbol: str) -> float:
        """REST fetch of exchange-reported unrealized PnL (section 10.6)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Derived metrics (section 4.2)
    # ------------------------------------------------------------------

    def get_mid_price(self, symbol: str) -> float:
        """Latest mid price for the asset."""
        return self._mid_prices.get(symbol, 0.0)

    def get_mark_price(self, symbol: str) -> float:
        """Latest mark price for the asset."""
        return self._mark_prices.get(symbol, 0.0)

    def compute_vol_metrics(self, symbol: str) -> VolMetrics:
        """Compute current volatility metrics from buffered data."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # L2 book depth (for flatten slippage estimation, section 5.7)
    # ------------------------------------------------------------------

    async def fetch_book_depth(self, symbol: str, depth_bps: float = 50.0) -> float:
        """Get available depth within depth_bps of mid price.

        Used by the emergency flatten protocol (section 6.7) for
        pre-flatten depth assessment.
        """
        raise NotImplementedError
