"""GridEngine — pure calculation module with no side effects.

Responsibilities (design doc section 3.2):
- Takes current anchor, range, step, regime, inventory state as inputs
- Outputs a "desired orders" set: list of (price, side, size, flags) tuples
- Manages Core and Expansion layers (section 5.2)
- Applies inventory-aware skewing (section 6.2)
- Applies staggered placement (section 5.3)
- Handles anchor re-centering logic (section 5.1)
- Generates deterministic client order IDs (section 7.3)

IMPORTANT: This module has NO side effects. It does not talk to the exchange.
All exchange interaction is through OrderManager.
"""

from __future__ import annotations

import hashlib
import logging

from gridbot.config import AssetConfig, OperationalConfig
from gridbot.types import (
    AssetState,
    DesiredOrder,
    GridConfig,
    GridLayer,
    GridLevel,
    InventoryZone,
    OrderSide,
    PendingFlip,
    Regime,
    TimeInForce,
    VolMetrics,
)

logger = logging.getLogger(__name__)

# Conservative constants (design doc section 5.4)
MAKER_FEE_BPS = 0.2
SAFETY_MARGIN_BPS = 1.5

# Core range constant (design doc section 5.2)
CORE_RANGE_ATR = 2.5

# Minimum order sizes per asset (placeholder, validated in Phase 8)
MIN_ORDER_SIZE = {"BTC-PERP": 0.001, "ETH-PERP": 0.01}
DEFAULT_MIN_ORDER_SIZE = 0.001


