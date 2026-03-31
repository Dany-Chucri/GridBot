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
from gridbot.types import Fill, Position

logger = logging.getLogger(__name__)


class PnLMonitor:
    """Tracks PnL and funding, cross-checks with exchange."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._divergence_threshold = config.portfolio.pnl_divergence_threshold_usd
        self._crosscheck_interval_s = config.portfolio.pnl_crosscheck_interval_seconds

        # Per-asset local PnL tracking
        self._realized_pnl: dict[str, float] = {}
        self._fills: dict[str, deque[Fill]] = {}
        self._funding_payments: dict[str, float] = {}

        # Cross-check state
        self._last_crosscheck_ms: dict[str, int] = {}
        self._pnl_diverged: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Fill processing
    # ------------------------------------------------------------------

    def record_fill(self, fill: Fill) -> None:
        """Record a fill and update local realized PnL."""
        raise NotImplementedError

    def get_realized_pnl(self, symbol: str) -> float:
        """Get locally-tracked realized PnL for an asset."""
        return self._realized_pnl.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Funding tracking
    # ------------------------------------------------------------------

    def record_funding_payment(self, symbol: str, amount: float) -> None:
        """Record a funding payment (positive = received, negative = paid)."""
        raise NotImplementedError

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
        """
        raise NotImplementedError

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
        raise NotImplementedError
