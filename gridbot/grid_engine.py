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

from gridbot.config import AssetConfig
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


class GridEngine:
    """Pure-function grid calculator. No I/O, no side effects."""

    def __init__(self, asset_config: AssetConfig) -> None:
        self._config = asset_config

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
        raise NotImplementedError

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
    ) -> list[GridLevel]:
        """Compute Core grid levels (section 5.2, Layer 1).

        Active only during RANGE regime.
        Range: +/- 2.5 ATR from anchor.
        Levels per side: 25.
        """
        raise NotImplementedError

    def _compute_expansion_levels(
        self,
        anchor: float,
        mid_price: float,
        step_bps: float,
        vol_metrics: VolMetrics,
        inventory_zone: InventoryZone,
        position_size: float,
    ) -> list[GridLevel]:
        """Compute Expansion grid levels (section 5.2, Layer 2).

        Active when price drifts beyond Core range but before breakout.
        Range: +/- expansion_range_atr ATR from anchor.
        Levels per side: 15.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Grid spacing (section 5.4)
    # ------------------------------------------------------------------

    def compute_effective_step(self, vol_metrics: VolMetrics) -> float:
        """Compute fee+spread+slippage-aware grid step.

        effective_min_step = max(
            ATR_based_step,
            2 * maker_fee + current_spread_bps + grid_slippage_buffer + safety_margin
        )
        """
        raise NotImplementedError

    def _compute_grid_slippage_buffer(self, vol_metrics: VolMetrics) -> float:
        """Dynamic slippage buffer that scales with vol (section 5.7)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Order sizing (section 5.5)
    # ------------------------------------------------------------------

    def _compute_order_size(
        self,
        vol_metrics: VolMetrics,
        account_equity: float,
    ) -> float:
        """Vol-scaled order size: target_risk_per_level / realized_vol."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Inventory skewing (section 6.2)
    # ------------------------------------------------------------------

    def _classify_inventory_zone(self, position_size: float) -> InventoryZone:
        """Classify current position into Normal / Soft Cap / Hard Cap."""
        raise NotImplementedError

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
        raise NotImplementedError

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
        """
        raise NotImplementedError

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
        raise NotImplementedError

    def compute_new_anchor(self, mid_price: float, old_anchor: float) -> float:
        """Compute the new anchor price after re-centering is approved."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Pending flips (section 7.6)
    # ------------------------------------------------------------------

    def _include_pending_flips(
        self,
        desired: list[DesiredOrder],
        pending_flips: list[PendingFlip],
        grid_config: GridConfig,
        inventory_zone: InventoryZone,
    ) -> list[DesiredOrder]:
        """Merge pending flip orders into the desired set.

        Pending flips are included regardless of current anchor/config.
        Subject to inventory cap enforcement (hard cap -> reduce-only).
        """
        raise NotImplementedError

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