class GridEngine:
    """Pure-function grid calculator. No I/O, no side effects."""

    def __init__(
        self, asset_config: AssetConfig, operational_config: OperationalConfig
    ) -> None:
        self._config = asset_config
        self._operational = operational_config

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def compute_desired_orders(self, state: AssetState) -> list[DesiredOrder]:
        """Compute the full desired order set for this cycle.

        This is the main method called by the Supervisor each cycle.
        It combines core grid, expansion grid, and pending flips into
        a single desired set, applying all filters and skewing.

        Returns an empty list if regime does not support grid activity.
        """
        if state.regime != Regime.RANGE:
            return []

        vol_metrics = state.vol_metrics
        if vol_metrics is None:
            return []

        grid_config = state.grid_config
        if grid_config is None:
            return []

        anchor = grid_config.anchor
        position_size = state.position.size if state.position else 0.0

        step_bps = self.compute_effective_step(vol_metrics, state.mid_price)
        order_size = self._compute_order_size(vol_metrics, state.account_equity)
        inventory_zone = self._classify_inventory_zone(position_size)

        # Core levels (always active in RANGE)
        core_levels = self._compute_core_levels(
            anchor, step_bps, vol_metrics, inventory_zone, position_size, order_size
        )

        # Expansion levels (conditional activation)
        expansion_levels = self._compute_expansion_levels(
            anchor, state.mid_price, step_bps, vol_metrics,
            inventory_zone, position_size, state.account_equity,
        )

        all_levels = core_levels + expansion_levels

        # Staggered placement
        all_levels = self._apply_stagger(
            all_levels, state.mid_price, state.stagger_placed_count
        )

        # Convert GridLevels to DesiredOrders with deterministic IDs
        config_hash = self.compute_config_hash(
            anchor, grid_config.range_atr, grid_config.step_bps
        )
        desired: list[DesiredOrder] = []
        for level in all_levels:
            cid = self.make_client_order_id(
                state.symbol, level.price, level.side, config_hash,
                grid_config.epoch,
            )
            desired.append(DesiredOrder(
                client_order_id=cid,
                symbol=state.symbol,
                price=level.price,
                size=level.size,
                side=level.side,
                time_in_force=TimeInForce.ALO,
                reduce_only=level.reduce_only,
            ))

        # Include pending flips
        desired = self._include_pending_flips(
            desired, state.pending_flips, grid_config, inventory_zone,
            position_size,
        )

        return desired

    # ------------------------------------------------------------------
    # Grid level computation
    # ------------------------------------------------------------------

    def _compute_core_levels(
        self,
        anchor: float,
        step_bps: float,
        vol_metrics: VolMetrics,
        inventory_zone: InventoryZone,
        position_size: float,
        order_size: float,
    ) -> list[GridLevel]:
        """Compute Core grid levels (section 5.2, Layer 1).

        Active only during RANGE regime.
        Range: +/- 2.5 ATR from anchor.
        Levels per side: config.levels_per_side (default 25).
        """
        atr = vol_metrics.atr
        core_range = CORE_RANGE_ATR * atr
        levels: list[GridLevel] = []

        # Buy levels below anchor
        for i in range(1, self._config.levels_per_side + 1):
            price = anchor * (1 - i * step_bps / 10_000)
            if abs(price - anchor) > core_range:
                break
            levels.append(GridLevel(
                price=price, side=OrderSide.BUY, size=order_size,
                layer=GridLayer.CORE,
            ))

        # Sell levels above anchor
        for i in range(1, self._config.levels_per_side + 1):
            price = anchor * (1 + i * step_bps / 10_000)
            if abs(price - anchor) > core_range:
                break
            levels.append(GridLevel(
                price=price, side=OrderSide.SELL, size=order_size,
                layer=GridLayer.CORE,
            ))

        levels = self._apply_inventory_skew(levels, position_size, inventory_zone)
        return levels

    def _compute_expansion_levels(
        self,
        anchor: float,
        mid_price: float,
        step_bps: float,
        vol_metrics: VolMetrics,
        inventory_zone: InventoryZone,
        position_size: float,
        account_equity: float,
    ) -> list[GridLevel]:
        """Compute Expansion grid levels (section 5.2, Layer 2).

        Active when price drifts beyond Core range but before breakout.
        Range: +/- expansion_range_atr ATR from anchor.
        Levels per side: config.expansion_levels_per_side (default 15).
        """
        atr = vol_metrics.atr
        if atr <= 0:
            return []

        core_range = CORE_RANGE_ATR * atr
        breakout_distance = self._config.breakout_atr_distance * atr
        drift = abs(mid_price - anchor)

        # Activation: mid price beyond core range but within breakout distance
        if drift <= core_range or drift >= breakout_distance:
            return []

        expansion_step_bps = step_bps * self._config.expansion_step_mult
        expansion_range = self._config.expansion_range_atr * atr

        # Expansion order size: use expansion_allocation fraction
        expansion_size = self._compute_expansion_order_size(
            vol_metrics, account_equity
        )

        levels: list[GridLevel] = []

        # Buy levels: from core_range below anchor out to expansion_range
        for i in range(1, self._config.expansion_levels_per_side + 1):
            offset = core_range + i * anchor * expansion_step_bps / 10_000
            if offset > expansion_range:
                break
            price = anchor - offset
            levels.append(GridLevel(
                price=price, side=OrderSide.BUY, size=expansion_size,
                layer=GridLayer.EXPANSION,
            ))

        # Sell levels: from core_range above anchor out to expansion_range
        for i in range(1, self._config.expansion_levels_per_side + 1):
            offset = core_range + i * anchor * expansion_step_bps / 10_000
            if offset > expansion_range:
                break
            price = anchor + offset
            levels.append(GridLevel(
                price=price, side=OrderSide.SELL, size=expansion_size,
                layer=GridLayer.EXPANSION,
            ))

        levels = self._apply_inventory_skew(levels, position_size, inventory_zone)
        return levels

    # ------------------------------------------------------------------
    # Grid spacing (section 5.4)
    # ------------------------------------------------------------------

    def compute_effective_step(
        self, vol_metrics: VolMetrics, mid_price: float
    ) -> float:
        """Compute fee+spread+slippage-aware grid step.

        effective_min_step = max(
            ATR_based_step,
            2 * maker_fee + current_spread_bps + grid_slippage_buffer + safety_margin
        )

        ATR_based_step = ATR / mid_price * 10_000, clamped to config bounds.
        """
        min_step = self._config.grid_step_bps_min
        max_step = self._config.grid_step_bps_max

        if mid_price > 0:
            atr_based_step = vol_metrics.atr / mid_price * 10_000
        else:
            atr_based_step = max_step

        atr_based_step = max(min_step, min(max_step, atr_based_step))

        # Friction floor
        slippage_buffer = self._compute_grid_slippage_buffer(vol_metrics)
        friction_floor = (
            2 * MAKER_FEE_BPS
            + vol_metrics.spread_bps
            + slippage_buffer
            + SAFETY_MARGIN_BPS
        )

        effective_step = max(atr_based_step, friction_floor)

        # Final clamp to configured bounds
        return max(min_step, min(max_step, effective_step))

    def _compute_grid_slippage_buffer(self, vol_metrics: VolMetrics) -> float:
        """Dynamic slippage buffer that scales with vol (section 5.7).

        grid_slippage_buffer = base_slippage_bps + (realized_vol / baseline_vol - 1) * vol_slippage_scale
        grid_slippage_buffer = clamp(grid_slippage_buffer, min_slippage_bps, max_slippage_bps)
        """
        cfg = self._config
        baseline = vol_metrics.baseline_vol

        if baseline <= 0:
            # No baseline — return max (conservative)
            return cfg.max_slippage_bps

        vol_ratio = vol_metrics.realized_vol / baseline
        buffer = cfg.base_slippage_bps + (vol_ratio - 1.0) * cfg.vol_slippage_scale

        return max(cfg.min_slippage_bps, min(cfg.max_slippage_bps, buffer))

    # ------------------------------------------------------------------
    # Order sizing (section 5.5)
    # ------------------------------------------------------------------

    def _compute_order_size(
        self,
        vol_metrics: VolMetrics,
        account_equity: float,
    ) -> float:
        """Vol-scaled order size: target_risk_per_level / realized_vol.

        target_risk_per_level = capital_allocation * account_equity / levels_per_side
        """
        cfg = self._config
        if account_equity <= 0:
            return self._min_order_size()

        target_risk = cfg.capital_allocation * account_equity / cfg.levels_per_side

        vol = vol_metrics.realized_vol
        # Guard against near-zero vol
        vol = max(vol, 0.01)

        order_size = target_risk / vol

        min_size = self._min_order_size()
        max_size = cfg.max_abs_position / cfg.levels_per_side if cfg.max_abs_position > 0 else min_size

        return max(min_size, min(max_size, order_size))

    def _compute_expansion_order_size(
        self,
        vol_metrics: VolMetrics,
        account_equity: float,
    ) -> float:
        """Expansion layer size using expansion_allocation fraction."""
        cfg = self._config
        if account_equity <= 0:
            return self._min_order_size()

        target_risk = cfg.expansion_allocation * account_equity / cfg.expansion_levels_per_side

        vol = max(vol_metrics.realized_vol, 0.01)
        order_size = target_risk / vol

        min_size = self._min_order_size()
        max_size = cfg.max_abs_position / cfg.expansion_levels_per_side if cfg.max_abs_position > 0 else min_size

        return max(min_size, min(max_size, order_size))

    def _min_order_size(self) -> float:
        return MIN_ORDER_SIZE.get(self._config.symbol, DEFAULT_MIN_ORDER_SIZE)

    # ------------------------------------------------------------------
    # Inventory skewing (section 6.2)
    # ------------------------------------------------------------------

    def _classify_inventory_zone(self, position_size: float) -> InventoryZone:
        """Classify current position into Normal / Soft Cap / Hard Cap."""
        cfg = self._config
        hard_cap = cfg.max_abs_position
        soft_cap = cfg.soft_cap_pct * hard_cap
        abs_pos = abs(position_size)

        if abs_pos >= hard_cap:
            return InventoryZone.HARD_CAP
        elif abs_pos >= soft_cap:
            return InventoryZone.SOFT_CAP
        else:
            return InventoryZone.NORMAL

    def _apply_inventory_skew(
        self,
        levels: list[GridLevel],
        position_size: float,
        inventory_zone: InventoryZone,
    ) -> list[GridLevel]:
        """Skew order sizes based on inventory zone.

        Normal: symmetric sizing.
        Soft cap: reduce exposure-increasing side, increase unwind side.
        Hard cap: cancel exposure-increasing orders, reduce-only on unwind.
        """
        if inventory_zone == InventoryZone.NORMAL:
            return list(levels)

        # Determine which side increases exposure
        if position_size > 0:
            # Long position: buying increases exposure
            increasing_side = OrderSide.BUY
        elif position_size < 0:
            # Short position: selling increases exposure
            increasing_side = OrderSide.SELL
        else:
            # Zero position: no skewing needed regardless of zone
            return list(levels)

        if inventory_zone == InventoryZone.HARD_CAP:
            result = []
            for level in levels:
                if level.side == increasing_side:
                    # Remove all exposure-increasing levels
                    continue
                # Unwind side: mark reduce_only
                result.append(GridLevel(
                    price=level.price,
                    side=level.side,
                    size=level.size,
                    layer=level.layer,
                    reduce_only=True,
                ))
            return result

        # SOFT_CAP: proportional skew
        cfg = self._config
        hard_cap = cfg.max_abs_position
        soft_cap = cfg.soft_cap_pct * hard_cap
        abs_pos = abs(position_size)

        # Skew factor: 0 at soft_cap boundary, 1 at hard_cap
        cap_range = hard_cap - soft_cap
        if cap_range <= 0:
            skew_factor = 1.0
        else:
            skew_factor = (abs_pos - soft_cap) / cap_range
        skew_factor = max(0.0, min(1.0, skew_factor))

        min_size = self._min_order_size()
        max_size = hard_cap / cfg.levels_per_side if hard_cap > 0 else min_size

        result = []
        for level in levels:
            if level.side == increasing_side:
                new_size = level.size * (1.0 - skew_factor)
                if new_size < min_size:
                    continue
            else:
                new_size = min(level.size * (1.0 + skew_factor), max_size)
            result.append(GridLevel(
                price=level.price,
                side=level.side,
                size=new_size,
                layer=level.layer,
                reduce_only=level.reduce_only,
            ))
        return result

    # ------------------------------------------------------------------
    # Staggered placement (section 5.3)
    # ------------------------------------------------------------------

    def _apply_stagger(
        self,
        levels: list[GridLevel],
        mid_price: float,
        placed_count: int,
    ) -> list[GridLevel]:
        """Apply staggered order placement.

        Place nearest N levels per side immediately.
        Queue remaining for progressive deployment.
        placed_count tracks additional levels promoted via fills.
        """
        initial = self._operational.stagger_initial_levels
        allowed_per_side = initial + placed_count

        buys = [l for l in levels if l.side == OrderSide.BUY]
        sells = [l for l in levels if l.side == OrderSide.SELL]

        # Sort by distance from mid (nearest first)
        buys.sort(key=lambda l: abs(l.price - mid_price))
        sells.sort(key=lambda l: abs(l.price - mid_price))

        return buys[:allowed_per_side] + sells[:allowed_per_side]

    # ------------------------------------------------------------------
    # Anchor management (section 5.1)
    # ------------------------------------------------------------------

    def should_reanchor(
        self,
        mid_price: float,
        current_anchor: float,
        atr: float,
        regime: Regime,
        vol_stable: bool,
        drift_start_ms: int | None,
        now_ms: int,
    ) -> bool:
        """Check all four re-anchoring conditions.

        1. abs(mid - anchor) > anchor_shift_threshold * ATR
        2. Drift persisted > anchor_delay
        3. Regime == RANGE
        4. Realized vol is stable or declining
        """
        cfg = self._config

        # Condition 3: regime must be RANGE
        if regime != Regime.RANGE:
            return False

        # Condition 4: vol must be stable
        if not vol_stable:
            return False

        # Condition 1: drift exceeds threshold
        drift = abs(mid_price - current_anchor)
        if drift <= cfg.anchor_shift_threshold_atr * atr:
            return False

        # Condition 2: drift has persisted beyond delay
        if drift_start_ms is None:
            return False
        elapsed_ms = now_ms - drift_start_ms
        delay_ms = cfg.anchor_delay_minutes * 60 * 1000
        if elapsed_ms < delay_ms:
            return False

        return True

    def compute_new_anchor(self, mid_price: float, old_anchor: float) -> float:
        """Compute the new anchor price after re-centering is approved."""
        return mid_price

    # ------------------------------------------------------------------
    # Pending flips (section 7.6)
    # ------------------------------------------------------------------

    def _include_pending_flips(
        self,
        desired: list[DesiredOrder],
        pending_flips: list[PendingFlip],
        grid_config: GridConfig,
        inventory_zone: InventoryZone,
        position_size: float = 0.0,
    ) -> list[DesiredOrder]:
        """Merge pending flip orders into the desired set.

        Pending flips are included regardless of current anchor/config.
        Subject to inventory cap enforcement (hard cap -> reduce-only).
        """
        if not pending_flips:
            return desired

        # Determine exposure-increasing side for hard cap enforcement
        if position_size > 0:
            increasing_side = OrderSide.BUY
        elif position_size < 0:
            increasing_side = OrderSide.SELL
        else:
            increasing_side = None

        # Build lookup of existing desired orders for dedup
        existing = {(d.price, d.side) for d in desired}

        result = list(desired)
        for flip in pending_flips:
            # Dedup: skip if a grid level already covers this price/side
            if (flip.price, flip.side) in existing:
                continue

            # Inventory cap: hard cap converts exposure-increasing flips to reduce_only
            reduce_only = False
            if inventory_zone == InventoryZone.HARD_CAP and flip.side == increasing_side:
                reduce_only = True

            cid = self.make_client_order_id(
                grid_config.symbol, flip.price, flip.side,
                f"flip:{flip.originating_fill_id}", grid_config.epoch,
            )

            result.append(DesiredOrder(
                client_order_id=cid,
                symbol=grid_config.symbol,
                price=flip.price,
                size=flip.size,
                side=flip.side,
                time_in_force=TimeInForce.ALO,
                reduce_only=reduce_only,
            ))
            existing.add((flip.price, flip.side))

        return result

    # ------------------------------------------------------------------
    # Deterministic order IDs (section 7.3)
    # ------------------------------------------------------------------

    @staticmethod
    def make_client_order_id(
        symbol: str,
        level_price: float,
        side: OrderSide,
        config_hash: str,
        epoch: int,
    ) -> str:
        """Generate deterministic client order ID.

        client_order_id = hash(symbol, level_price, side, grid_config_hash, epoch)
        """
        raw = f"{symbol}:{level_price}:{side.value}:{config_hash}:{epoch}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def compute_config_hash(anchor: float, range_atr: float, step_bps: float) -> str:
        """Hash of anchor + range + step for order ID disambiguation."""
        raw = f"{anchor}:{range_atr}:{step_bps}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]
