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

# Rolling window durations in milliseconds
_24H_MS = 24 * 60 * 60 * 1000
_168H_MS = 168 * 60 * 60 * 1000

# Vol history window thresholds in milliseconds
_48H_MS = 48 * 60 * 60 * 1000
_7D_MS = 7 * 24 * 60 * 60 * 1000

# Taker fee estimate for the worst-case-loss preflight formula (section 6.3:
# worst_case_loss = grid_range*position + flatten_slippage + taker_fees).
# Mirrors MarketData._TAKER_FEE_RATE (0.05%); kept local rather than imported
# to avoid coupling this pure risk-check module to MarketData.
_TAKER_FEE_BPS = 5.0


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

        # Rolling drawdown tracking: flat list of (timestamp_ms, equity) tuples.
        # Account-level — one account shared across assets.
        self._equity_history: list[tuple[int, float]] = []

        # Error tracking
        self._consecutive_errors: int = 0
        self._last_sync_ms: int = 0

        # Vol history for percentile calculation: {symbol: [(timestamp_ms, vol)]}
        self._vol_history: dict[str, list[tuple[int, float]]] = {}

        # Vol recovery timer: {symbol: timestamp_ms when vol first dropped below pause}
        self._vol_recovery_start: dict[str, int | None] = {}

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
        config = self._asset_config(state.symbol)

        # 1. Drawdown
        result = self._check_drawdown(state.symbol, state.account_equity)
        if result is not None:
            return result

        # 2. Errors / desync
        result = self._check_errors()
        if result is not None:
            return result

        # 3. Breakout
        if state.vol_metrics is not None and state.grid_config is not None:
            result = self._check_breakout(
                state.mid_price,
                state.grid_config.anchor,
                state.vol_metrics.atr,
                state.vol_metrics,
                config,
            )
            if result is not None:
                return result

        # 4. Volatility
        if state.vol_metrics is not None:
            result = self._check_volatility(state.symbol, state.vol_metrics, config)
            if result is not None:
                return result

        # 5. Funding
        result = self._check_funding(state.funding_rate, state.position, config)
        if result is not None:
            return result

        # 6. Inventory
        result = self._check_inventory(state.position, config)
        if result is not None:
            return result

        # 7. Momentum
        if state.vol_metrics is not None and state.mid_price > 0:
            result = self._check_momentum(state.vol_metrics, state.mid_price)
            if result is not None:
                return result

        return RiskDecision(action=RiskAction.CONTINUE, reason="all checks passed")

    # ------------------------------------------------------------------
    # Individual risk checks
    # ------------------------------------------------------------------

    def _check_drawdown(self, symbol: str, current_equity: float) -> RiskDecision | None:
        """Rolling 24h / 168h drawdown check (section 6.6).

        Includes both realized and unrealized PnL.
        Uses rolling windows, not calendar-based.
        Equity history is account-level; symbol is used to look up drawdown limits.
        """
        if not self._equity_history:
            return None

        now_ms = self._equity_history[-1][0]
        config = self._asset_config(symbol)

        # Check 24h window
        window_24h = [(ts, eq) for ts, eq in self._equity_history if ts >= now_ms - _24H_MS]
        if window_24h:
            peak_24h = max(eq for _, eq in window_24h)
            if peak_24h > 0:
                drawdown_24h = (peak_24h - current_equity) / peak_24h
                if drawdown_24h >= config.max_daily_drawdown_pct:
                    return RiskDecision(
                        action=RiskAction.KILL,
                        reason=f"24h drawdown {drawdown_24h:.2%} >= limit {config.max_daily_drawdown_pct:.2%}",
                        details={"drawdown_24h": drawdown_24h, "peak_24h": peak_24h},
                    )

        # Check 168h window
        window_168h = [(ts, eq) for ts, eq in self._equity_history if ts >= now_ms - _168H_MS]
        if window_168h:
            peak_168h = max(eq for _, eq in window_168h)
            if peak_168h > 0:
                drawdown_168h = (peak_168h - current_equity) / peak_168h
                if drawdown_168h >= config.max_weekly_drawdown_pct:
                    return RiskDecision(
                        action=RiskAction.KILL,
                        reason=f"168h drawdown {drawdown_168h:.2%} >= limit {config.max_weekly_drawdown_pct:.2%}",
                        details={"drawdown_168h": drawdown_168h, "peak_168h": peak_168h},
                    )

        return None

    def _check_errors(self) -> RiskDecision | None:
        """Check consecutive error count and desync duration."""
        op = self._config.operational
        if self._consecutive_errors >= op.max_consecutive_errors:
            return RiskDecision(
                action=RiskAction.KILL,
                reason=f"consecutive errors {self._consecutive_errors} >= {op.max_consecutive_errors}",
                details={"consecutive_errors": self._consecutive_errors},
            )

        if self._last_sync_ms > 0:
            # _time_desynced_ms is set externally by the Supervisor
            desynced_s = self._last_sync_ms / 1000.0
            if desynced_s >= op.max_time_desynced_seconds:
                return RiskDecision(
                    action=RiskAction.KILL,
                    reason=f"desynced {desynced_s:.1f}s >= {op.max_time_desynced_seconds}s",
                    details={"desynced_seconds": desynced_s},
                )

        return None

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
        if atr <= 0:
            return None

        # Distance breakout
        breakout_distance = config.breakout_atr_distance * atr
        if abs(mid_price - anchor) > breakout_distance:
            return RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason=f"distance breakout: |{mid_price} - {anchor}| > {breakout_distance:.2f}",
                details={"type": "distance", "distance": abs(mid_price - anchor), "threshold": breakout_distance},
            )

        # 5m return breakout: threshold = 2.0 * ATR / price (design doc section 6.3)
        if mid_price > 0:
            return_threshold = 2.0 * atr / mid_price
            if abs(vol_metrics.rolling_return_5m) > return_threshold:
                return RiskDecision(
                    action=RiskAction.CANCEL_AND_FLATTEN,
                    reason=f"5m return breakout: |{vol_metrics.rolling_return_5m:.6f}| > {return_threshold:.6f}",
                    details={"type": "return_5m", "return_5m": vol_metrics.rolling_return_5m, "threshold": return_threshold},
                )

        # Vol spike: realized vol > vol_kill_percentile
        vol_percentile = self._compute_vol_percentile(config.symbol, vol_metrics.realized_vol)
        if vol_percentile is not None and vol_percentile > config.vol_kill_percentile:
            # This duplicates _check_volatility's kill threshold and always
            # fires first in evaluate()'s check order (breakout before vol),
            # so _check_volatility's kill branch is never reached for this
            # event. Arm the recovery timer here too, or the mandatory
            # sustained-normalization wait (section 6.4) never starts.
            self._vol_recovery_start[config.symbol] = None
            return RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason=f"vol spike: percentile {vol_percentile:.2f} > {config.vol_kill_percentile}",
                details={"type": "vol_spike", "percentile": vol_percentile},
            )

        return None

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
        vol_percentile = self._compute_vol_percentile(symbol, vol_metrics.realized_vol)
        if vol_percentile is None:
            return None

        # Kill threshold (more severe, check first)
        if vol_percentile > config.vol_kill_percentile:
            # Mark that recovery will be needed when vol drops
            self._vol_recovery_start[symbol] = None
            return RiskDecision(
                action=RiskAction.CANCEL_AND_FLATTEN,
                reason=f"vol kill: percentile {vol_percentile:.2f} > {config.vol_kill_percentile}",
                details={"percentile": vol_percentile},
            )

        # Pause threshold
        if vol_percentile > config.vol_pause_percentile:
            # Mark that recovery will be needed when vol drops
            self._vol_recovery_start[symbol] = None
            return RiskDecision(
                action=RiskAction.PAUSE_GRID,
                reason=f"vol pause: percentile {vol_percentile:.2f} > {config.vol_pause_percentile}",
                details={"percentile": vol_percentile},
            )

        # Vol is below pause — check if recovery is needed.
        # Key not in dict → vol was never elevated for this symbol → no recovery.
        # Key is None → vol was elevated, just dropped → start recovery timer.
        # Key is a timestamp → recovery in progress → check elapsed.
        if symbol not in self._vol_recovery_start:
            return None

        recovery_start = self._vol_recovery_start[symbol]
        if recovery_start is None:
            # Vol just dropped below pause — start the recovery timer.
            # Grid stays paused until recovery period elapses.
            vol_hist = self._vol_history.get(symbol, [])
            if vol_hist:
                self._vol_recovery_start[symbol] = vol_hist[-1][0]
            return RiskDecision(
                action=RiskAction.PAUSE_GRID,
                reason="vol recovery: timer started, waiting for sustained normalization",
                details={"recovery_remaining_ms": config.vol_recovery_minutes * 60 * 1000},
            )

        # Check if recovery period has elapsed
        vol_hist = self._vol_history.get(symbol, [])
        if vol_hist:
            now_ms = vol_hist[-1][0]
            recovery_ms = config.vol_recovery_minutes * 60 * 1000
            if now_ms - recovery_start < recovery_ms:
                return RiskDecision(
                    action=RiskAction.PAUSE_GRID,
                    reason="vol recovery: waiting for sustained normalization",
                    details={"recovery_remaining_ms": recovery_ms - (now_ms - recovery_start)},
                )

        # Recovery period elapsed — clear state and resume
        del self._vol_recovery_start[symbol]
        return None

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
        abs_funding = abs(funding_rate)

        if abs_funding <= config.funding_moderate_threshold:
            return None

        # Extreme funding with wrong-side position -> PAUSE
        if abs_funding > config.funding_extreme_threshold:
            if position is not None and position.size != 0:
                # "Wrong side" = position is on the funding-paying side.
                # Positive funding rate means longs pay shorts.
                # If funding > 0 and position > 0: long is paying -> wrong side.
                # If funding < 0 and position < 0: short is paying -> wrong side.
                paying_side = (funding_rate > 0 and position.size > 0) or \
                              (funding_rate < 0 and position.size < 0)
                if paying_side:
                    return RiskDecision(
                        action=RiskAction.PAUSE_GRID,
                        reason=f"extreme funding {funding_rate:.4f} with wrong-side position {position.size}",
                        details={"funding_rate": funding_rate, "position": position.size},
                    )

        # Moderate (or extreme but right-side/no position) -> SKEW
        return RiskDecision(
            action=RiskAction.SKEW_FUNDING,
            reason=f"moderate funding {funding_rate:.4f} > {config.funding_moderate_threshold}",
            details={"funding_rate": funding_rate},
        )

    def _check_inventory(
        self,
        position: Position | None,
        config: AssetConfig,
    ) -> RiskDecision | None:
        """Inventory cap enforcement (section 6.2)."""
        if position is None or position.size == 0:
            return None

        abs_pos = abs(position.size)
        hard_cap = config.max_abs_position
        soft_cap = config.soft_cap_pct * hard_cap

        if hard_cap <= 0:
            return None

        if abs_pos >= hard_cap:
            return RiskDecision(
                action=RiskAction.REDUCE_ONLY,
                reason=f"hard cap: |{position.size}| >= {hard_cap}",
                details={"position": position.size, "hard_cap": hard_cap},
            )

        if abs_pos >= soft_cap:
            return RiskDecision(
                action=RiskAction.SKEW_INVENTORY,
                reason=f"soft cap: |{position.size}| >= {soft_cap}",
                details={"position": position.size, "soft_cap": soft_cap, "hard_cap": hard_cap},
            )

        return None

    def _check_momentum(self, vol_metrics: VolMetrics, mid_price: float) -> RiskDecision | None:
        """Momentum micro-filter (section 8.3).

        Suppress new entries if:
        - abs(rolling_return_5m) > 1.2 * ATR / mid_price
        - abs(rolling_return_1m) > 0.5 * ATR / mid_price

        Rolling returns are fractional (price-relative), thresholds are ATR / price.
        """
        atr = vol_metrics.atr
        if atr <= 0 or mid_price <= 0:
            return None

        atr_frac = atr / mid_price

        # 5m threshold
        threshold_5m = 1.2 * atr_frac
        if abs(vol_metrics.rolling_return_5m) > threshold_5m:
            return RiskDecision(
                action=RiskAction.SUPPRESS_NEW_ENTRIES,
                reason=f"momentum 5m: |{vol_metrics.rolling_return_5m:.6f}| > {threshold_5m:.6f}",
                details={"trigger": "5m", "return_5m": vol_metrics.rolling_return_5m},
            )

        # 1m threshold
        threshold_1m = 0.5 * atr_frac
        if abs(vol_metrics.rolling_return_1m) > threshold_1m:
            return RiskDecision(
                action=RiskAction.SUPPRESS_NEW_ENTRIES,
                reason=f"momentum 1m: |{vol_metrics.rolling_return_1m:.6f}| > {threshold_1m:.6f}",
                details={"trigger": "1m", "return_1m": vol_metrics.rolling_return_1m},
            )

        return None

    # ------------------------------------------------------------------
    # Portfolio-level checks (section 9.2)
    # ------------------------------------------------------------------

    def check_portfolio_delta(
        self,
        positions: dict[str, Position],
        account_equity: float = 0.0,
    ) -> bool:
        """Check if portfolio delta exceeds the portfolio cap.

        portfolio_delta = abs(sum(pos.size * mark_price for pos in positions))
        portfolio_cap = total_risk_budget_pct * portfolio_delta_mult * account_equity

        Uses mark price (derived from avg_entry + unrealized_pnl / size).
        Returns True if cap is breached.
        """
        if not positions or account_equity <= 0:
            return False

        total_delta_usd = 0.0
        for pos in positions.values():
            if pos.size == 0:
                continue
            # mark_price ≈ avg_entry + unrealized_pnl / size
            mark_price = pos.avg_entry_price
            if pos.unrealized_pnl != 0:
                mark_price = pos.avg_entry_price + pos.unrealized_pnl / pos.size
            total_delta_usd += pos.size * mark_price

        portfolio_delta = abs(total_delta_usd)
        portfolio_cap = (
            self._portfolio_config.total_risk_budget_pct
            * self._portfolio_config.portfolio_delta_mult
            * account_equity
        )
        return portfolio_delta > portfolio_cap

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
        1. Vol below pause threshold (percentile-based)
        2. Price within X * ATR of moving average
        3. No recent breakout within cooldown window
        """
        # Check vol history sufficiency
        if not self._vol_history_sufficient(symbol):
            return Regime.UNKNOWN

        # Signal 1: Volatility level
        vol_percentile = self._compute_vol_percentile(symbol, vol_metrics.realized_vol)
        if vol_percentile is None:
            return Regime.UNKNOWN

        # Apply bootstrap bias: tighten vol_pause_percentile during bootstrap
        effective_pause_threshold = self._bootstrap_adjusted_threshold(
            symbol, config.vol_pause_percentile
        )

        if vol_percentile > effective_pause_threshold:
            return Regime.HIGH_VOL

        # Signal 2: Price distance from moving average (trend filter)
        # RANGE requires price within 2.0 * ATR of moving average
        atr = vol_metrics.atr
        if atr > 0 and moving_avg > 0:
            trend_distance = abs(mid_price - moving_avg)
            trend_threshold = 2.0 * atr
            if trend_distance > trend_threshold:
                return Regime.TREND

        # Signal 3: No recent breakout within cooldown
        if last_breakout_ms is not None:
            cooldown_ms = config.cooldown_minutes * 60 * 1000
            if now_ms - last_breakout_ms < cooldown_ms:
                return Regime.TREND

        return Regime.RANGE

    def _vol_history_sufficient(self, symbol: str) -> bool:
        """Check if minimum vol history window (48h) has been met."""
        history = self._vol_history.get(symbol, [])
        if len(history) < 2:
            return False
        oldest_ts = history[0][0]
        newest_ts = history[-1][0]
        span_ms = newest_ts - oldest_ts
        return span_ms >= _48H_MS

    def _bootstrap_adjusted_threshold(self, symbol: str, base_threshold: float) -> float:
        """Tighten vol_pause_percentile during bootstrap (48h–7d).

        At 48h: use 70th percentile (tighter) instead of configured threshold.
        At 7d: use configured threshold as-is.
        Linear interpolation between.
        """
        history = self._vol_history.get(symbol, [])
        if len(history) < 2:
            return base_threshold

        oldest_ts = history[0][0]
        newest_ts = history[-1][0]
        span_ms = newest_ts - oldest_ts

        if span_ms >= _7D_MS:
            # Steady state — no bias
            return base_threshold

        # Bootstrap bias: interpolate between tighter threshold and configured
        # At 48h: bootstrap_threshold. At 7d: base_threshold.
        bootstrap_threshold = min(base_threshold, 0.70)
        progress = max(0.0, (span_ms - _48H_MS) / (_7D_MS - _48H_MS))
        return bootstrap_threshold + progress * (base_threshold - bootstrap_threshold)

    def _compute_vol_percentile(self, symbol: str, current_vol: float) -> float | None:
        """Compute percentile of current vol within the rolling history.

        Returns None if insufficient history.
        """
        history = self._vol_history.get(symbol, [])
        if not history:
            return None

        vol_values = [v for _, v in history]
        if not vol_values:
            return None

        count_below = sum(1 for v in vol_values if v <= current_vol)
        return count_below / len(vol_values)

    # ------------------------------------------------------------------
    # Vol history management
    # ------------------------------------------------------------------

    def record_vol(self, symbol: str, timestamp_ms: int, realized_vol: float) -> None:
        """Record a vol observation for percentile calculations.

        Called each cycle by the Supervisor with fresh vol data.
        Trims history to 7 days.
        """
        if symbol not in self._vol_history:
            self._vol_history[symbol] = []

        self._vol_history[symbol].append((timestamp_ms, realized_vol))

        # Trim to 7 day window
        cutoff = timestamp_ms - _7D_MS
        self._vol_history[symbol] = [
            (ts, v) for ts, v in self._vol_history[symbol] if ts >= cutoff
        ]

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
        violations: list[str] = []

        if account_equity <= 0:
            violations.append("account_equity must be positive")
            return violations

        # 1. Liquidation buffer check
        # liq_distance >= grid_range * liq_buffer_mult (2-3x)
        # grid_range ≈ levels_per_side * grid_step_bps_max / 10000 * price
        # liq_distance ≈ 1/leverage (simplified)
        # We check: 1/leverage >= (levels_per_side * grid_step_bps_max / 10000) * liq_buffer_mult
        liq_buffer_mult = 2.0  # Conservative default
        liq_distance_frac = 1.0 / config.leverage if config.leverage > 0 else 0
        grid_range_frac = config.levels_per_side * config.grid_step_bps_max / 10000
        required_liq_distance = grid_range_frac * liq_buffer_mult

        if liq_distance_frac < required_liq_distance:
            violations.append(
                f"liq buffer insufficient: 1/leverage={liq_distance_frac:.4f} < "
                f"grid_range*{liq_buffer_mult}={required_liq_distance:.4f}"
            )

        # 2. Worst-case loss check
        # worst_case_loss = grid_range * max_abs_position + flatten_slippage + taker_fees
        # Must be <= max_daily_drawdown * account_equity
        if config.max_abs_position > 0:
            worst_case_loss_frac = (
                grid_range_frac * config.max_abs_position  # grid range loss in USD-ish
                + config.max_flatten_slippage_bps / 10000 * config.max_abs_position  # flatten slippage
            )
            # Normalize: this is a price-relative loss. Convert to equity fraction.
            # Approximate: worst_case_loss ≈ grid_range_frac * max_position_usd + flatten_slippage + taker_fees
            # where max_position_usd ≈ max_abs_position * price. Without price, we use
            # the fraction-based approach: loss_pct = grid_range_frac + flatten_slippage_bps/10000 + taker_fee_bps/10000
            # applied to the notional of max_abs_position.
            # Since we can't get price here, use the equity-relative budget check:
            # (grid_range_frac + flatten_slippage_frac + taker_fee_frac) * leverage * capital_allocation
            loss_pct_of_equity = (
                (grid_range_frac + config.max_flatten_slippage_bps / 10000 + _TAKER_FEE_BPS / 10000)
                * config.leverage
                * config.capital_allocation
            )
            if loss_pct_of_equity > config.max_daily_drawdown_pct:
                violations.append(
                    f"worst-case loss {loss_pct_of_equity:.4f} > "
                    f"max_daily_drawdown {config.max_daily_drawdown_pct:.4f}"
                )

        # 3. Flattenability — just check the formula is valid
        # This is a runtime check; pre-flight validates the config is plausible
        if config.max_flatten_slippage_bps <= 0:
            violations.append("max_flatten_slippage_bps must be positive")

        return violations

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
        remaining_budget_bps = config.max_flatten_slippage_bps - current_spread_bps
        if remaining_budget_bps <= 0 or config.depth_impact_scale <= 0:
            # Spread exceeds slippage budget — effective cap is zero/minimum
            return 0.0

        max_flattenable = (remaining_budget_bps / config.depth_impact_scale) * recent_avg_depth
        effective = min(config.max_abs_position, max_flattenable)

        if max_flattenable < config.max_abs_position:
            logger.warning(
                "Flattenability constraint tightens %s hard cap: %.4f -> %.4f "
                "(spread=%.1f bps, depth=%.2f)",
                config.symbol,
                config.max_abs_position,
                effective,
                current_spread_bps,
                recent_avg_depth,
            )

        return max(effective, 0.0)

    # ------------------------------------------------------------------
    # Equity tracking (for drawdown)
    # ------------------------------------------------------------------

    def record_equity(self, timestamp_ms: int, equity: float) -> None:
        """Record an equity observation for drawdown calculation.

        Called each cycle by the Supervisor with exchange-reported account equity.
        Trims history to 168h window.
        """
        self._equity_history.append((timestamp_ms, equity))

        # Trim to 168h window
        cutoff = timestamp_ms - _168H_MS
        self._equity_history = [
            (ts, eq) for ts, eq in self._equity_history if ts >= cutoff
        ]

    # ------------------------------------------------------------------
    # Error tracking
    # ------------------------------------------------------------------

    def record_error(self, is_maintenance: bool = False) -> None:
        """Record an error. Maintenance errors do NOT count (section 2.5)."""
        if not is_maintenance:
            self._consecutive_errors += 1

    def clear_errors(self) -> None:
        self._consecutive_errors = 0

    def record_desync(self, duration_ms: int) -> None:
        """Record the current desync duration (time since last successful sync)."""
        self._last_sync_ms = duration_ms

    def clear_desync(self) -> None:
        """Clear the desync timer after a successful sync."""
        self._last_sync_ms = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _asset_config(self, symbol: str) -> AssetConfig:
        """Look up the AssetConfig for a symbol."""
        for ac in self._config.assets:
            if ac.symbol == symbol:
                return ac
        # Fallback to first asset config
        return self._config.assets[0]
