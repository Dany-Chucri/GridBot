"""RiskManager — enforces all safety constraints.

Responsibilities (design doc section 3.2):
- Leverage and liquidation buffer checks (section 6.1)
- Inventory cap enforcement: soft cap -> skew, hard cap -> reduce-only (section 6.2)
- Breakout detection with cancel + flatten (section 6.3)
- Volatility circuit breakers: pause and kill (section 6.4)
- Funding rate monitoring: two-tier skew/pause (section 6.5)
- Drawdown limits: daily (rolling 24h), weekly (rolling 168h) (section 6.6)
- Portfolio-level exposure cap across assets (section 9.2)
- Momentum micro-filter (section 8.3)

This module makes NO orders. It returns risk decisions that the Supervisor acts on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto

from gridbot.config import AssetConfig, BotConfig, PortfolioConfig
from gridbot.types import AssetState, Position, Regime, VolMetrics

logger = logging.getLogger(__name__)


class RiskAction(Enum):
    """Actions the RiskManager can recommend."""
    CONTINUE = auto()           # All clear, proceed with grid
    SKEW_INVENTORY = auto()     # Soft cap hit — skew order sizes
    REDUCE_ONLY = auto()        # Hard cap hit — reduce-only orders
    SKEW_FUNDING = auto()       # Moderate funding — bias grid
    PAUSE_GRID = auto()         # Vol pause or extreme funding — no new orders
    SUPPRESS_NEW_ENTRIES = auto()  # Momentum micro-filter — keep existing, no new
    CANCEL_AND_FLATTEN = auto() # Breakout / vol kill / drawdown — emergency
    KILL = auto()               # Dead state — manual restart required


@dataclass
class RiskDecision:
    """Result of a risk evaluation cycle."""
    action: RiskAction
    reason: str
    details: dict | None = None


class RiskManager:
    """Evaluates all risk constraints and returns a decision per cycle."""

    def __init__(self, config: BotConfig) -> None:
        self._config = config
        self._portfolio_config = config.portfolio

        # Rolling drawdown tracking
        self._pnl_history: list[tuple[int, float]] = []  # (timestamp_ms, equity)

        # Error tracking
        self._consecutive_errors: int = 0
        self._last_sync_ms: int = 0

        # Vol history for percentile calculation
        self._vol_history: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Main entry point — called every cycle
    # ------------------------------------------------------------------

    def evaluate(self, state: AssetState) -> RiskDecision:
        """Run all risk checks and return the most restrictive action.

        Check order (most severe first):
        1. Drawdown limits (section 6.6)
        2. Consecutive errors / desync
        3. Breakout detection (section 6.3)
        4. Volatility circuit breakers (section 6.4)
        5. Funding rate (section 6.5)
        6. Inventory caps (section 6.2)
        7. Momentum micro-filter (section 8.3)
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Individual risk checks
    # ------------------------------------------------------------------

    def _check_drawdown(self, symbol: str, current_equity: float) -> RiskDecision | None:
        """Rolling 24h / 168h drawdown check (section 6.6).

        Includes both realized and unrealized PnL.
        Uses rolling windows, not calendar-based.
        """
        raise NotImplementedError

    def _check_errors(self) -> RiskDecision | None:
        """Check consecutive error count and desync duration."""
        raise NotImplementedError

    def _check_breakout(
        self,
        mid_price: float,
        anchor: float,
        atr: float,
        vol_metrics: VolMetrics,
        config: AssetConfig,
    ) -> RiskDecision | None:
        """Breakout detection (section 6.3).

        Triggers on any of:
        - abs(mid - anchor) > breakout_atr_distance * ATR
        - abs(return_5m) > return_threshold
        - Realized vol spike
        """
        raise NotImplementedError

    def _check_volatility(
        self,
        symbol: str,
        vol_metrics: VolMetrics,
        config: AssetConfig,
    ) -> RiskDecision | None:
        """Volatility circuit breakers (section 6.4).

        vol_pause_threshold -> stop placing new orders.
        vol_kill_threshold -> cancel all + flatten.
        """
        raise NotImplementedError

    def _check_funding(
        self,
        funding_rate: float,
        position: Position | None,
        config: AssetConfig,
    ) -> RiskDecision | None:
        """Two-tier funding check (section 6.5).

        Moderate: skew grid sizes.
        Extreme + wrong-side inventory: pause.
        """
        raise NotImplementedError

    def _check_inventory(
        self,
        position: Position | None,
        config: AssetConfig,
    ) -> RiskDecision | None:
        """Inventory cap enforcement (section 6.2)."""
        raise NotImplementedError

    def _check_momentum(self, vol_metrics: VolMetrics) -> RiskDecision | None:
        """Momentum micro-filter (section 8.3).

        Suppress new entries if:
        - abs(price_change_5m) > 1.2 * ATR
        - abs(price_change_1m) > 0.5 * ATR
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Portfolio-level checks (section 9.2)
    # ------------------------------------------------------------------

    def check_portfolio_delta(
        self,
        positions: dict[str, Position],
    ) -> bool:
        """Check if portfolio delta exceeds the portfolio cap.

        portfolio_delta = abs(sum of position_usd across assets)
        Returns True if cap is breached.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Regime detection (section 8)
    # ------------------------------------------------------------------

    def detect_regime(
        self,
        symbol: str,
        mid_price: float,
        vol_metrics: VolMetrics,
        moving_avg: float,
        last_breakout_ms: int | None,
        now_ms: int,
        config: AssetConfig,
    ) -> Regime:
        """Determine current market regime (section 8.1-8.2).

        RANGE requires ALL signals to agree:
        1. Vol below pause threshold
        2. Price within X * ATR of moving average
        3. No recent breakout within cooldown window
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Pre-flight checks (section 6.1)
    # ------------------------------------------------------------------

    def preflight_check(self, config: AssetConfig, account_equity: float) -> list[str]:
        """Validate that the configuration is safe before starting.

        Checks:
        - Liquidation buffer >= grid_range * liq_buffer_mult
        - worst_case_loss under max inventory <= max_daily_drawdown
        - Flattenability constraint satisfied

        Returns list of violation messages (empty = pass).
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Flattenability constraint (section 5.7)
    # ------------------------------------------------------------------

    def compute_effective_hard_cap(
        self,
        config: AssetConfig,
        current_spread_bps: float,
        recent_avg_depth: float,
    ) -> float:
        """Compute effective hard cap respecting flattenability.

        max_flattenable = (max_flatten_slippage - spread) / depth_impact_scale * depth
        effective_hard_cap = min(hard_cap, max_flattenable)
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Error tracking
    # ------------------------------------------------------------------

    def record_error(self, is_maintenance: bool = False) -> None:
        """Record an error. Maintenance errors do NOT count (section 2.5)."""
        if not is_maintenance:
            self._consecutive_errors += 1

    def clear_errors(self) -> None:
        self._consecutive_errors = 0
