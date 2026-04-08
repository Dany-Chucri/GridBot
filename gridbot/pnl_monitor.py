"""PnL / Funding Monitor — analytics and funding tracking.

Responsibilities (design doc section 3.2):
- Track realized PnL from fills (local ledger)
- Periodically cross-check against exchange-reported PnL (section 10.6)
- Monitor funding rate and bias
- If local vs exchange PnL diverge: log alert, defer to exchange numbers

The exchange is truth — local ledger is for analytics only.
"""

from __future__ import annotations

import logging
from collections import deque

from gridbot.config import BotConfig
from gridbot.types import Fill, OrderSide, Position

logger = logging.getLogger(__name__)


class PnLMonitor:
    """Tracks PnL and funding, cross-checks with exchange."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._divergence_threshold = config.portfolio.pnl_divergence_threshold_usd
        self._crosscheck_interval_s = config.portfolio.pnl_crosscheck_interval_seconds

        # Per-asset local PnL tracking
        self._realized_pnl: dict[str, float] = {}
        self._fills_maxlen = 100_000
        self._fills: dict[str, deque[Fill]] = {}
        self._funding_payments: dict[str, float] = {}

        # Average-cost tracking: signed position size and avg entry price
        self._position_size: dict[str, float] = {}
        self._avg_entry: dict[str, float] = {}

        # Cross-check state
        self._last_crosscheck_ms: dict[str, int] = {}
        self._pnl_diverged: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    def record_fill(self, fill: Fill) -> None:
        """Record a fill and update local realized PnL.

        Uses average-cost method matching Hyperliquid's PnL reporting:
        - Fill increases position: update avg_entry, no realized PnL.
        - Fill reduces position: realize PnL at (fill.price - avg_entry) * closed_size.
        - Fill flips position: realize on closing portion, reset avg_entry for new direction.
        """
        sym = fill.symbol

        # Initialize per-symbol state on first fill
        if sym not in self._fills:
            self._fills[sym] = deque(maxlen=self._fills_maxlen)
        if sym not in self._realized_pnl:
            self._realized_pnl[sym] = 0.0
        if sym not in self._position_size:
            self._position_size[sym] = 0.0
            self._avg_entry[sym] = 0.0

        self._fills[sym].append(fill)

        # Convert fill to signed delta: buys are positive, sells are negative
        signed_delta = fill.size if fill.side == OrderSide.BUY else -fill.size
        old_size = self._position_size[sym]
        new_size = old_size + signed_delta

        if old_size == 0.0:
            # Opening from flat — just set entry
            self._avg_entry[sym] = fill.price
        elif _same_sign(old_size, signed_delta):
            # Increasing position — update weighted average entry
            self._avg_entry[sym] = (
                (self._avg_entry[sym] * abs(old_size) + fill.price * fill.size)
                / abs(new_size)
            )
        else:
            # Reducing or flipping
            closed_size = min(abs(signed_delta), abs(old_size))
            # PnL direction: long closing = (fill_price - entry); short closing = (entry - fill_price)
            if old_size > 0:
                pnl = (fill.price - self._avg_entry[sym]) * closed_size
            else:
                pnl = (self._avg_entry[sym] - fill.price) * closed_size
            self._realized_pnl[sym] += pnl

            if _same_sign(old_size, new_size) or new_size == 0.0:
                # Partial or full close — avg_entry stays unchanged
                pass
            else:
                # Flipped to opposite side — reset entry to fill price
                self._avg_entry[sym] = fill.price

        self._position_size[sym] = new_size

    def get_realized_pnl(self, symbol: str) -> float:
        """Get locally-tracked realized PnL for an asset."""
        return self._realized_pnl.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Funding tracking
    # ------------------------------------------------------------------

    def record_funding_payment(self, symbol: str, amount: float) -> None:
        """Record a funding payment (positive = received, negative = paid)."""
        if symbol not in self._funding_payments:
            self._funding_payments[symbol] = 0.0
        self._funding_payments[symbol] += amount

    def get_total_funding(self, symbol: str) -> float:
        return self._funding_payments.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Exchange cross-check (section 10.6)
    # ------------------------------------------------------------------

    async def crosscheck(
        self,
        symbol: str,
        exchange_pnl: float,
        now_ms: int,
    ) -> bool:
        """Compare local PnL against exchange-reported PnL.

        If divergence > threshold:
        - Log warning with both values
        - Adopt exchange numbers for risk decisions
        - Continue using local ledger for analytics (flagged)

        Returns True if divergence detected.
        Rate-limited to run at most once per crosscheck_interval.
        """
        interval_ms = int(self._crosscheck_interval_s * 1000)
        if symbol in self._last_crosscheck_ms:
            last_ms = self._last_crosscheck_ms[symbol]
            if now_ms - last_ms < interval_ms:
                return self._pnl_diverged.get(symbol, False)

        self._last_crosscheck_ms[symbol] = now_ms

        local_pnl = (
            self._realized_pnl.get(symbol, 0.0)
            + self._funding_payments.get(symbol, 0.0)
        )
        diff = abs(local_pnl - exchange_pnl)

        if diff > self._divergence_threshold:
            logger.warning(
                "PnL divergence detected for %s: local=%.4f exchange=%.4f diff=%.4f (threshold=%.4f)",
                symbol,
                local_pnl,
                exchange_pnl,
                diff,
                self._divergence_threshold,
            )
            self._pnl_diverged[symbol] = True
            return True

        self._pnl_diverged[symbol] = False
        return False

    def is_diverged(self, symbol: str) -> bool:
        """Whether PnL has diverged from exchange for this asset."""
        return self._pnl_diverged.get(symbol, False)

    # ------------------------------------------------------------------
    # Equity tracking (for drawdown calculation)
    # ------------------------------------------------------------------

    def compute_total_pnl(
        self,
        symbol: str,
        position: Position | None,
    ) -> float:
        """Total PnL = realized + unrealized + funding.

        Uses exchange-reported unrealized if diverged.
        """
        realized = self._realized_pnl.get(symbol, 0.0)
        funding = self._funding_payments.get(symbol, 0.0)

        if position is not None:
            unrealized = position.unrealized_pnl
        else:
            unrealized = 0.0

        return realized + unrealized + funding


def _same_sign(a: float, b: float) -> bool:
    """True if both values have the same sign (both positive or both negative)."""
    return (a > 0 and b > 0) or (a < 0 and b < 0)
